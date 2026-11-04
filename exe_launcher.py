#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Windows 单文件启动器。

PyInstaller 把 bundle.zip（内置 Python 运行时 + 依赖 + 项目）打进来，这个脚本负责：

  1. 首次运行时把 bundle.zip 解压到 exe 同目录的 _runtime/
  2. 之后启动只校验版本号，跳过解压
  3. 用 _runtime/python/python.exe 拉起 _runtime/app/server.py

为什么不在每次启动都解压：158 MB 解一遍要 5~8 秒，而且会覆盖用户解析好的
survey.json。所以解一次、记版本号，升级时只覆盖代码并把数据文件放回去。

参数：
    --port 9000     换端口
    --no-browser    不自动开浏览器
    --doctor        只做环境自检（转给 run.py doctor）
    --rebuild       删掉版本标记，强制重解压一次

作者：Richard Huang
本项目完全免费，禁止商用和学术造假
"""

import os
import subprocess
import sys
import zipfile

APP_VERSION = "1.4.2"

BANNER = """
  ============================================
    问卷星批量作答控制台
    作者：Richard Huang
    本项目完全免费，禁止商用和学术造假
  ============================================
"""

# 升级时不能被覆盖的用户数据
KEEP_FILES = ("survey.json", "fill_template.json", "choice_weights.json")


def base_dir():
    """exe 所在目录。onefile 下 __file__ 在临时目录，必须用 executable。"""
    if getattr(sys, "frozen", False):
        return os.path.dirname(os.path.abspath(sys.executable))
    return os.path.dirname(os.path.abspath(__file__))


def meipass():
    """PyInstaller 解自身用的临时目录；源码运行时就是脚本目录。"""
    return getattr(sys, "_MEIPASS", base_dir())


def say(msg=""):
    # 解压期间十几秒没有输出会让人以为卡死，强制逐行刷出
    print(msg, flush=True)


def pause(msg="  按回车退出..."):
    try:
        input(msg)
    except Exception:
        pass


def read_version(marker):
    try:
        with open(marker, encoding="utf-8") as f:
            return f.read().strip()
    except Exception:
        return ""


def unpack(rt, bundle, marker):
    """解压到运行时目录。已同版本则跳过；升级时保留数据文件。"""
    if read_version(marker) == APP_VERSION:
        return "cached"

    keep = {}
    for name in KEEP_FILES:
        p = os.path.join(rt, "app", name)
        if os.path.exists(p):
            try:
                with open(p, "rb") as f:
                    keep[name] = f.read()
            except Exception:
                pass

    say(f"  首次启动：解压内置 Python 与依赖")
    say(f"    -> {rt}")
    say("  约 160 MB，需要 10 秒左右，请勿关闭窗口...")
    os.makedirs(rt, exist_ok=True)
    with zipfile.ZipFile(bundle) as z:
        total = len(z.infolist())
        for i, info in enumerate(z.infolist(), 1):
            z.extract(info, rt)
            if i % 200 == 0 or i == total:
                say(f"    {i}/{total}")

    for name, data in keep.items():
        try:
            with open(os.path.join(rt, "app", name), "wb") as f:
                f.write(data)
        except Exception:
            pass

    with open(marker, "w", encoding="utf-8") as f:
        f.write(APP_VERSION)
    say("  解压完成" + ("（已保留原有问卷与模板）" if keep else ""))
    return "fresh"


def main():
    for s in (sys.stdout, sys.stderr):
        try:
            s.reconfigure(encoding="utf-8", errors="replace", line_buffering=True)
        except Exception:
            pass

    sys.stdout.write(BANNER)
    sys.stdout.flush()

    base = base_dir()
    rt = os.path.join(base, "_runtime")
    marker = os.path.join(rt, ".version")
    bundle = os.path.join(meipass(), "bundle.zip")

    args = list(sys.argv[1:])

    if "--rebuild" in args:
        args.remove("--rebuild")
        try:
            os.remove(marker)
            say("  已清除版本标记，将重新解压")
        except Exception:
            pass

    if not os.path.exists(bundle):
        say("  包内缺少 bundle.zip，无法启动。请重新下载完整 exe。")
        pause()
        return 1

    try:
        state = unpack(rt, bundle, marker)
    except Exception as e:
        say(f"  解压失败：{type(e).__name__}: {e}")
        say(f"  可以删掉 {rt} 后重试，或改用 wjx_probe_portable_win64.zip")
        pause()
        return 2

    if state == "cached":
        say(f"  运行时已就绪：{rt}")

    py = os.path.join(rt, "python", "python.exe")
    app = os.path.join(rt, "app", "server.py")
    if not os.path.exists(py) or not os.path.exists(app):
        say("  运行时文件不全，加 --rebuild 或删掉 _runtime 目录后重试")
        pause()
        return 3

    # --doctor 是查运行环境的，server.py 不认识，转给 run.py
    if "--doctor" in args:
        args = [a for a in args if a != "--doctor"]
        target = os.path.join(rt, "app", "run.py")
        cmd = [py, target, "doctor"] + args
    else:
        cmd = [py, app] + args
        say()
        say("  启动服务，浏览器会自动打开 http://127.0.0.1:8765/")
        say("  关闭这个窗口即停止服务")
        say()

    try:
        return subprocess.call(cmd, cwd=rt)
    except KeyboardInterrupt:
        return 0
    except Exception as e:
        say(f"  启动失败：{type(e).__name__}: {e}")
        pause()
        return 4


if __name__ == "__main__":
    sys.exit(main())
