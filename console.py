#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""控制台编码统一。

问题出在哪：Windows 上 Python 的 stdout 编码跟随系统 locale——
中文系统是 cp936，英文系统是 cp437。而父进程（server.py）按 UTF-8 读子进程输出，
两边对不上，日志里就是 `锟斤拷` 或者一串 `�`。

为什么不靠 chcp：chcp 只改控制台回显的代码页，改不了管道里的字节；
而且 chcp 65001 会让一部分老程序的中文输出直接坏掉。改 Python 自己的编码更可控。

用法：每个能被单独执行的入口，在 main() 第一行调用 force_utf8()。
同时把 PYTHONIOENCODING 写进环境变量，让它启动的子进程也一致，
不用每个子进程各自再猜一次。
"""

import os
import sys


def force_utf8():
    """把 stdout/stderr 固定成 UTF-8，并让子进程继承同样的设置。"""
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except Exception:
            # 不是 TextIOWrapper（比如被重定向成别的对象）时跳过，不影响主流程
            pass
    os.environ["PYTHONIOENCODING"] = "utf-8"


if __name__ == "__main__":
    force_utf8()
    print("控制台编码测试：中文 / 标点，。！？ / 生僻字 龘")
    print("stdout encoding =", getattr(sys.stdout, "encoding", "?"))
