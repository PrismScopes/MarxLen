# -*- coding: utf-8 -*-
"""
CI 测试聚合入口 —— 本地与 GitHub Actions 共用

依次运行全部"零依赖测试"(不调用任何外部 API、不烧钱、秒级完成),
任一失败立即计入汇总,最终以非零退出码表达结果:

    python tests/run_all.py

包含:
  test_unit.py        检索融合 / 解构解析 / 维度标签剥离等纯逻辑
  test_cache.py       回答缓存、来源配套与知识库版本隔离
  test_config.py      配置中心 schema 与读写
  test_kb_pipeline.py 离线数据工程全管道(假嵌入,零 API)
  test_fixes.py       可观测性与六项缺陷修复专项
"""

import os
import subprocess
import sys

TESTS = [
    "test_unit.py",
    "test_cache.py",
    "test_config.py",
    "test_kb_pipeline.py",
    "test_fixes.py",
    "test_model_mgmt.py",
]


def main() -> int:
    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    tests_dir = os.path.join(root, "tests")
    python = sys.executable

    failed = []
    for name in TESTS:
        path = os.path.join(tests_dir, name)
        print("\n" + "=" * 70)
        print(f"运行测试: {name}")
        print("=" * 70)
        result = subprocess.run([python, path], cwd=root)
        if result.returncode != 0:
            failed.append(name)
            print(f">>> {name} 失败 (exit={result.returncode})")

    print("\n" + "=" * 70)
    print(f"汇总: 共 {len(TESTS)} 个套件, "
          f"通过 {len(TESTS) - len(failed)} 个, 失败 {len(failed)} 个")
    if failed:
        print("失败套件: " + ", ".join(failed))
        return 1
    print("全部通过")
    return 0


if __name__ == "__main__":
    sys.exit(main())
