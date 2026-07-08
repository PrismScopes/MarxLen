<p align="center">
  <h1 align="center">MarxLen · 马列通</h1>
  <p align="center">基于人工智能的马列经典著作智能问答系统</p>
  <p align="center">
    <a href="https://github.com/PrismScopes/MarxLen">GitHub</a>
  </p>
</p>

## 📖 这是什么？

MarxLen（马列通）是一个**马列经典著作智能问答系统**。你可以用自然语言提问，系统会从《马克思恩格斯全集》《列宁全集》《毛泽东选集》等经典著作中检索相关内容，结合大语言模型给出有依据的回答。

**它能做什么？**

- 📚 基于马列经典著作原文回答你的问题
- 🔍 自动定位相关著作章节并标注来源
- 🧠 支持深度思考模式（DeepSeek-R1），展示推理过程
- 🌐 支持联网搜索，补充最新资料
- 💬 流式输出、对话历史管理

**三大问答模式：**

| 模式 | 说明 | 状态 |
|------|------|------|
| 通用问答 | 基于全量著作检索 + LLM 回答 | ✅ 已实现 |
| 马哲方法论 | 运用马克思主义哲学方法论分析问题 | 🚧 开发中 |
| 原文查询 | 精确定位并展示经典著作原文段落 | 🚧 开发中 |

## 📚 文档来源

全部语料来源于 [marxist-classics-markdown](https://github.com/PrismScopes/marxist-classics-markdown) 项目，涵盖《马克思恩格斯全集》《列宁全集》《毛泽东选集》《斯大林全集》等马列经典著作的 Markdown 版本。

## 🚀 快速上手

### 1. 启动系统

```bash
git clone https://github.com/PrismScopes/MarxLen.git
cd MarxLen
git lfs pull
```

### 2. 配置 API Key

```bash
cp rag/.env.example rag/.env
```

编辑 `rag/.env`，填入你的 API Key：

| 变量 | 去哪申请 |
|------|----------|
| `DEEPSEEK_API_KEY` | [DeepSeek 开放平台](https://platform.deepseek.com/) |
| `RERANK_API_KEY` | 任意支持 rerank 的 API 服务 |

> `RERANK_API_KEY` 与检索流程相关，可复用兼容 OpenAI 格式的 API Key。

### 3. 一键启动

```bash
docker compose up -d
```

打开浏览器访问 **http://localhost:8000**

### 无 Docker 启动

```bash
cd rag && uv sync
cd .. && rag/.venv/Scripts/python -m api.main
```

## 🖥️ 功能

- **问答对话**：在输入框提问，实时流式输出回答
- **深度思考**：开启后可查看 AI 的推理过程
- **联网搜索**：让 AI 同时检索互联网获取最新信息
- **来源卡片**：每次回答下方显示引用的文献章节
- **对话历史**：左侧栏管理所有历史对话
- **模型选择**：在设置面板切换不同模型

## 🔧 配置项

| 选项 | 默认值 | 说明 |
|------|--------|------|
| `DEEPSEEK_MODEL` | `deepseek-chat` | 使用的模型 ID |
| `DEEPSEEK_MODEL_LIST` | — | 下拉列表可选模型（`id1:显示名1,id2:显示名2`） |
| `RERANK_MODEL` | `Qwen/Qwen3-Reranker-4B` | 重排序模型 |

## 🏗️ 项目结构

```
api/            后端服务
rag/            检索与问答引擎
marxist-rag-ui/ 前端页面
```

## 📄 License

MIT
