# -*- coding: utf-8 -*-
"""
kb —— MarxLen 离线数据工程与知识库活水更新工具包

设计原则（与在线服务解耦，构建产物永不写入 rag/ 运行目录）：

1. 内容寻址（CAS）：chunk 身份由文本哈希确定，同文同 ID，
   增量 diff 与跨构建复用都建立在这个不变式上。
2. 不可变构建：每次构建产出到 data/builds/<build_id>/ 的独立目录，
   现有 rag/ 下的索引文件（v1）只读不写。
3. 原子发布：上线只是修改 data/releases.json 指针，旧版本物理保留，
   任何时刻可回滚。
4. 门禁说话：一致性校验 + golden 集评估通过才允许发布。
5. 断点续跑：嵌入结果按文本哈希缓存，崩溃后重跑不重复调用 API。

本包内部依赖方向约定：
  kb.release / kb.manifest / kb.cas / kb.paths —— 不 import rag，
    可被在线端（api/、rag/generator.py）安全引用。
  kb.builder / kb.verify / kb.eval —— 允许 import rag，
    仅在离线 CLI 进程中运行。
"""
__version__ = "0.1.0"
