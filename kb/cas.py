# -*- coding: utf-8 -*-
"""
内容寻址（Content-Addressable Storage）—— 增量能力的根基

现有 v1 建库脚本用 uuid4() 给 chunk 命名：同一段文字两次入库
会得到两个不同的 ID，增量无从谈起。这里改为确定性身份：

    chunk_id = uuid5(命名空间, sha256(规范化 chunk 文本))

同一份文本永远得到同一个 ID。由此可以安全地：
  - 跨 build 判断"这个 chunk 上次已经嵌过，向量直接复用"
  - 用文本哈希做嵌入缓存键，相同文本全库只嵌入一次（重复的
    "编者说明""注释"等段落由此省掉大量 API 调用）
"""

import hashlib
import uuid

# 稳定的命名空间（RFC 4122 附录示例命名空间即可，只需保证恒定）
_NAMESPACE_CHUNK = uuid.UUID("6ba7b810-9dad-11d1-80b4-00c04fd430c8")


def text_sha(text: str) -> str:
    """文本的 sha256 十六进制串"""
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def chunk_uuid(text: str) -> str:
    """由文本内容确定性地生成 chunk 的 uuid（字符串形式）

    文本在分块时已经 strip 过，同一分块算法 + 同一文本
    必然得到同一 uuid。
    """
    return str(uuid.uuid5(_NAMESPACE_CHUNK, text_sha(text)))


def build_id_slug() -> str:
    """生成一次构建的短标识（时间戳 + 随机段，仅用于人类可读）"""
    import datetime
    import secrets
    stamp = datetime.datetime.now().strftime("%Y%m%d-%H%M%S")
    return "b-%s-%s" % (stamp, secrets.token_hex(3))
