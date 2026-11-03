#!/usr/bin/env python3
# -*- coding: utf-8 -*-
r"""生成 Windows 免安装版（自带 Python，目标机器不用装任何东西）。

    python build_win64.py              生成目录版 + zip + exe
    python build_win64.py --no-exe     只生成目录版和 zip（跳过 PyInstaller）
    python build_win64.py --refresh    重新下载 embeddable Python 与依赖

产物：
    _portable/wjx_probe/              目录便携版，160 MB，解压即用
    wjx_probe_portable_win64.zip      上面的压缩包，60 MB
    wjx_probe_win64.exe               单文件 exe，70 MB，首次解压到 _runtime

原理：
    1. 下载 python.org 官方 embeddable 包（11 MB，含解释器与标准库）
    2. 改 python313._pth 让 Lib\site-packages 进 sys.path
    3. 把 offline_deps/*.whl 直接解压到 site-packages（不走 pip，不需要网络）
    4. 复制项目源码到 app\
    5. 打 bundle.zip，PyInstaller 把它塞进 exe，启动器再解压到 _runtime

为什么 exe 要套一层解压：158 MB 解一遍 5~8 秒，每次都解会覆盖用户解析好的
survey.json。所以只解一次、按版本号判断，升级时保留数据文件。

作者：Richard Huang
本项目完全免费，禁止商用和学术造假
"""

import glob
import os
import shutil
import subprocess
import sys
import urllib.request
import zipfile

HERE = os.path.dirname(os.path.abspath(__file__))
EMB = os.path.join(HERE, "_emb")
PY_DIR = os.path.join(EMB, "python")
APP_DIR = os.path.join(EMB, "app")
PORTABLE = os.path.join(HERE, "_portable")
DIST_DIR = os.path.join(PORTABLE, "wjx_probe")
WHEEL_DIR = os.path.join(HERE, "offline_deps")
BUNDLE = os.path.join(HERE, "bundle.zip")
OUT_ZIP = os.path.join(HERE, "wjx_probe_portable_win64.zip")
OUT_EXE = os.path.join(HERE, "wjx_probe_win64.exe")
LAUNCHER = os.path.join(HERE, "exe_launcher.py")

PY_VER = "3.13.12"

SOURCE_FILES = [
    "server.py", "parse_wjx.py", "wjx_autofill.py", "wjx_browser.py",
    "choice_weights.py", "quota.py", "survey_text.py", "humanize.py", "qr_decode.py",
    "console.py", "run.py", "parallel.py", "probe_parallel.py", "survey.json",
    "LICENSE", "NOTICE",
]

SEVEN_ZIP = r"C:\Program Files\7-Zip\7z.exe"

BAT = r"""@echo off
rem 936 = 简体中文；本文件也是 GBK 编码，两者对齐才不会截断中文
chcp 936 >nul
title 问卷星批量作答控制台
cd /d "%~dp0"

echo.
echo   ============================================
echo     问卷星批量作答控制台
echo     作者：Richard Huang
echo     本项目完全免费，禁止商用和学术造假
echo   ============================================
echo.

rem --doctor 查的是运行环境，交给 run.py；其余参数归 server.py
if /i "%~1"=="--doctor" (
  python\python.exe app\run.py doctor
  goto :done
)

python\python.exe app\server.py %*
set "CODE=%errorlevel%"
if not "%CODE%"=="0" (
  echo.
  echo   启动失败，退出码 %CODE%
  echo   把这个窗口截图发给作者即可定位。
  echo.
  pause
)

:done
"""

README_TXT = """问卷星批量作答控制台 —— 便携版
作者：Richard Huang
本项目完全免费，禁止商用和学术造假

【怎么用】
双击 启动.bat，浏览器会自动打开 http://127.0.0.1:8765/

【为什么不用装 Python】
本目录里带着完整的 Python {pyver} 运行时和全部依赖：

  python\\       解释器 + 依赖库（160 MB，已解压好，直接可用）
  app\\          程序本体（服务端、前端、问卷解析、浏览器驱动）
  启动.bat      入口

复制整个目录到任何 Win10/Win11 x64 机器都能跑，U 盘也行。

【换端口】
启动.bat --port 9000

【只做环境自检】
启动.bat --doctor

【常见问题】
1. 浏览器没弹出：手动打开 http://127.0.0.1:8765/
2. 浏览器作答失败：需要系统装有 Edge 或 Chrome（Win10/11 自带 Edge）
3. 杀毒软件报毒：把本目录加入白名单。这是绿色软件的常见误报，
   不放行就改用 wjx_probe_offline.pyz（需要本机已装 Python）。
"""


