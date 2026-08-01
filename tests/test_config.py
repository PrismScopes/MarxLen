"""配置中心测试：schema 完整性、类型转换、校验、持久化"""
import os
import sys
import json
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

FAIL = []


def check(name, cond, extra=""):
    if cond:
        print(f"PASS  {name}")
    else:
        FAIL.append(name)
        print(f"FAIL  {name}  {extra}")


from rag.config_store import (
    CONFIG_SCHEMA, ConfigItem, ConfigStore, CATEGORIES,
    get_config, reload_config,
)

# ============ 1. Schema 完整性 ============
keys = [it.key for it in CONFIG_SCHEMA]
check("schema/非空", len(CONFIG_SCHEMA) > 30, f"仅 {len(CONFIG_SCHEMA)} 项")
check("schema/key唯一", len(keys) == len(set(keys)),
      f"重复: {[k for k in keys if keys.count(k) > 1]}")

# 每项必须有 label / description，前端要显示
no_label = [it.key for it in CONFIG_SCHEMA if not it.label]
no_desc = [it.key for it in CONFIG_SCHEMA if not it.description]
check("schema/均有label", not no_label, str(no_label))
check("schema/均有描述", not no_desc, str(no_desc))

# category 必须在已声明的分类里，否则前端无法归组
cat_ids = {c["id"] for c in CATEGORIES}
bad_cat = [it.key for it in CONFIG_SCHEMA if it.category not in cat_ids]
check("schema/分类合法", not bad_cat, str(bad_cat))

# select 类型应给 options；model 例外——其候选项由 .env 的
# DEEPSEEK_MODEL_LIST 在运行时动态提供，故允许 options 为空
_DYNAMIC_OPTIONS = {"model"}
bad_select = [it.key for it in CONFIG_SCHEMA
              if it.type == "select" and not it.options
              and it.key not in _DYNAMIC_OPTIONS]
check("schema/select有选项", not bad_select, str(bad_select))

# number 类型的默认值应落在 min/max 区间内
bad_range = []
for it in CONFIG_SCHEMA:
    if it.type in ("number", "int") and isinstance(it.default, (int, float)):
        if it.min is not None and it.default < it.min:
            bad_range.append(it.key)
        if it.max is not None and it.default > it.max:
            bad_range.append(it.key)
check("schema/默认值在区间内", not bad_range, str(bad_range))

# ============ 2. 类型转换 ============
ci = ConfigItem(key="t", type="int", default=5, min=1, max=10)
check("cast/int字符串", ci.cast("7") == 7)
check("cast/int越界钳制上限", ci.cast("999") == 10)
check("cast/int越界钳制下限", ci.cast("-5") == 1)
check("cast/int非法回退默认", ci.cast("abc") == 5)

cf = ConfigItem(key="t", type="number", default=0.3, min=0.0, max=2.0)
check("cast/float字符串", abs(cf.cast("0.8") - 0.8) < 1e-9)
check("cast/float钳制", abs(cf.cast("5.0") - 2.0) < 1e-9)

cb = ConfigItem(key="t", type="boolean", default=True)
for truthy in ("true", "True", "1", "yes", "on", True):
    if cb.cast(truthy) is not True:
        check(f"cast/bool真值{truthy}", False)
        break
else:
    check("cast/bool各种真值", True)
for falsy in ("false", "False", "0", "no", "off", False):
    if cb.cast(falsy) is not False:
        check(f"cast/bool假值{falsy}", False)
        break
else:
    check("cast/bool各种假值", True)

cs = ConfigItem(key="t", type="select", default="a", options=["a", "b"])
check("cast/select合法值", cs.cast("b") == "b")
check("cast/select非法回退默认", cs.cast("zzz") == "a")

ct = ConfigItem(key="t", type="text", default="")
check("cast/text去空白", ct.cast("  hi  ") == "hi")
check("cast/text None转空", ct.cast(None) == "")

# ============ 3. 读写与持久化 ============
tmpdir = tempfile.mkdtemp()
cfg_path = os.path.join(tmpdir, "config.json")
store = ConfigStore(config_path=cfg_path)

# 默认值可读
check("store/读默认temperature", store.get("temperature") == 0.3)
check("store/读默认top_k", store.get("top_k") == 8)
check("store/未知key返回None", store.get("__nonexistent__") is None)

# 更新并落盘
res = store.update({"temperature": "0.9", "top_k": "12"})
check("store/更新后生效", store.get("temperature") == 0.9 and store.get("top_k") == 12)
check("store/更新返回全量", "temperature" in res and "top_k" in res)
check("store/已落盘", os.path.exists(cfg_path))

# 只应持久化被改动的项（_comment 是有意写入的文件说明，不计入）
with open(cfg_path, encoding="utf-8") as f:
    saved = json.load(f)
saved_keys = {k for k in saved if not k.startswith("_")}
check("store/仅存改动项", saved_keys == {"temperature", "top_k"}, str(saved_keys))
check("store/含说明注释", "_comment" in saved)

# 新实例读取，验证持久化真的生效
store2 = ConfigStore(config_path=cfg_path)
check("store/重载后保留", store2.get("temperature") == 0.9 and store2.get("top_k") == 12)

# 非法值应被拒绝或钳制，而不是写坏配置
store2.update({"temperature": "abc"})
check("store/非法值回退默认", store2.get("temperature") == 0.3)

# 未知 key 应被忽略，不污染配置文件
r = store2.update({"__evil__": "x"})
check("store/忽略未知key", "__evil__" not in r)

# 重置单项
store2.update({"top_k": "20"})
store2.reset("top_k")
check("store/重置单项回默认", store2.get("top_k") == 8)

# 全部重置
store2.update({"temperature": "1.5", "top_k": "25"})
store2.reset()
check("store/重置全部", store2.get("temperature") == 0.3 and store2.get("top_k") == 8)

# ============ 4. 前端渲染元数据 ============
schema = store.get_schema()
check("api/schema含分类", "categories" in schema and len(schema["categories"]) > 0)
check("api/schema含项目", "items" in schema and len(schema["items"]) > 30)

item0 = schema["items"][0]
for f in ("key", "label", "type", "value", "default", "description", "category"):
    if f not in item0:
        check(f"api/item含{f}", False, str(item0.keys()))
        break
else:
    check("api/item字段完整", True)

# 敏感项必须标记，前端才知道要打码
secret_items = [it for it in schema["items"] if it.get("secret")]
check("api/有敏感项标记", len(secret_items) > 0)
# 敏感项的值不应原样下发
check("api/敏感值已遮蔽",
      all(it["value"] == "" or "*" in str(it["value"]) for it in secret_items),
      str([(it["key"], it["value"]) for it in secret_items]))

# 需要重启才生效的项要标出来，前端好提示用户
restart_items = [it for it in schema["items"] if it.get("requires_restart")]
check("api/有重启标记项", len(restart_items) > 0)

# ============ 5. 全局单例 ============
g1 = get_config()
g2 = get_config()
check("global/单例一致", g1 is g2)
check("global/可reload", reload_config() is not None)

print("\n" + "=" * 50)
if FAIL:
    print(f"失败 {len(FAIL)} 项: {FAIL}")
    sys.exit(1)
print(f"全部通过（共 {len(CONFIG_SCHEMA)} 个配置项）")
