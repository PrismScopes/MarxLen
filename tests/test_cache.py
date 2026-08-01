"""回答缓存测试：正文与来源必须配套，且兼容旧版无 sources 列的库"""
import os
import sys
import sqlite3
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

FAIL = []
def check(name, cond, detail=""):
    print(f"{'PASS' if cond else 'FAIL'}  {name}" + (f"  <- {detail}" if not cond and detail else ""))
    if not cond:
        FAIL.append(name)

from rag.cache_store import AnswerCache

tmp = tempfile.mkdtemp()

# ============ 1. 正文与来源往返 ============
db1 = os.path.join(tmp, "c1.db")
c = AnswerCache(db_path=db1)
srcs = [{"title": "资本论 > 商品", "author": "马恩全集23", "chapter": "第一章",
         "score": 0.97, "excerpt": "商品是...", "source_url": ""}]
c.put("问题A", "回答A", srcs)

got = c.get("问题A")
check("cache/命中返回元组", isinstance(got, tuple) and len(got) == 2, str(type(got)))
ans, s = got
check("cache/正文正确", ans == "回答A")
check("cache/来源条数正确", len(s) == 1, str(len(s)))
check("cache/来源字段完整", s[0]["title"] == "资本论 > 商品" and s[0]["author"] == "马恩全集23")
check("cache/score保留", abs(s[0]["score"] - 0.97) < 1e-6)
check("cache/未命中返回None", c.get("不存在的问题") is None)

# 无来源时也要能正常存取
c.put("问题B", "回答B", [])
ans2, s2 = c.get("问题B")
check("cache/空来源可存取", ans2 == "回答B" and s2 == [])

# sources 省略时默认空列表
c.put("问题C", "回答C")
ans3, s3 = c.get("问题C")
check("cache/省略sources参数", ans3 == "回答C" and s3 == [])
c.close()

# ============ 2. 旧版库迁移（无 sources 列）============
db2 = os.path.join(tmp, "old.db")
conn = sqlite3.connect(db2)
conn.execute("""CREATE TABLE answer_cache (
    query_hash TEXT PRIMARY KEY, query_text TEXT NOT NULL,
    answer TEXT NOT NULL, created_at REAL NOT NULL)""")
import hashlib
qh = hashlib.md5("旧问题".encode("utf-8")[:500]).hexdigest()
conn.execute("INSERT INTO answer_cache VALUES (?,?,?,?)", (qh, "旧问题", "旧回答", 0.0))
conn.commit()
conn.close()

c2 = AnswerCache(db_path=db2)   # 触发 ALTER TABLE 迁移
cols = {r[1] for r in c2._conn.execute("PRAGMA table_info(answer_cache)")}
check("migrate/已加sources列", "sources" in cols, str(cols))
old = c2.get("旧问题")
check("migrate/旧记录仍可读", old is not None and old[0] == "旧回答", str(old))
check("migrate/旧记录来源为空列表", old and old[1] == [], str(old[1] if old else None))
# 迁移后新写入正常
c2.put("新问题", "新回答", srcs)
check("migrate/迁移后可写新记录", c2.get("新问题")[1][0]["title"] == "资本论 > 商品")
c2.close()

# ============ 3. 损坏的 sources JSON 不应影响正文 ============
db3 = os.path.join(tmp, "bad.db")
c3 = AnswerCache(db_path=db3)
c3.put("坏问题", "好回答", srcs)
qh3 = hashlib.md5("坏问题".encode("utf-8")[:500]).hexdigest()
c3._conn.execute("UPDATE answer_cache SET sources='{不是JSON' WHERE query_hash=?", (qh3,))
c3._conn.commit()
bad = c3.get("坏问题")
check("robust/坏JSON仍返回正文", bad is not None and bad[0] == "好回答")
check("robust/坏JSON来源降级为空", bad and bad[1] == [])

# sources 存成非列表（如对象）时也应降级
c3._conn.execute("UPDATE answer_cache SET sources='{\"a\":1}' WHERE query_hash=?", (qh3,))
c3._conn.commit()
check("robust/非列表来源降级为空", c3.get("坏问题")[1] == [])
c3.close()

print("\n" + "=" * 50)
print(f"失败 {len(FAIL)} 项: {FAIL}" if FAIL else "全部通过")
sys.exit(1 if FAIL else 0)