def ensure_embeddable(refresh=False):
    exe = os.path.join(PY_DIR, "python.exe")
    if os.path.exists(exe) and not refresh:
        print(f"      复用已有解释器 {PY_DIR}")
        return
    url = f"https://www.python.org/ftp/python/{PY_VER}/python-{PY_VER}-embed-amd64.zip"
    os.makedirs(EMB, exist_ok=True)
    tmp = os.path.join(EMB, "py_embed.zip")
    print(f"      下载 {url}")
    urllib.request.urlretrieve(url, tmp)
    with zipfile.ZipFile(tmp) as z:
        z.extractall(PY_DIR)
    os.remove(tmp)
    print(f"      已解压到 {PY_DIR}")


def setup_pth():
    """_pth 决定 sys.path：标准库 zip -> site-packages -> 当前目录 -> app，并启用 site。

    带 ._pth 的 embedded Python 是 isolated 模式：不会把「脚本所在目录」
    加进 sys.path，PYTHONPATH 也整个忽略。app 目录必须显式列出来，
    否则 server.py 拉起的子进程（wjx_browser.py）import 同级模块会
    ModuleNotFoundError。路径相对 ._pth 自己所在的 python/ 目录。
    """
    pth = os.path.join(PY_DIR, "python313._pth")
    with open(pth, "w", encoding="utf-8") as f:
        f.write("python313.zip\nLib\\site-packages\n.\n..\\app\nimport site\n")


def install_wheels(refresh=False):
    """把 wheel 直接解压到 site-packages。不走 pip，也就不需要 pip 和网络。"""
    sp = os.path.join(PY_DIR, "Lib", "site-packages")
    os.makedirs(sp, exist_ok=True)
    wheels = sorted(glob.glob(os.path.join(WHEEL_DIR, "*.whl")))
    if not wheels:
        raise RuntimeError(f"{WHEEL_DIR} 里没有 wheel，先跑 build_single.py --bundle-deps")

    if not refresh and os.path.isdir(os.path.join(sp, "playwright")):
        print(f"      依赖已就位（{len(os.listdir(sp))} 个条目），跳过")
        return

    n = 0
    for w in wheels:
        with zipfile.ZipFile(w) as z:
            for info in z.infolist():
                name = info.filename
                # .data 目录是 pip 安装时才会用的脚本/数据，直接解压没意义
                if name.endswith("/") or ".data/" in name:
                    continue
                z.extract(info, sp)
                n += 1
    print(f"      解压 {len(wheels)} 个 wheel / {n} 个文件")


def copy_sources():
    shutil.rmtree(APP_DIR, ignore_errors=True)
    os.makedirs(APP_DIR, exist_ok=True)
    for f in SOURCE_FILES:
        src = os.path.join(HERE, f)
        if os.path.exists(src):
            shutil.copy2(src, os.path.join(APP_DIR, f))
    shutil.copytree(os.path.join(HERE, "web"), os.path.join(APP_DIR, "web"))
    print(f"      已复制 {len(SOURCE_FILES)} 个文件 + web/")


def write_text_files():
    with open(os.path.join(EMB, "启动.bat"), "w",
              encoding="gbk", errors="replace", newline="\r\n") as f:
        f.write(BAT)
    with open(os.path.join(EMB, "使用说明.txt"), "w",
              encoding="utf-8", newline="\r\n") as f:
        f.write(README_TXT.format(pyver=PY_VER))
    # SFX 用的配置对 exe 版没有意义，删掉免得误会
    cfg = os.path.join(EMB, "config.txt")
    if os.path.exists(cfg):
        os.remove(cfg)


