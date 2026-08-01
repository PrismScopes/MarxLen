<p align="center">
  <h1 align="center">MarxLen · 马列通</h1>
  <p align="center">基于检索增强生成（RAG）的马列经典著作智能问答系统</p>
</p>

## 这是什么

MarxLen（马列通）让你用自然语言向马列经典著作提问。系统先从《马克思恩格斯全集》《列宁全集》《毛泽东选集》《斯大林全集》等原文中检索相关段落，再交由大语言模型作答，每条回答都附带可追溯的原文出处。

与直接问通用大模型的区别在于：**回答有据可查**。来源卡片会指明具体是哪部著作的哪一章节，双击即可跳转到原文阅读器对应位置。

## 功能一览

| 功能 | 状态 | 说明 |
|------|------|------|
| 通用问答 | 可用 | 全量著作检索 + LLM 生成，附来源引用 |
| 原文阅读器 | 可用 | 连续阅读原文，支持目录跳转、模糊搜索、来源定位 |
| 深度思考 | 可用 | 使用推理模型，展示完整思考过程 |
| 检索过程可视化 | 可用 | 实时展示问题解构、检索、重排各阶段进度 |
| 对话消息树 | 可用 | 修改提问或重新生成不会丢弃旧回答，可用箭头切换版本 |
| 马哲方法论模式 | 开发中 | 运用唯物辩证法方法论分析现实问题 |
| **联网搜索** | **开发中，暂未启用** | 界面上的开关目前不会生效，请勿依赖 |

## 部署前必读：两个仓库的关系

本项目的数据分成两块，**分别存放在两个仓库**，请按需取用：

