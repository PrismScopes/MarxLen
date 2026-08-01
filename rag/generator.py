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
from .query_planner import load_main_prompt, load_rag_prompt, build_query_plan
from .config_store import get_config

logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')
# 关闭一些不需要的第三方库警告日志，保持终端清爽
logging.getLogger("httpx").setLevel(logging.WARNING)
# 如果觉得检索器的日志太刷屏，可以将其设置为 WARNING 级别
# logging.getLogger("retriever").setLevel(logging.WARNING)

load_dotenv(override=True)

class RAGPipeline:
    """RAG 流水线：将 Retriever 检索到的上下文喂给 Generator (DeepSeek) 进行回答"""
    
    def __init__(self):
        self.config = get_config()

        logging.info("正在初始化检索器...")
        self.retriever = HybridRetriever()
        
        # 初始化回答缓存
        self.answer_cache = AnswerCache()
        
        logging.info("正在初始化大语言模型 (DeepSeek)...")
        self.api_key = self.config.get("api_key")
        if not self.api_key or "replace_with_your_deepseek_key" in self.api_key:
            raise ValueError("请在 .env 文件中正确配置 DEEPSEEK_API_KEY")
            
        self.base_url = self.config.get("api_base_url")
        self.model_name = self.config.get("model")

        # 1. OpenAI SDK 客户端（用于思考模式，手动提取 reasoning_content）
        self.sdk_client = OpenAI(
            api_key=self.api_key,
            base_url=self.base_url,
        )
        
        # 2. LangChain 客户端（用于普通模式，使用简洁的 stream）
        #    max_tokens 为 0 表示不限制，需传 None 才能让服务端自行决定
        _max_tokens = self.config.get("max_tokens")
        self.llm = ChatOpenAI(
            model=self.model_name,
            api_key=self.api_key,
            base_url=self.base_url,
            temperature=self.config.get("temperature"),
            top_p=self.config.get("top_p"),
            max_tokens=_max_tokens if _max_tokens > 0 else None,
        )
        
        # ── 加载外部提示词 ──────────────────────────────────
        # 提示词存放在项目根 Prompt/ 目录，改提示词无需改代码
        self.main_prompt = load_main_prompt()
        self.rag_prompt = load_rag_prompt()

        # 主提示词作为 system message，用户输入与检索结果作为 user message。
        # 这样分离的好处：思考方法论属于模型的固有设定，不随每轮输入变化，
        # 也避免了长篇方法论淹没在用户消息里被模型轻视。
        template = """{plan_context}
{search_context}
{history_context}【参考文档】：
{context}

---

请依据上述结构化数据与参考文档，回答用户的问题：

{question}
"""
        self.prompt = ChatPromptTemplate.from_messages([
            ("system", "{main_prompt}"),
            ("human", template),
        ])

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

    def _build_plan(self, question: str):
        """执行 RAG_prompt 定义的检索前置分析，产出结构化检索计划

        用 planner_model 而不是回答所用的模型：前置分析只是把问题解构成
        JSON，推理型模型会为此消耗数倍 token 和时间（实测 9.8s vs 3.5s，
        而产出的命题质量相当）。留空则跟随回答模型。
        """
        planner_model = (self.config.get("planner_model") or "").strip()
        return build_query_plan(
            sdk_client=self.sdk_client,
            model_name=planner_model or self.model_name,
            question=question,
            rag_prompt=self.rag_prompt,
        )

    def ask_stream(self, question: str, top_k: int = 5, fetch_k: int = 30,
                    thinking_mode: bool = False,
                    history: list = None, search_mode: bool = False):
        """流式执行 RAG 问答，逐个 token 产出
        
        history: [{"role": "user"/"assistant", "content": str}, ...]
        search_mode: 是否启用联网搜索
        """
        # 1. 联网搜索（如果启用）
        #
        # 从这里到正文首个 token 之间要等 7 秒以上（前置分析 + 检索 + 重排），
        # 期间前端只能显示"处理中"。逐阶段下发 stage 事件，让用户看得见
        # 系统在做什么、进行到哪一步。
        search_context = ""
        search_refs = []
        if search_mode:
            yield {"type": "stage", "stage": "search", "text": "正在联网搜索"}
            body_len = int(self.config.get("web_search_excerpt"))
            search_results = web_search(question)
            search_context = format_search_results(search_results)
            search_refs = [{"title": r["title"], "body": r["body"][:body_len], "url": r["href"]}
                          for r in search_results if r.get("title")]
            # 搜索结果先行下发，前端可在正文生成前就展示参考链接
            if search_refs:
                yield {"type": "search_result", "references": search_refs}
            yield {"type": "stage", "stage": "search", "status": "done",
                   "text": f"联网搜索完成，获取 {len(search_refs)} 条结果"}

        # 2. 格式化对话历史（轮数设为 0 表示不携带上下文）
        history_context = ""
        turns = int(self.config.get("history_turns"))
        if history and turns > 0:
            msg_len = int(self.config.get("history_msg_len"))
            lines = ["【对话历史】："]
            for msg in history[-turns:]:
                label = "用户" if msg["role"] == "user" else "助手"
                lines.append(f"  {label}: {msg['content'][:msg_len]}")
            history_context = "\n".join(lines) + "\n\n"

        # 3. 检查缓存（仅无联网搜索时可用缓存）
        # 缓存里连同来源一起存，命中时来源要一并返回，
        # 否则正文有"参考自《xxx》"而来源卡片为空，前后不一致
        use_cache = bool(self.config.get("enable_answer_cache"))
        cached = self.answer_cache.get(question) if use_cache else None
        if cached is not None and not search_mode:
            cached_answer, cached_sources = cached
            # 命中缓存时几乎瞬间返回，但仍要发一个阶段事件：
            # 前端的阶段面板据此才知道该结束，否则会一直停在"处理中"
            yield {"type": "stage", "stage": "cache", "status": "done",
                   "text": "命中历史回答缓存"}
            yield {"type": "token", "content": cached_answer}
            yield {"type": "done", "sources": cached_sources, "search_refs": []}
            return

        # 4. 检索前置分析：解构问题 → 结构化检索计划（RAG_prompt 步骤一~三）
        # 5. 多通道检索（RAG_prompt 步骤四）
        #
        # 这两步依赖外部服务（分析 LLM、嵌入 API、Rerank API、SQLite），
        # 任一环节故障都不应让整个请求失败——没有资料时模型仍可基于自身
        # 理论素养作答，这比返回 500 对用户有用得多。
        # build_query_plan 内部已自带降级，此处再兜一层防不可预期的异常。
        docs = []
        plan_context = ""
        try:
            yield {"type": "stage", "stage": "analyze", "text": "正在解构问题"}
            plan = self._build_plan(question)

            if plan.analysis_ok:
                # 把分析结论摊给用户看：这几条命题决定了接下来检索什么，
                # 展示出来既填补了等待，也让检索结果显得有据可循
                yield {
                    "type": "stage", "stage": "analyze", "status": "done",
                    "text": "问题解构完成",
                    "detail": {
                        "domain": plan.domain,
                        "level": plan.level,
                        "nature": plan.nature,
                        "core_contradiction": plan.core_contradiction,
                        "propositions": plan.propositions,
                        "keywords": plan.keywords,
                    },
                }
            else:
                yield {"type": "stage", "stage": "analyze", "status": "skipped",
                       "text": "未做前置解构，直接按原问题检索"}

            channels = len(plan.dense_queries()) + 1 if plan.analysis_ok else 2
            yield {"type": "stage", "stage": "retrieve",
                   "text": f"正在检索文献（{channels} 路并行）"}
            docs = self.retriever.retrieve_by_plan(plan, top_k=top_k, fetch_k=fetch_k)
            yield {"type": "stage", "stage": "retrieve", "status": "done",
                   "text": f"检索完成，选出 {len(docs)} 篇最相关文献"}

            # 把前置分析结论作为认知锚点传给主思考模块
            plan_context = plan.to_context_block()
        except Exception as e:
            logging.warning(f"  检索环节失败，降级为无资料回答: {e}")
            plan_context = f"【原始问题】：{question}\n"
            yield {"type": "stage", "stage": "retrieve", "status": "failed",
                   "text": "检索未能完成，将基于理论素养作答"}

        # 即使没有检索到资料，也让 LLM 基于自身知识回答
        context_str = self._format_context(docs) if docs else ""
        if not docs and not search_context:
            logging.info("  未检索到相关资料，将基于自身知识回答")
            context_str = "（本次未检索到相关文献资料，请直接基于自身理论素养回答。）"

        # 6. 生成回答
        response_parts = []

        # 模型开始吐字前还有一段首 token 延迟，思考模式下尤其长，
        # 补一个阶段提示，避免这里又出现无反馈的空档
        yield {"type": "stage", "stage": "generate",
               "text": "正在深度思考" if thinking_mode else "正在组织回答"}

        prompt_vars = {
            "main_prompt": self.main_prompt,
            "plan_context": plan_context,
            "context": context_str,
            "question": question,
            "history_context": history_context,
            "search_context": search_context,
        }

        try:
            if thinking_mode:
                langchain_messages = self.prompt.format_messages(**prompt_vars)
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
                for chunk in (self.prompt | self.llm).stream(prompt_vars):
                    if chunk.content:
                        yield {"type": "token", "content": chunk.content}
                        response_parts.append(chunk.content)
        except Exception as e:
            yield {"type": "token", "content": f"\n[错误] API 调用失败: {e}"}
            yield {"type": "done", "sources": [], "search_refs": search_refs}
            return

        full_answer = "".join(response_parts)

        # 构造来源数据
        sources = []
        excerpt_len = int(self.config.get("excerpt_len"))
        for doc in docs:
            meta = doc.get('metadata', {})
            source_name = meta.get('source', '').replace('.md', '').strip()
            title_full = f"{meta.get('chapter', '')} > {meta.get('title', '')}" if meta.get('chapter') else meta.get('title', '')
            sources.append({
                "title": title_full or meta.get('title', ''),
                "author": source_name,
                "chapter": meta.get('chapter', ''),
                "score": doc.get('rerank_score', 0.0),
                "excerpt": self._clean_text(doc.get('text', '')[:excerpt_len]),
                "source_url": "",
                "doc_uuid": doc.get('uuid', ''),
                "source_file": meta.get('source', ''),
            })

        # 正文与来源一起写缓存，保证下次命中时两者仍然配套
        if full_answer and not thinking_mode and use_cache:
            self.answer_cache.put(question, full_answer, sources)

        yield {"type": "done", "sources": sources, "search_refs": search_refs}
