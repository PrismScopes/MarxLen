# -*- coding: utf-8 -*-
"""
BM25 分词清洗工具

BM25 索引与查询必须使用同一套分词逻辑，否则查询词与索引词无法匹配。
本模块集中定义分词与乱码过滤规则，建库端与检索端 (retriever.py) 共用，
避免两处规则漂移。仓库提供的预构建索引即由这套规则切分而成，
因此本文件的规则不可随意修改——改了会导致查询分词与既有索引对不上。


策略：
  - HTML 标签在分词前整体移除（标签本身无检索价值，只保留标签间的正文）
  - markdown 图片引用整体删除（图片文件本体不存在，纯噪声）；
    markdown 链接 "[文字](说明)" 转为文字（说明多为注记编号，无检索价值）
  - LaTeX 公式块不做整体剥离（保留公式原文），只通过 token 级过滤清掉
    纯符号、单字母、LaTeX 命令词等乱码，公式中有意义的词仍可参与检索
"""

import re
import jieba

# HTML 标签：<td rowspan=1 colspan=1> 等整段移除，只保留标签之间的正文。
# 源文档中混有网页拷贝来的表格，jieba 会把标签拆成 <、td、rowspan、colspan 等噪声 token。
_HTML_TAG = re.compile(r"<[^>]*>")

# 分块截断导致的残缺 HTML 标签：如 "<td rowspan=2 colsp"（缺右尖括号），
# 完整标签正则匹配不到，需单独清理。限定标签名，避免误删正文中的 "<"。
_HTML_TAG_BROKEN = re.compile(r"<(?:td|tr|th|table|thead|tbody|div|span)[^>\n]{0,60}", re.IGNORECASE)

# 标签前缀也被切走时的残留属性：如 "d rowspan=1 colspan=1>100.0"（原 <td rowspan=1 colspan=1>100.0，
# 被截断只剩 "rowspan=1 colspan=1>"），删除属性残留、保留数值正文。
_HTML_ATTR_BROKEN = re.compile(r"(?:rowspan|colspan)\s*=\s*[\"']?\d+[\"']?\s*>?")

# markdown 图片引用：![](images/xxx.jpg)。
# 路径一律以 images/ 开头；因长文本分块时右括号可能被截断，
# 故把右括号设为可选（兼容 "![](images/xxx.jpg" 缺右括号的残缺形态）。
_MARKDOWN_IMAGE = re.compile(r"!\[[^\]]*\]\(images/[^)\s]*\)?")

# 残缺图片引用的另一形态：分块截断导致 "![ " 前缀丢失，如 "](images/xxx.jpg)"。
_MARKDOWN_IMAGE_TAIL = re.compile(r"\]\(images/[^)\s]*\)?")

# 分块截断后孤立残留的图片文件名：长 hex + 图片扩展名（如 "607e7d...caf.jpg"），
# 前面的 "![](images/" 已被切到上一个分块，这里只删文件名本身。
_IMAGE_FILENAME = re.compile(r"[0-9a-f]{20,}\.(?:jpg|jpeg|png|gif|webp)\)?")

# 分块切剩的孤立 "images/" 残留（前面的 "![](" 前缀丢失）
_MARKDOWN_IMAGE_ORPHAN = re.compile(r"images/[^\s)]*")

# 分块切剩的短文件名碎片（如 "5.jpg)"、"1b78aea.jpg)"，前缀被切走只剩尾部）
_IMAGE_FILENAME_TAIL = re.compile(r"(?:^|\s)[0-9a-f]{1,40}\.jpg\)?")

# markdown 链接：[文字](说明)，保留"文字"、丢弃"说明"。
# 源文档中的此类链接实际是注记（如 "[1](一九一五年—一九一六年)"），
# 说明部分多为编号与日期，保留文字即可。
_MARKDOWN_LINK = re.compile(r"\[([^\]]*)\]\([^)]*\)")