def make_bundle():
    t0 = os.times()
    with zipfile.ZipFile(BUNDLE, "w", zipfile.ZIP_DEFLATED, compresslevel=6) as z:
        for base in ("python", "app"):
            root_dir = os.path.join(EMB, base)
            for root, _dirs, files in os.walk(root_dir):
                for f in files:
                    p = os.path.join(root, f)
                    arc = os.path.relpath(p, EMB).replace("\\", "/")
                    z.write(p, arc)
        for f in ("启动.bat", "使用说明.txt"):
            p = os.path.join(EMB, f)
            if os.path.exists(p):
                z.write(p, f)
    print(f"      bundle.zip {os.path.getsize(BUNDLE) / 1048576:.1f} MB")


def make_portable():
    os.makedirs(PORTABLE, exist_ok=True)
    # 不 rmtree：这棵树有 800 多个文件，整目录删除会触发环境的批量删除保护。
    # 分发包里的文件清单是固定的，直接覆盖复制即可，不会有残留旧模块的问题。
    shutil.copytree(EMB, DIST_DIR, dirs_exist_ok=True,
                    ignore=shutil.ignore_patterns("setup.py", "*.tmp", "*.zip", "__pycache__"))
    total = sum(os.path.getsize(os.path.join(r, f))
                for r, _d, fs in os.walk(DIST_DIR) for f in fs)
    print(f"      {DIST_DIR}  {total / 1048576:.0f} MB")


def make_zip():
    if os.path.exists(OUT_ZIP):
        os.remove(OUT_ZIP)
    if os.path.exists(SEVEN_ZIP):
        subprocess.run([SEVEN_ZIP, "a", "-tzip", "-mx=9", "-mmt=on", "-y",
                        OUT_ZIP, os.path.join(PORTABLE, "wjx_probe")],
                       check=True, stdout=subprocess.DEVNULL)
    else:
        with zipfile.ZipFile(OUT_ZIP, "w", zipfile.ZIP_DEFLATED, compresslevel=9) as z:
            for root, _dirs, files in os.walk(DIST_DIR):
                for f in files:
                    p = os.path.join(root, f)
                    arc = os.path.join("wjx_probe", os.path.relpath(p, DIST_DIR))
                    z.write(p, arc.replace("\\", "/"))
    print(f"      {os.path.basename(OUT_ZIP)}  {os.path.getsize(OUT_ZIP) / 1048576:.1f} MB")


def make_exe():
    if not os.path.exists(LAUNCHER):
        raise RuntimeError(f"缺少 {LAUNCHER}")
    if os.path.exists(OUT_EXE):
        os.remove(OUT_EXE)
    work = os.path.join(HERE, "_pyi")
    os.makedirs(work, exist_ok=True)
    # 临时目录交给 PyInstaller 的 --clean 清理，别在这里 rmtree
    cmd = [sys.executable, "-m", "PyInstaller",
           "--onefile", "--console", "--noconfirm", "--clean",
           "--name", "wjx_probe_win64",
           "--add-data", f"{BUNDLE};.",
           "--distpath", HERE,
           "--workpath", work,
           "--specpath", work,
           LAUNCHER]
    r = subprocess.run(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.STDOUT)
    if r.returncode != 0 or not os.path.exists(OUT_EXE):
        raise RuntimeError("PyInstaller 打包失败")
    print(f"      {os.path.basename(OUT_EXE)}  {os.path.getsize(OUT_EXE) / 1048576:.1f} MB")


def main():
    no_exe = "--no-exe" in sys.argv
    refresh = "--refresh" in sys.argv

    print("[1/6] 准备 embeddable Python")
    ensure_embeddable(refresh)
    setup_pth()

    print("[2/6] 安装依赖（解 wheel，不走 pip）")
    install_wheels(refresh)

    print("[3/6] 复制项目源码")
    copy_sources()

    print("[4/6] 生成启动脚本与说明")
    write_text_files()

    print("[5/6] 打包运行时")
    make_bundle()
    make_portable()
    make_zip()

    print("[6/6] 打包 exe")
    if no_exe:
        print("      按 --no-exe 跳过")
    else:
        make_exe()

    print()
    print("完成。产物：")
    for p in (OUT_EXE, OUT_ZIP):
        if os.path.exists(p):
            print(f"      {os.path.basename(p):<32} {os.path.getsize(p) / 1048576:6.1f} MB")
    print(f"      {DIST_DIR}")
    print()
    print("作者：Richard Huang    本项目完全免费，禁止商用和学术造假")
    return 0


if __name__ == "__main__":
    sys.exit(main())
