# -*- coding: utf-8 -*-
"""支持 `python -m kb` 运行"""
import sys

from .cli import main

if __name__ == "__main__":
    sys.exit(main())