| 内容 | 位置 | 体积 | 什么时候需要 |
|------|------|------|--------------|
| 代码 + 预构建索引 | 本仓库（索引通过 Git LFS 分发） | 约 1.2 GB | 必需 |
| 原文 Markdown 语料 | [marxist-classics-markdown](https://github.com/PrismScopes/marxist-classics-markdown) | 约 152 MB | 想用**原文阅读器**时必需 |

为什么要拆开？索引是二进制产物、语料是纯文本，两者更新节奏完全不同；而且不少人只想问答、不需要翻原文，拆开可以少下 152 MB。

**只想问答**：只 clone 本仓库即可，检索和回答都能正常工作（原文片段已存在索引里）。

**还想读原文 / 用来源跳转**：需要额外把语料仓库放到 `ww/` 目录，见下文第三步。

## 快速开始

### 前置要求

- Docker（推荐）或 Python 3.11+
- [Git LFS](https://git-lfs.com/)（必须，否则拉下来的索引是几百字节的指针文件而非真实数据）
- 一个 DeepSeek API Key，以及一个提供 embedding / rerank 的 API Key

### 1. 克隆仓库

```bash
git lfs install
git clone https://github.com/PrismScopes/MarxLen.git
cd MarxLen
git lfs pull
```

拉完后确认索引是真实文件而不是 LFS 指针：

```bash
# Linux / macOS
ls -lh rag/faiss_index.idx    # 应约 637 MB

# Windows PowerShell
(Get-Item rag/faiss_index.idx).Length / 1MB
```

若只有几百字节，说明 LFS 没生效，重新执行 `git lfs install && git lfs pull`。

### 2. 配置 API Key

```bash
cp rag/.env.example rag/.env
```

编辑 `rag/.env`：

| 变量 | 用途 | 去哪申请 |
|------|------|----------|
| `DEEPSEEK_API_KEY` | 生成回答 | [DeepSeek 开放平台](https://platform.deepseek.com/) |
| `EMBED_API_KEY` | 向量检索与重排序 | 任意兼容 OpenAI 格式的服务（如硅基流动） |

`DEEPSEEK_API_BASE_URL` 与 `EMBED_API_BASE_URL` 也要改成对应服务商的地址。其余项留空即用默认值。

### 3. 下载原文语料（可选，但推荐）

不做这一步，问答一切正常，只是**原文阅读器打不开、来源卡片无法跳转**。

仓库里有一个空的 `ww/` 目录（内含 `ww/README.md` 说明），语料就放在这里：

```bash
git clone https://github.com/PrismScopes/marxist-classics-markdown.git ww-tmp
mv ww-tmp/*.md ww/
rm -rf ww-tmp
```

Windows PowerShell：

```powershell
git clone https://github.com/PrismScopes/marxist-classics-markdown.git ww-tmp
Move-Item ww-tmp\*.md ww\
Remove-Item -Recurse -Force ww-tmp
```

完成后目录形如：

```
MarxLen/
├── ww/
│   ├── 马克思恩格斯全集01上.md
│   ├── 列宁全集第01卷.md
│   └── ...
├── rag/
└── api/
```

想放到别处，可在设置页把 `reader_corpus_dir` 改成你的路径。

### 4. 启动

**Docker（推荐）**

```bash
# 首次启动前，先创建运行时数据库文件（空文件即可，SQLite 会自动建表）
touch rag/cache_embeddings.db rag/cache_answers.db api/conversations.db
# Windows PowerShell:
# New-Item -ItemType File rag/cache_embeddings.db,rag/cache_answers.db,api/conversations.db

docker compose up -d
```

**不用 Docker**

```bash
cd rag && uv sync && cd ..

# Linux / macOS
rag/.venv/bin/python -m api.main
# Windows
rag/.venv/Scripts/python -m api.main
```

打开 http://localhost:8000

## 使用说明

### 提问

在输入框输入问题回车即可。提问时你会依次看到检索的各个阶段（问题解构、检索、重排、生成），而不是干等。

### 来源卡片与原文跳转

回答下方列出本次引用的文献。**双击来源卡片**会打开原文阅读器并定位到对应段落。定位依据是原文的空白归一化匹配，实测命中率 99.5%；极少数定位不到时会退回到章节开头。

### 原文阅读器

左侧栏进入阅读器，可以：

- 按书籍浏览，左侧目录跳转章节
- 在书内做**模糊搜索**（输入大意即可，不必是原文原句）
- 从问答的来源卡片直接跳进来

阅读器读的是 `ww/` 下的 Markdown 原文，不是索引。**你在运行期间直接编辑原文文件，刷新页面即可看到改动**，无需重启服务。

### 对话消息树

修改已发送的提问、或对回答点重新生成时，旧版本**不会被丢弃**。消息上方会出现 `< 2/2 >` 形式的切换器，可随时翻回之前的版本。同一个对话里的所有分支都在这一个对话内，不会产生新的对话窗口。

### 深度思考

开启后使用推理模型，可展开查看模型的完整推理过程。思考过程由 API 单独返回，不占用正文。

### 联网搜索（暂未启用）

界面上有联网搜索开关，但**该功能仍在开发中，尚未接入正式流程，打开也不会生效**。请不要依赖它，后续版本会正式启用。

## 配置

大部分参数在设置页面直接改，即时生效，会写入根目录 `config.json`（该文件不入库，属于你的本地状态）。

需要写在 `rag/.env` 里的只有以下几项：

| 变量 | 默认值 | 说明 |
|------|--------|------|
| `DEEPSEEK_API_BASE_URL` | `https://api.deepseek.com/v1` | 生成模型服务地址 |
| `DEEPSEEK_API_KEY` | — | 生成模型密钥 |
| `DEEPSEEK_MODEL` | `deepseek-chat` | 默认模型 ID |
| `DEEPSEEK_MODEL_LIST` | — | 设置页下拉可选模型，格式 `id1:显示名1,id2:显示名2` |
| `EMBED_API_BASE_URL` | — | embedding / rerank 服务地址 |
| `EMBED_API_KEY` | — | embedding / rerank 密钥 |
| `EMBED_MODEL` | `Qwen/Qwen3-Embedding-0.6B` | 向量模型，**不要改** |
| `RERANK_MODEL` | `Qwen/Qwen3-Reranker-4B` | 重排序模型，可换 |
| `CORS_ALLOW_ORIGINS` | 本地两个地址 | 前端独立部署到别的域名时才需要配 |

`EMBED_MODEL` 必须保持默认值。仓库提供的索引是用这个模型生成的，换成别的模型后向量维度与语义空间都不一致，检索结果会完全错乱。你的 embedding 服务商需要支持该模型。

## 提示词自定义

`Prompt/` 下两份提示词可直接编辑，重启服务生效：

- `main_prompt.txt` — AI 的思考方法与输出规范。「输入数据契约」章节声明它从检索模块接收哪些字段；「输出要求」章节的引用格式被前端依赖，改动需谨慎。
- `RAG_prompt.txt` — 检索前的问题解构流程。末尾的 JSON 输出格式由程序解析，字段名不能改。

文件缺失时会回退到内置精简提示词，服务不会崩，但检索质量会下降。

## 关于索引

仓库通过 Git LFS 直接提供预构建索引，开箱即用，无需自己跑向量化：

- `rag/documents.db` — 文档库，约 282 MB
- `rag/faiss_index.idx` — 向量索引，约 637 MB
- `rag/bm25_index.pkl` — 关键词索引，约 246 MB

三者是配套的：`documents.db` 的主键即 FAISS 的向量 ID，BM25 也按同一顺序对齐，**不要单独替换其中任何一个**，否则来源会整体错位。

建库与向量化脚本不随仓库分发。

## 项目结构

```
api/                后端服务（FastAPI + SSE 流式接口）
  reader.py         原文阅读器与模糊搜索
  conversation_store.py  对话消息树存储
rag/                检索与问答引擎
  retriever.py      混合检索（向量 + BM25 并行，RRF 融合）
  generator.py      回答生成与流式输出
  query_planner.py  问题解构与检索计划
Prompt/             提示词（可独立编辑，改动无需改代码）
marxist-rag-ui/     前端页面
tests/              测试
ww/                 原文语料（需另行下载，不在本仓库）
```

## 技术实现

检索采用向量与关键词双通道并行，再用 RRF 融合、重排序模型精排：

- 向量检索：FAISS `IndexIDMap`，向量 ID 直接对应 SQLite 主键
- 关键词检索：BM25 + jieba 分词
- 两路检索并行执行，融合顺序固定以保证结果可复现
- 嵌入与回答均有缓存

## 测试

```bash
# 纯逻辑测试，秒级完成，不消耗 API 额度
rag/.venv/Scripts/python tests/test_unit.py
rag/.venv/Scripts/python tests/test_cache.py
rag/.venv/Scripts/python tests/test_config.py

# 需要真实 API，耗时较长
rag/.venv/Scripts/python tests/test_integration.py
rag/.venv/Scripts/python tests/test_api.py
rag/.venv/Scripts/python tests/test_degrade.py
```

## 常见问题

**索引文件只有几百字节？**
Git LFS 没装或没拉。执行 `git lfs install && git lfs pull`。

**阅读器提示原文目录不存在？**
没有下载语料仓库，见「快速开始」第三步。Docker 部署还需确认 `ww` 已挂载进容器。

**启动很慢？**
需加载 637 MB 的 FAISS 索引和 245 MB 的 BM25 索引，首次启动约一分半属正常。

**回答里的来源点了没反应？**
需要双击。若仍无反应，多半是没下载语料。

## License

MIT

语料版权归原出版方所有，仅供学习研究使用。
