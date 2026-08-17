import os
import re
import time
import logging
import threading
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
from .telemetry import StageTimer, set_request_id

# 日志配置由 api/main.py(在线)或 kb/cli.py(离线)统一负责,
# 此处不调用 basicConfig,避免 import 顺序重置全局日志格式。
load_dotenv(override=True)

# ── 引用后处理的匹配规则(模块级,便于独立测试) ─────────────
# 引用行:整行内容为"参考自……"的各种变体,书名可能被书名号、
# 中英文引号或尖括号包裹,也可能不带任何包裹
_REFERENCE_LINE_RE = re.compile(
    r'^\s*>?\s*参考自\s*[:：]?\s*'
    r'[《"「\'<]?(?P<book>[^》"」\'>\n]+?)[》"」\'>]?\s*$',
    re.MULTILINE)
# 编号引用:[来源 N] / [参考 N] / [来源文档 N]
_NUM_REF_RE = re.compile(r'\[(?:来源|参考)(?:文档)?\s*(\d{1,2})\]')

# 思考强度的合法档位(参考 DSH 推理等级)
THINKING_EFFORTS = ("off", "high", "max")


def normalize_effort(effort) -> str:
    """思考强度校验:非法/空值一律回退 off

    与 DSH 的校验语义一致——显式选择必须是声明的档位之一,
    未知值不猜测、直接按关闭思考处理。
    """
    return effort if effort in THINKING_EFFORTS else "off"