# LaTeX 命令单词黑名单。
# jieba 会把 "\begin" 拆成 "\" + "begin" 两个 token，这些英文单词
# 是公式命令而非正文内容，检索时没有任何意义。
_LATEX_COMMANDS = {
    "begin", "end", "array", "mathrm", "mathbf", "mathcal", "mathbb",
    "operatorname", "overrightarrow", "rightarrow", "leftarrow", "uparrow",
    "downarrow", "leftrightarrow", "Rightarrow", "Leftrightarrow", "equiv",
    "approx", "ne", "leq", "geq", "ll", "gg", "sim", "cong", "propto",
    "lambda", "Delta", "Omega", "Sigma", "alpha", "beta", "gamma", "delta",
    "varepsilon", "theta", "iota", "kappa", "mu", "xi", "pi", "rho",
    "sigma", "phi", "psi", "omega", "Gamma", "Lambda", "frac", "sqrt", "sum",
    "prod", "int", "partial", "nabla", "infty", "times", "cdot", "qquad",
    "quad", "text", "textrm", "textbf", "textit", "emph", "label", "ref",
    "cite", "section", "subsection", "item", "left", "right", "big", "Big",
    "bigg", "Bigg", "limits", "displaystyle", "ldots", "cdots", "vdots",
    "dots", "underline", "overbrace", "underbrace", "hat", "bar", "tilde",
    "vec", "dot", "ddot", "caption", "table", "figure", "centering",
}

# HTML 属性词黑名单（兜底：标签被部分移除后残留的单词）
_HTML_WORDS = {
    "td", "tr", "table", "rowspan", "colspan", "html", "div", "span",
    "tbody", "thead", "style", "class", "align", "width", "height",
    "border", "cellpadding", "cellspacing", "valign", "bgcolor", "br",
    # 图片路径/扩展名残留（分块截断后可能残余 "images"、"jpg" 独立 token）
    "images", "jpg", "jpeg", "png", "gif", "webp",
}


def strip_html(text: str) -> str:
    """移除文本中的 HTML 标签，只保留标签之间的正文"""
    text = _HTML_TAG.sub(" ", text)
    text = _HTML_TAG_BROKEN.sub(" ", text)
    text = _HTML_ATTR_BROKEN.sub(" ", text)
    return text


def strip_markdown(text: str) -> str:
    """清洗 markdown 图片引用与链接

    图片引用 "![](images/xxx.jpg)"（含分块截断的残缺形态）整体删除；
    链接 "[文字](说明)" 转为文字。
    """
    text = _MARKDOWN_IMAGE.sub(" ", text)
    text = _MARKDOWN_IMAGE_TAIL.sub(" ", text)
    text = _IMAGE_FILENAME.sub(" ", text)
    text = _MARKDOWN_IMAGE_ORPHAN.sub(" ", text)
    text = _IMAGE_FILENAME_TAIL.sub(" ", text)
    text = _MARKDOWN_LINK.sub(r"\1", text)
    return text


def tokenize_bm25(text: str) -> list:
    """BM25 分词：jieba 搜索模式分词 + 乱码过滤

    处理流程:
      0. 移除 HTML 标签、markdown 图片引用与链接（LaTeX 公式块保留原文）
      1. 空白 token
      2. 纯标点/符号 token，如 '$'、'\\'、'{'、'→'、'↓' 等（不含任何字母/数字/汉字）
      3. 单个英文字母（公式里的 i/E 等无检索意义；中文单字与数字保留）
      4. LaTeX 命令单词与 HTML 属性词（begin/array/mathrm、td/rowspan 等）

    说明: 该函数同时用于建库与查询。仓库自带的 BM25 索引已按当前规则切分，
          修改规则会使查询分词与索引不一致，导致关键词检索失效。
    """
    text = strip_html(text)
    text = strip_markdown(text)
    tokens = []
    for tok in jieba.cut_for_search(text):
        tok = tok.strip()
        if not tok:
            continue
        # 单个英文字母：公式残留，无检索价值
        if len(tok) == 1 and tok.isascii() and tok.isalpha():
            continue
        # 纯符号：无字母/数字/汉字，如 '$$'、'\\'、'→'、'↓'、'......'
        if not any(ch.isalnum() or "\u4e00" <= ch <= "\u9fff" for ch in tok):
            continue
        # LaTeX 命令单词
        if tok in _LATEX_COMMANDS:
            continue
        # HTML 属性词
        if tok in _HTML_WORDS:
            continue
        tokens.append(tok)
    return tokens
