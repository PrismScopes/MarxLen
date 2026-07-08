import os
import re
import logging
from typing import List, Dict, Optional
from dotenv import load_dotenv

# LangChain 相关组件
from langchain_openai import ChatOpenAI
from langchain_core.prompts import ChatPromptTemplate
from openai import OpenAI  # 新增：用于绕过 LangChain 提取思考过程

# 导入我们刚刚写好的检索器
from .retriever import HybridRetriever
from .cache_store import AnswerCache
from .web_search import web_search, format_search_results

logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')
# 关闭一些不需要的第三方库警告日志，保持终端清爽
logging.getLogger("httpx").setLevel(logging.WARNING)
# 如果觉得检索器的日志太刷屏，可以将其设置为 WARNING 级别
# logging.getLogger("retriever").setLevel(logging.WARNING)

load_dotenv(override=True)

class RAGPipeline:
    """RAG 流水线：将 Retriever 检索到的上下文喂给 Generator (DeepSeek) 进行回答"""
    
    def __init__(self):
        logging.info("正在初始化检索器...")
        self.retriever = HybridRetriever()
        
        # 初始化回答缓存
        self.answer_cache = AnswerCache()
        
        logging.info("正在初始化大语言模型 (DeepSeek)...")
        self.api_key = os.getenv("DEEPSEEK_API_KEY")
        if not self.api_key or "replace_with_your_deepseek_key" in self.api_key:
            raise ValueError("请在 .env 文件中正确配置 DEEPSEEK_API_KEY")
            
        self.base_url = os.getenv("DEEPSEEK_API_BASE_URL", "https://api.deepseek.com/v1")
        self.model_name = os.getenv("DEEPSEEK_MODEL", "deepseek-chat")

        # 1. OpenAI SDK 客户端（用于思考模式，手动提取 reasoning_content）
        self.sdk_client = OpenAI(
            api_key=self.api_key,
            base_url=self.base_url,
        )
        
        # 2. LangChain 客户端（用于普通模式，使用简洁的 stream）
        self.llm = ChatOpenAI(
            model=self.model_name,
            api_key=self.api_key,
            base_url=self.base_url,
            temperature=0.3,
        )
        
        # 定义 Prompt 模板
        template = """你是一个精通马列哲学的专业智能问答助手。请根据以下【参考资料】和你的自身知识来回答用户的【问题】。

【回答要求】：
0. **不要在回答中复述或讨论上述格式要求，直接给出回答。你对格式规则的思考不应对用户可见。**
1. 优先从【参考资料】中提取信息进行回答。引用格式要求：
   - **引用必须在行文中穿插**，在引用具体观点后立即用 `> 参考自《资本论》` 标注来源
   - **`>` blockquote 行只允许写"参考自《xxx》"这一句话，正文内容绝对不能放在 `>` 行里**
   - **禁止把所有引用堆在回答末尾**
   - **禁止使用"[来源 N]"这种编号引用或普通文本的"参考自"**
   - **引用行前面必须空一行**（markdown 规则要求 blockquote 前有空行才能正确渲染），即：段落正文 → 空行 → `> 参考自《xxx》` → 空行 → 下一段正文
2. 如果在参考资料中找到了足够的信息，优先使用参考资料的内容。如果参考资料信息不完整或不够直接，**你可以自由结合自身的知识进行补充**，让回答完整、准确。
3. **禁止回答"找不到"或"参考资料中没有相关信息"** -- 即使参考资料不完全相关，你也必须基于自己的知识给出有实质内容的回答。
4. 凡是参考资料中提供的内容，请在其所在段落末尾立即标注来源（使用 `> 参考自《实际书名》` 格式）。你自己知识补充的部分不需要标注来源。
5. 回答要通俗易懂，条理清晰，适当使用分点说明提升阅读体验。

{search_context}
{history_context}
【参考资料】：
{context}

【问题】：
{question}
"""
        self.prompt = ChatPromptTemplate.from_template(template)

    @staticmethod
    def _clean_text(text: str) -> str:
        """清理文本中的 markdown 格式残留"""
        if not text:
            return ""
        # 移除 markdown 分隔线（***、---、* * *  等变体）
        text = re.sub(r'(?m)^[\s]*[\*\-_]{3,}[\s]*$', '', text)
        # 移除行首 > 引用标记
        text = re.sub(r'(?m)^>\s?', '', text)
        # 移除 # 标题标记
        text = re.sub(r'(?m)^#{1,6}\s+', '', text)
        # 合并多余空行为最多一个空行
        text = re.sub(r'\n{3,}', '\n\n', text)
        return text.strip()

    def _format_context(self, retrieved_docs: List[Dict]) -> str:
        """将检索到的多个文档片段，格式化成易于大模型阅读的字符串形式"""
        formatted_str = ""
        for i, doc in enumerate(retrieved_docs):
            chapter = doc.get('metadata', {}).get('chapter', '')
            title = doc.get('metadata', {}).get('title', '未知标题')
            source = doc.get('metadata', {}).get('source', '').replace('.md', '')
            text = self._clean_text(doc.get('text', ''))
            # 使用实际著作名称作为上下文标记，避免引导 LLM 输出 [来源 N]
            label = chapter or source or title
            formatted_str += f"《{label}》:\n"
            formatted_str += f"{text}\n\n"
        return formatted_str

    def ask(self, question: str, top_k: int = 3, thinking_mode: bool = False) -> tuple:
        """执行完整的 RAG 问答流程，并支持深度思考与流式输出
        
        返回: (完整回答字符串, 来源文档列表)
        """

        # 0. 检查回答缓存
        cached_answer = self.answer_cache.get(question)
        if cached_answer is not None:
            print(f"\n[回答]（缓存）\n{cached_answer}")
            return cached_answer, []

        # 1. 检索 (Retrieve)
        docs = self.retriever.retrieve(question, top_k=top_k)
        
        if not docs:
            print("\n[回答]\n抱歉，检索系统未能找到任何相关资料。")
            return "抱歉，检索系统未能找到任何相关资料。", []
            
        # 2. 拼接上下文
        context_str = self._format_context(docs)
        response_parts = []
        
        # 3. 生成回答 (Generate)
        try:
            # ────────── 思考模式 (使用 OpenAI SDK) ──────────
            if thinking_mode:
                reasoning_started = False
                thinking_done = False
                
                # 使用 LangChain 的 prompt 组装 messages
                langchain_messages = self.prompt.format_messages(context=context_str, question=question)
                openai_messages = [
                    {
                        "role": {"human": "user", "ai": "assistant", "system": "system"}.get(m.type, m.type),
                        "content": m.content,
                    }
                    for m in langchain_messages
                ]
                
                response = self.sdk_client.chat.completions.create(
                    model=self.model_name,
                    messages=openai_messages,
                    stream=True,
                    extra_body={"thinking": {"type": "enabled"}},
                )
                
                for chunk in response:
                    if not chunk.choices:
                        continue
                    delta = chunk.choices[0].delta
                    if not delta:
                        continue
                        
                    # 提取思考过程
                    reasoning = getattr(delta, "reasoning_content", None)
                    if reasoning:
                        if not reasoning_started:
                            print("\n╔══ 深度思考 ══╗", flush=True)
                            reasoning_started = True
                        print(reasoning, end="", flush=True)
                        continue
                        
                    if not getattr(delta, "content", None):
                        continue
                        
                    # 思考结束，开始输出正式内容
                    if not thinking_done:
                        if reasoning_started:
                            print("\n╚══════════════╝", flush=True)
                        print("\n[回答]", flush=True)
                        thinking_done = True
                        
                    print(delta.content, end="", flush=True)
                    response_parts.append(delta.content)
                    
            # ────────── 普通模式 (使用 LangChain) ──────────
            else:
                first_chunk = True
                
                for chunk in (self.prompt | self.llm).stream({"context": context_str, "question": question}):
                    if not chunk.content:
                        continue
                        
                    if first_chunk:
                        print("\n[回答]", flush=True)
                        first_chunk = False
                        
                    print(chunk.content, end="", flush=True)
                    response_parts.append(chunk.content)
                    
        except Exception as e:
            print(f"\n[错误] API 调用失败: {e}")
            return "", []
        
        # 4. 展示检索来源详情
        print("\n")
        print("─" * 60)
        print("【检索来源详情】")
        print("─" * 60)

        # 将完整回答写入缓存（跳过思考模式的结果，因为深度思考结果不稳定）
        full_answer = "".join(response_parts)

        # 后处理：将 [来源 N] 替换为实际著作名称
        if docs:
            # 先收集当前的来源列表
            def _get_source_ref(idx_str):
                try:
                    idx = int(idx_str)
                    if 0 < idx <= len(docs):
                        meta = docs[idx - 1].get('metadata', {})
                        src = meta.get('source', '').replace('.md', '')
                        ch = meta.get('chapter', '')
                        title = meta.get('title', '')
                        name = ch or src or title or f"来源{idx}"
                        return f"《{name}》" if not name.startswith("《") else name
                except (ValueError, IndexError):
                    pass
                return idx_str
            full_answer = re.sub(r'\[来源\s*(\d+)\]', lambda m: _get_source_ref(m.group(1)), full_answer)

        if full_answer and not thinking_mode:
            self.answer_cache.put(question, full_answer)
        for i, doc in enumerate(docs):
            meta = doc.get('metadata', {})
            source = meta.get('source', '') or ''
            chapter = meta.get('chapter', '')
            title = meta.get('title', '')
            text = doc.get('text', '')
            rerank = doc.get('rerank_score', '')
            rrf = doc.get('rrf_score', '')
            
            # 来源信息降级显示：source > chapter > title
            display_source = source if source else (f"{chapter} > {title}" if chapter else (title or '未知来源'))
            
            print(f"\n[来源 {i+1}]")
            print(f"  文件: {display_source}")
            if chapter and source:
                print(f"  章节: {chapter}")
            if title and source:
                print(f"  标题: {title}")
            if rerank:
                print(f"  相关度: {rerank:.4f}")
            print(f"  原文片段（前300字）:")
            print(f"  ┌{'─'*56}┐")
            for line in text[:300].split('\n'):
                if line.strip():
                    print(f"  │ {line[:80]}")
            if len(text) > 300:
                print(f"  │ ...（共 {len(text)} 字，已截取前300字）")
            print(f"  └{'─'*56}┘")
            
        return "".join(response_parts), docs

    def _rewrite_query(self, question: str, history: list) -> str:
        """用 LLM 理解用户问题，生成优化后的检索查询
        
        先理解问题的主旨和隐含条件，再提取核心检索关键词。
        """
        if len(question) < 10:
            return question

        try:
            # 第一步：分析问题
            analyze_prompt = (
                "分析下面用户问题的核心主题、隐含假设和关键概念。"
                "然后输出2-5个最可能在某学著作原文中找到答案的关键词（用空格分隔）。\n"
                "只输出关键词，不要解释。\n"
                f"问题：{question}"
            )

            resp = self.sdk_client.chat.completions.create(
                model=self.model_name,
                messages=[{"role": "user", "content": analyze_prompt}],
                max_tokens=60,
                temperature=0.1,
            )
            rewritten = resp.choices[0].message.content.strip()
            rewritten = rewritten.replace("\n", " ").replace('"', "").replace("'", "").replace("。", "").replace("，", "")
            logging.info(f"  查询理解: '{question[:40]}' → '{rewritten[:60]}'")
            # 如果提取出的关键词太短或太像原问题，就用原问题
            if len(rewritten) < 5 or rewritten == question:
                return question
            return rewritten
        except Exception as e:
            logging.warning(f"  查询理解失败: {e}")
            return question

    def ask_stream(self, question: str, top_k: int = 5, fetch_k: int = 30,
                    thinking_mode: bool = False,
                    history: list = None, search_mode: bool = False):
        """流式执行 RAG 问答，逐个 token 产出
        
        history: [{"role": "user"/"assistant", "content": str}, ...]
        search_mode: 是否启用联网搜索
        """
        # 1. 联网搜索（如果启用）
        search_context = ""
        search_refs = []
        if search_mode:
            search_results = web_search(question)
            search_context = format_search_results(search_results)
            search_refs = [{"title": r["title"], "body": r["body"][:200], "href": r["href"]}
                          for r in search_results if r.get("title")]

        # 2. 格式化对话历史
        history_context = ""
        if history:
            lines = ["【对话历史】："]
            for msg in history[-20:]:  # 最多保留最近 20 轮
                label = "用户" if msg["role"] == "user" else "助手"
                lines.append(f"  {label}: {msg['content'][:500]}")
            history_context = "\n".join(lines) + "\n\n"

        # 3. 检查缓存（仅无联网搜索时可用缓存）
        cached = self.answer_cache.get(question)
        if cached is not None and not search_mode:
            yield {"type": "token", "content": cached}
            yield {"type": "done", "sources": []}
            return

        # 4. 理解用户问题 → 优化检索查询
        search_query = self._rewrite_query(question, history)
        if search_query != question:
            logging.info(f"  原始问题: {question}")
            logging.info(f"  检索用词: {search_query}")

        # 5. 检索
        docs = self.retriever.retrieve(search_query, top_k=top_k, fetch_k=fetch_k)

        # 即使没有检索到资料，也让 LLM 基于自身知识回答
        context_str = self._format_context(docs) if docs else ""
        if not docs and not search_context:
            logging.info("  未检索到相关资料，将基于自身知识回答")
            context_str = "（本次未检索到相关文献资料，请直接基于自身知识回答。）"

        # 6. 生成回答
        response_parts = []

        try:
            if thinking_mode:
                langchain_messages = self.prompt.format_messages(
                    context=context_str, question=question,
                    history_context=history_context, search_context=search_context,
                )
                openai_messages = [
                    {"role": {"human": "user", "ai": "assistant", "system": "system"}.get(m.type, m.type), "content": m.content}
                    for m in langchain_messages
                ]
                response = self.sdk_client.chat.completions.create(
                    model=self.model_name,
                    messages=openai_messages,
                    stream=True,
                    extra_body={"thinking": {"type": "enabled"}},
                )
                for chunk in response:
                    if not chunk.choices:
                        continue
                    delta = chunk.choices[0].delta
                    if not delta:
                        continue
                    reasoning = getattr(delta, "reasoning_content", None)
                    if reasoning:
                        yield {"type": "thinking", "content": reasoning}
                        continue
                    if getattr(delta, "content", None):
                        yield {"type": "token", "content": delta.content}
                        response_parts.append(delta.content)
            else:
                for chunk in (self.prompt | self.llm).stream({
                    "context": context_str, "question": question,
                    "history_context": history_context, "search_context": search_context,
                }):
                    if chunk.content:
                        yield {"type": "token", "content": chunk.content}
                        response_parts.append(chunk.content)
        except Exception as e:
            yield {"type": "token", "content": f"\n[错误] API 调用失败: {e}"}
            yield {"type": "done", "sources": []}
            return

        full_answer = "".join(response_parts)
        if full_answer and not thinking_mode:
            self.answer_cache.put(question, full_answer)

        # 构造来源数据
        sources = []
        for doc in docs:
            meta = doc.get('metadata', {})
            source_name = meta.get('source', '').replace('.md', '').strip()
            title_full = f"{meta.get('chapter', '')} > {meta.get('title', '')}" if meta.get('chapter') else meta.get('title', '')
            sources.append({
                "title": title_full or meta.get('title', ''),
                "author": source_name,
                "chapter": meta.get('chapter', ''),
                "score": doc.get('rerank_score', doc.get('score', 0)),
                "excerpt": self._clean_text(doc.get('text', '')[:300]),
                "source_url": "",
            })

        yield {"type": "done", "sources": sources, "search_refs": search_refs}
