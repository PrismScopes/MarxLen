# 原文语料目录

这个目录是**空的**，需要你自己填充——原文语料没有放在本仓库里。

## 为什么是空的

原文 Markdown 单独发布在另一个仓库：

**https://github.com/PrismScopes/marxist-classics-markdown**

拆开的原因是索引与语料的更新节奏不同，而且只做问答的用户不需要这 152 MB。

## 不下载会怎样

| 功能 | 不下载语料 |
|------|-----------|
| 提问与回答 | 正常（原文片段已存在索引中） |
| 来源卡片显示 | 正常 |
| **原文阅读器** | **无法使用**，会提示原文目录不存在 |
| **来源卡片双击跳转** | **无法使用** |

## 怎么填

在项目根目录执行：

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

完成后本目录下应有一百多个 `.md` 文件，形如：

```
ww/
├── 马克思恩格斯全集01上.md
├── 列宁全集第01卷.md
├── 毛泽东选集十卷合订本.md
└── ...
```

重启服务（或直接刷新页面）即可使用阅读器。

## 换成别的目录

若不想放在 `ww/`，可在设置页面修改 `reader_corpus_dir` 指向其他路径。

## Docker 部署注意

`docker-compose.yml` 已把本目录以只读方式挂载进容器。语料是宿主机上放好后再启动容器，还是启动后再放，都可以——阅读器每次请求都会检查文件改动，无需重启。
