# ADR-0001: 知识库单轨化 —— kb 数据工程作为唯一入口

- 状态: 已采纳
- 日期: 2026-08-18
- 决策者: 项目作者 + AI 协作者
- 关联: kb/ 离线数据工程、rag/retriever.py、api/main.py

## 背景

项目历史上存在两套知识库加载逻辑:

1. **legacy 路径**: `rag/retriever.py` 在 `index_dir=None` 时硬编码回退到
   `rag/` 目录,直接加载三件套(documents.db / faiss_index.idx / bm25_index.pkl)。
   这是 v1 时代的唯一方式。
2. **kb 数据工程**: `kb/` 包以 `data/releases.json` 为发布指针,构建产物在
   `data/builds/<id>/`,支持增量构建、门禁发布、热切换。

`kb seed` 把 v1 三件套登记为 `seed-v1` 基线后,两条路径在运行时
**指向同一份数据**(rag/ 目录),但代码层面仍双轨:retriever 自己决定回退、
cache 版本键有 `legacy` 影子、启动解析的失败回退语义模糊。

## 决策

**知识库目录与版本号的唯一决策者是 `kb.resolve_index_dir()`。**
retriever 不再自行决定"用哪个目录",只负责加载调用方给定的目录。

具体约定:

1. **入口统一**: `api/main.py` 启动时调用 `kb.resolve_index_dir()`,
   得到 `(index_dir, kb_build_id)`;显式传入 `RAGPipeline` 与 `HybridRetriever`。
2. **retriever 收口**: `HybridRetriever` 接受显式 `kb_build_id` 参数;
   不再从目录名推断传统目录为 None。传统 rag/ 目录只有在它被登记为
   seed-v1 时,才以 `(rag/, "seed-v1")` 形态出现。
3. **未初始化语义**: 无 `data/releases.json`(裸装未登记)时,
   `resolve_index_dir()` 返回 `(None, None)`,retriever 回退 rag/ 目录,
   但启动日志明确提示"运行 kb seed 登记基线"——这是**未初始化状态**,
   不是 legacy 体系。
4. **cache 版本键**: 已登记时用真实 build_id(seed-v1 / data-v2);
   `legacy` 仅作为"未登记"的兜底键,保留历史缓存兼容,不再视为并行体系。
5. **退役标准**: 发布首个 data-v2(非 seed)版本后,`index_dir=None`
   回退路径标记为 deprecated,并评估移除 `HybridRetriever` 对
   `faiss_path`/`db_path` 参数与 `RAG_DIR` 路径假设的兼容。

## 影响

- retriever / generator 构造签名新增 `kb_build_id` 透传,调用方需适配;
- 在线端启动日志区分"未初始化"与"指针损坏";
- cache 键语义澄清,不破坏既有缓存数据(legacy 键数据仍在,不迁移)。

## 非目标

- 不迁移历史缓存数据(旧 legacy 缓存保留,新写一律用 build_id);
- 不删除 legacy 加载代码本身(seed 基线依赖它),只收口决策权。

## 验证

- 测试套件全绿(含 kb 管道假嵌入测试);
- 端到端: seed 登记后服务以 seed-v1 启动,kb_version 接口返回 seed-v1;
- 裸装(删除 releases.json)启动仍回退 rag/,日志提示登记。
