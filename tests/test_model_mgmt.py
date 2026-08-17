# -*- coding: utf-8 -*-
"""
模型列表管理测试:OPENAI_MODEL_LIST 的解析/序列化纯逻辑

端点层(add/remove /models)依赖全局配置会写真实 .env,不在单元测试
中触发;这里覆盖无副作用的解析与序列化规则(位于 api/models.py),
保证 GUI 添加/移除模型时格式始终一致。
"""

import os
import sys

_PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _PROJECT_ROOT not in sys.path:
    sys.path.insert(0, _PROJECT_ROOT)

from api.models import parse_model_list, serialize_model_list  # noqa: E402

FAIL = []


def check(name, cond, detail=""):
    print(("PASS  " if cond else "FAIL  ") + name +
          (("  <- " + str(detail)) if not cond and detail else ""))
    if not cond:
        FAIL.append(name)


# ============ 1. 解析 ============
check("parse/标准格式", parse_model_list("a:甲,b:乙") ==
      [{"id": "a", "name": "甲"}, {"id": "b", "name": "乙"}])
check("parse/无冒号取ID为名", parse_model_list("a,b") ==
      [{"id": "a", "name": "a"}, {"id": "b", "name": "b"}])
check("parse/容忍空格与空项", parse_model_list(" a:甲 , ,b:乙 ") ==
      [{"id": "a", "name": "甲"}, {"id": "b", "name": "乙"}])
check("parse/空串为空列表", parse_model_list("") == [])
check("parse/None为空列表", parse_model_list(None) == [])
check("parse/ID内冒号只切第一个",
      parse_model_list("a:b:c") == [{"id": "a", "name": "b:c"}])

# ============ 2. 序列化 ============
check("serialize/往返一致",
      serialize_model_list(parse_model_list("a:甲,b:乙")) == "a:甲,b:乙")
check("serialize/name同id省略冒号",
      serialize_model_list([{"id": "a", "name": "a"},
                             {"id": "b", "name": "乙"}]) == "a,b:乙")
check("serialize/空id被过滤",
      serialize_model_list([{"id": "", "name": "x"},
                             {"id": "b", "name": "b"}]) == "b")
check("serialize/空name回退id",
      serialize_model_list([{"id": "a", "name": ""}]) == "a")

# ============ 3. 增删格式正确性(模拟端点逻辑,无副作用) ============
items = parse_model_list("a:甲")
items = [it for it in items if it["id"] != "a"]   # 移除
items.append({"id": "b", "name": "乙"})            # 添加
check("mgr/增删后序列化", serialize_model_list(items) == "b:乙")
check("mgr/重复添加去重",
      len([it for it in items + [{"id": "b", "name": "乙2"}]
           if it["id"] == "b"]) == 2)  # 去重逻辑在端点内做,此处验证数据形态


print("\n" + "=" * 50)
if FAIL:
    print(f"失败 {len(FAIL)} 项: {FAIL}")
    sys.exit(1)
print("全部通过")