class RAGPipeline:
    """RAG 流水线：将 Retriever 检索到的上下文喂给 Generator (DeepSeek) 进行回答"""
    
    def __init__(self, index_dir: Optional[str] = None,
                 kb_build_id: Optional[str] = None):
        """构造 RAG 流水线

        参数:
            index_dir: 知识库三件套所在目录。None 表示使用
                rag/ 传统目录(与旧行为一致)。
            kb_build_id: 知识库版本号(仅用于展示与热切换判定)。
        """
        self.config = get_config()

        logging.info("正在初始化检索器...")
        self.retriever = HybridRetriever(index_dir=index_dir)
        self.kb_build_id = kb_build_id or getattr(
            self.retriever, "kb_build_id", None)
        if self.kb_build_id:
            logging.info(f"知识库版本: {self.kb_build_id}")
        
        # 初始化回答缓存
        self.answer_cache = AnswerCache()
        
        logging.info("正在初始化大语言模型...")
        self.api_key = self.config.get("api_key")
        if not self.api_key or "your-api-key" in self.api_key:
            raise ValueError("请在 rag/.env 中正确配置 OPENAI_API_KEY")
        self.base_url = self.config.get("api_base_url")
        self.model_name = self.config.get("model")
        self._build_llm_clients()

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

        # 最近一次请求的耗时快照(设置页性能统计读取)
        self._last_timings = None

    def _build_llm_clients(self):
        """按当前配置构建两个客户端(初始化与热更新共用)"""
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

    def reconfigure(self):
        """设置保存后的运行时重建(密钥/端点/模型/参数保存即生效)

        与 DSH 的配置热更新体验一致:下一次请求立即用新值,
        不打断进行中的流(在途请求持有旧客户端引用)。
        """
        self.api_key = self.config.get("api_key")
        self.base_url = self.config.get("api_base_url")
        self.model_name = self.config.get("model")
        self._build_llm_clients()
        logging.info("对话 API 客户端已重建: model=%s", self.model_name)

    def swap_retriever(self, new_retriever, kb_build_id: str = ""):
        """原子替换检索器（知识库热更新入口，由 kb.watcher 调用）

        只做引用级替换：在途请求仍持有旧检索器对象，检索照常完成，
        不受切换影响。旧检索器延迟 60 秒后释放连接与内存
        （覆盖在途检索的最坏时长，过期后不再被任何请求引用）。
        """
        old = self.retriever
        self.retriever = new_retriever
        if kb_build_id:
            self.kb_build_id = kb_build_id
        logging.info(f"检索器已切换: kb_build_id={self.kb_build_id or 'legacy'}")
        if old is not None:
            threading.Thread(
                target=self._close_retriever_later,
                args=(old, 60.0),
                daemon=True,
            ).start()

    @staticmethod
    def _close_retriever_later(retriever, delay: float):
        time.sleep(delay)
        try:
            retriever.close()
        except Exception as e:
            logging.warning(f"延迟释放旧检索器失败: {e}")

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

    def _build_plan(self, question: str, history: Optional[list] = None):
        """执行 RAG_prompt 定义的检索前置分析，产出结构化检索计划

        用 planner_model 而不是回答所用的模型：前置分析只是把问题解构成
        JSON，推理型模型会为此消耗数倍 token 和时间（实测 9.8s vs 3.5s，
        而产出的命题质量相当）。留空则跟随回答模型。

        history: 最近的对话消息，用于还原追问中的指代
        （"那第二条呢？"这类问题脱离上下文无法解构）。
        """
        planner_model = (self.config.get("planner_model") or "").strip()
        return build_query_plan(
            sdk_client=self.sdk_client,
            model_name=planner_model or self.model_name,
            question=question,
            rag_prompt=self.rag_prompt,
            history=history,
        )

    # ── 引用后处理(问题 7) ──────────────────────────────────
    # 引用格式不再完全依赖模型自觉:后端在流结束后统一校验与修补。
    @staticmethod
    def _postprocess_references(answer: str,
                                sources: List[Dict]) -> tuple:
        """回答引用后处理,返回 (修正后的回答, 处理报告)

        1. 规范化引用行:把 "参考自:《xxx》" / "参考自 \"xxx\"" /
           缺书名号等变体统一为前端依赖的标准格式 "> 参考自《xxx》";
        2. 编号引用转换:模型违反提示词输出 "[来源 N]" 时,
           在有效范围内转换为行内 "（见《书名》）",无效编号直接删除;
        3. 统计引用覆盖率:哪些来源没被正文引用,供前端提示。
        """
        report = {"normalized": 0, "num_converted": 0,
                  "num_dropped": 0, "uncited": []}

        def _norm(m):
            book = (m.group("book") or "").strip()
            if not book:
                return m.group(0)
            report["normalized"] += 1
            return f"> 参考自《{book}》"

        answer = _REFERENCE_LINE_RE.sub(_norm, answer)

        books = [s.get("author", "") for s in sources]

        def _conv(m):
            n = int(m.group(1))
            if 1 <= n <= len(books) and books[n - 1]:
                report["num_converted"] += 1
                return f"（见《{books[n - 1]}》）"
            report["num_dropped"] += 1
            return ""

        answer = _NUM_REF_RE.sub(_conv, answer)

        if report["normalized"] or report["num_converted"] \
                or report["num_dropped"]:
            logging.info("引用后处理: 规范化 %d 行, 编号转换 %d, 丢弃 %d",
                         report["normalized"], report["num_converted"],
                         report["num_dropped"])

        # 覆盖率统计:模型引用行里写的书名(如《资本论》)与来源的
        # 任一字段(author 文件名 / 标题 / 章节末段)相等或互为子串
        # 即视为已引用——模型不会照抄文件名,精确匹配必然大量误报。
        cited_names = {m.group(1)
                       for m in re.finditer(r'参考自《([^》]+)》', answer)}

        def _is_cited(src: Dict) -> bool:
            cands = [
                (src.get("author") or "").strip(),
                (src.get("source_file") or "").replace(".md", "").strip(),
                ((src.get("chapter") or "").split(">")[-1]).strip(),
                (src.get("title") or "").strip(),
            ]
            cands = [c for c in cands if c]
            for name in cited_names:
                for cand in cands:
                    if name == cand or name in cand or cand in name:
                        return True
            return False

        report["uncited"] = [s.get("author", "") for s in sources
                             if s.get("author") and not _is_cited(s)]
        if report["uncited"]:
            logging.info("有 %d 条来源未被正文引用: %s",
                         len(report["uncited"]),
                         "、".join(report["uncited"][:5]))
        return answer, report

    def ask_stream(self, question: str, top_k: int = 5, fetch_k: int = 30,
                    thinking_effort: str = "off",
                    history: list = None, search_mode: bool = False,
                    request_id: Optional[str] = None):
        """流式执行 RAG 问答，逐个 token 产出

        thinking_effort: 思考强度,三档(参考 DSH 的推理等级设计):
            off  - 关闭思考,普通流式调用,秒级首字
            high - thinking enabled + reasoning_effort="high"
            max  - thinking enabled + reasoning_effort="max"
            非上述取值一律按 off 处理(与 DSH 的校验语义一致)。

        history: [{"role": "user"/"assistant", "content": str}, ...]
        search_mode: 是否启用联网搜索
        request_id: 请求关联 ID（日志与耗时统计用，可选）
        """
        set_request_id(request_id or "")
        timer = StageTimer()
        self._last_timings = None
        first_token_recorded = False

        # 思考强度校验:非法取值一律回退 off(与 DSH 的校验语义一致)
        effort = normalize_effort(thinking_effort)
        thinking_enabled = effort != "off"
        logging.info("思考强度: %s", effort)

        # 1. 联网搜索（如果启用）
        #
        # 从这里到正文首个 token 之间要等 7 秒以上（前置分析 + 检索 + 重排），
        # 期间前端只能显示"处理中"。逐阶段下发 stage 事件，让用户看得见
        # 系统在做什么、进行到哪一步。
        search_context = ""
        search_refs = []
        if search_mode:
            timer.start("search")
            yield {"type": "stage", "stage": "search", "text": "正在联网搜索"}
            body_len = int(self.config.get("web_search_excerpt"))
            search_results = web_search(question)
            search_context = format_search_results(search_results)
            search_refs = [{"title": r["title"], "body": r["body"][:body_len], "url": r["href"]}
                          for r in search_results if r.get("title")]
            ms = timer.end("search")
            # 搜索结果先行下发，前端可在正文生成前就展示参考链接
            if search_refs:
                yield {"type": "search_result", "references": search_refs}
            yield {"type": "stage", "stage": "search", "status": "done",
                   "elapsed_ms": round(ms, 1),
                   "text": f"联网搜索完成，获取 {len(search_refs)} 条结果"}
            logging.info("联网搜索完成: %d 条 (%.0fms)", len(search_refs), ms)

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
        # 缓存按知识库版本隔离：知识库热更新后，旧版本的缓存回答不会
        # 命中新版本，否则用户会拿到基于旧语料的答案。
        # 缓存里连同来源一起存，命中时来源要一并返回，
        # 否则正文有"参考自《xxx》"而来源卡片为空，前后不一致
        use_cache = bool(self.config.get("enable_answer_cache"))
        cache_kb = self.kb_build_id or "legacy"
        # 思考强度不同答案也不同,参与缓存键隔离;
        # off(默认)保持原键,历史缓存继续命中
        cache_query = question if effort == "off" \
            else f"[effort:{effort}] {question}"
        cached = self.answer_cache.get(cache_query, kb_version=cache_kb) \
            if use_cache else None
        if cached is not None and not search_mode:
            cached_answer, cached_sources = cached
            # 命中缓存时几乎瞬间返回，但仍要发一个阶段事件：
            # 前端的阶段面板据此才知道该结束，否则会一直停在"处理中"
            yield {"type": "stage", "stage": "cache", "status": "done",
                   "text": "命中历史回答缓存"}
            yield {"type": "token", "content": cached_answer}
            timings = timer.to_dict()
            self._last_timings = timings
            logging.info("缓存命中,直接返回 (%.0fms)", timings["total_ms"])
            yield {"type": "done", "sources": cached_sources,
                   "search_refs": [], "timings": timings}
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
            timer.start("analyze")
            yield {"type": "stage", "stage": "analyze", "text": "正在解构问题"}
            plan = self._build_plan(question, history)
            analyze_ms = timer.end("analyze")

            if plan.analysis_ok:
                # 把分析结论摊给用户看：这几条命题决定了接下来检索什么，
                # 展示出来既填补了等待，也让检索结果显得有据可循
                yield {
                    "type": "stage", "stage": "analyze", "status": "done",
                    "elapsed_ms": round(analyze_ms, 1),
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
                logging.info("问题解构完成 (%.0fms): 域=%s 命题=%d 范畴=%d",
                             analyze_ms, plan.domain or "?",
                             len(plan.propositions), len(plan.keywords))
            else:
                yield {"type": "stage", "stage": "analyze", "status": "skipped",
                       "elapsed_ms": round(analyze_ms, 1),
                       "text": "未做前置解构，直接按原问题检索"}

            channels = len(plan.dense_queries()) + 1 if plan.analysis_ok else 2
            timer.start("retrieve")
            yield {"type": "stage", "stage": "retrieve",
                   "text": f"正在检索文献（{channels} 路并行）"}
            docs = self.retriever.retrieve_by_plan(plan, top_k=top_k, fetch_k=fetch_k)
            retrieve_ms = timer.end("retrieve")
            yield {"type": "stage", "stage": "retrieve", "status": "done",
                   "elapsed_ms": round(retrieve_ms, 1),
                   "text": f"检索完成，选出 {len(docs)} 篇最相关文献"}
            logging.info("检索完成: %d 篇 (%.0fms)", len(docs), retrieve_ms)

            # 把前置分析结论作为认知锚点传给主思考模块
            plan_context = plan.to_context_block()
        except Exception as e:
            logging.warning(f"  检索环节失败，降级为无资料回答: {e}")
            timer.end("retrieve")
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
        # 补一个阶段提示，避免这里又出现无反馈的空档。
        # first_token 阶段从此刻开始计时，首个正文 token 到来时截止。
        _gen_text = ("正在深度思考" if effort == "max" else
                     ("正在思考" if effort == "high" else "正在组织回答"))
        yield {"type": "stage", "stage": "generate", "text": _gen_text}
        timer.start("first_token")

        prompt_vars = {
            "main_prompt": self.main_prompt,
            "plan_context": plan_context,
            "context": context_str,
            "question": question,
            "history_context": history_context,
            "search_context": search_context,
        }

        def _on_first_token():
            """首个正文 token:记录首 token 延迟,开始生成计时"""
            nonlocal first_token_recorded
            if not first_token_recorded:
                first_token_recorded = True
                ms = timer.end("first_token")
                timer.start("generate")
                logging.info("首个正文 token 到达 (%.0fms)", ms)

        try:
            if thinking_enabled:
                # 思考档映射(参考 DSH 的 resolveThinking):
                # off → 不传 thinking;high/max → type=enabled +
                # reasoning_effort=high/max
                langchain_messages = self.prompt.format_messages(**prompt_vars)
                openai_messages = [
                    {"role": {"human": "user", "ai": "assistant", "system": "system"}.get(m.type, m.type), "content": m.content}
                    for m in langchain_messages
                ]
                response = self.sdk_client.chat.completions.create(
                    model=self.model_name,
                    messages=openai_messages,
                    stream=True,
                    extra_body={"thinking": {
                        "type": "enabled",
                        "reasoning_effort": effort,
                    }},
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
                        _on_first_token()
                        yield {"type": "token", "content": delta.content}
                        response_parts.append(delta.content)
            else:
                for chunk in (self.prompt | self.llm).stream(prompt_vars):
                    if chunk.content:
                        _on_first_token()
                        yield {"type": "token", "content": chunk.content}
                        response_parts.append(chunk.content)
        except Exception as e:
            timer.end("generate")
            if not first_token_recorded:
                timer.end("first_token")
            logging.warning(f"生成阶段失败: {e} | {timer.summarize()}")
            timings = timer.to_dict()
            err_text = f"\n[错误] API 调用失败: {e}"
            if thinking_enabled:
                err_text += ("\n当前模型可能不支持深度思考,"
                             "请关闭思考或切换推理模型后重试。")
            yield {"type": "token", "content": err_text}
            yield {"type": "done", "sources": [], "search_refs": search_refs,
                   "timings": timings}
            return

        if not first_token_recorded:
            timer.end("first_token")
        generate_ms = timer.end("generate")
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

        # 引用后处理:规范引用行格式、转换编号引用、统计引用覆盖率
        full_answer, ref_report = self._postprocess_references(
            full_answer, sources)

        # 正文与来源一起写缓存，保证下次命中时两者仍然配套；
        # 按知识库版本隔离，热更新后旧版本缓存不再命中。
        # 思考档的答案只对同档命中(cache_query 已带 effort 前缀)
        if full_answer and not thinking_enabled and use_cache:
            self.answer_cache.put(cache_query, full_answer, sources,
                                  kb_version=cache_kb)

        timings = timer.to_dict()
        self._last_timings = timings
        logging.info("回答完成: %d 字 | %s", len(full_answer), timer.summarize())
        yield {"type": "done", "sources": sources, "search_refs": search_refs,
               "timings": timings, "ref_report": ref_report}
