#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""把整个项目打成一个可执行文件。

两种产物，按需要选：

    python build_single.py                  -> wjx_probe_all_in_one.py   约 130 KB
                                               依赖缺失时联网安装
    python build_single.py --bundle-deps    -> wjx_probe_offline.pyz     约 52 MB
                                               依赖也打进去，全程离线

`.py` 用 base64 字符串存数据；`.pyz` 是标准 Python zipapp，数据以二进制直接存，
体积不会因为 base64 膨胀 33%。两者都由 launcher_template.py 驱动，逻辑只有一份。

    --bundle-deps     下载依赖 wheel 并打进包
    --refresh-deps    重新下载（依赖清单变了之后用）
    --deps-dir DIR    wheel 缓存目录，默认 offline_deps

产物：
    wjx_probe_all_in_one.py   轻量版，python 直接跑
    wjx_probe_offline.pyz     离线版，python 直接跑
    启动.bat                  双击入口，自动选上面两个里存在的那个

作者：黄智皓
本项目完全免费，禁止商用和学术造假
"""

import argparse
import base64
import glob
import io
import os
import py_compile
import shutil
import subprocess
import sys
import zipapp
import zipfile

HERE = os.path.dirname(os.path.abspath(__file__))
TEMPLATE = os.path.join(HERE, "launcher_template.py")

OUT_PY = os.path.join(HERE, "wjx_probe_all_in_one.py")
OUT_PYZ = os.path.join(HERE, "wjx_probe_offline.pyz")
OUT_BAT = os.path.join(HERE, "启动.bat")

# 打进包里的代码与静态资源；换新模块时只改这张表
CODE_FILES = [
    "server.py",
    "parse_wjx.py",
    "wjx_autofill.py",
    "wjx_browser.py",
    "choice_weights.py",
    "quota.py",
    "survey_text.py",
    "humanize.py",
    "qr_decode.py",
    "console.py",
    "run.py",
    "parallel.py",
    "probe_parallel.py",
    "web/index.html",
    "web/style.css",
    "web/app.js",
    # 许可证随包分发：PolyForm 的 Notices 条款要求拿到副本的人同时拿到条款
    "LICENSE",
    "NOTICE",
]

# 用户数据：只在运行时目录里不存在时写入
DATA_FILES = ["survey.json"]

CHUNK = 120   # base64 每行长度

WHEEL_SUBDIR = "wheels"   # 包内 wheel 放在这个目录下，与 launcher 的 WHEEL_DIRNAME 对齐

BAT = r"""@echo off
rem 936 = 简体中文；本文件也是 GBK 编码，两者对齐才不会截断中文
chcp 936 >nul
title 问卷星批量作答控制台
cd /d "%~dp0"
setlocal enabledelayedexpansion

echo.
echo   ============================================
echo     问卷星批量作答控制台
echo     作者：黄智皓
echo     本项目完全免费，禁止商用和学术造假
echo   ============================================
echo.

set "PY="

rem ---- 1) 官方 py launcher，装在 C:\Windows，不受 PATH 顺序影响
py -3 -c "import sys" >nul 2>nul
if not errorlevel 1 (
  set "PY=py -3"
  goto :pick
)

rem ---- 2) 逐个实测 PATH 里的 python
rem      Microsoft Store 的 python.exe 是 0 字节占位符，执行即返回 9009。
rem      所以不能只用 where 判断「存在」，必须真跑一次 -c 才知道能不能用。
for /f "delims=" %%P in ('where python 2^>nul') do (
  "%%P" -c "import sys" >nul 2>nul
  if not errorlevel 1 (
    set "PY=%%P"
    goto :pick
  )
)

rem ---- 3) 常见安装目录（官方安装器 + 用户级安装）
for %%D in (
  "%LOCALAPPDATA%\Programs\Python\Python313\python.exe"
  "%LOCALAPPDATA%\Programs\Python\Python312\python.exe"
  "%LOCALAPPDATA%\Programs\Python\Python311\python.exe"
  "%LOCALAPPDATA%\Programs\Python\Python310\python.exe"
  "%LOCALAPPDATA%\Programs\Python\Python39\python.exe"
  "%ProgramFiles%\Python313\python.exe"
  "%ProgramFiles%\Python312\python.exe"
  "%ProgramFiles%\Python311\python.exe"
  "%ProgramFiles%\Python310\python.exe"
  "C:\Python313\python.exe"
  "C:\Python312\python.exe"
  "C:\Python311\python.exe"
  "C:\Python310\python.exe"
) do (
  if exist %%D (
    set "PY=%%D"
    goto :pick
  )
)

rem ---- 4) 注册表里的安装记录（前三种都漏掉时兜底）
for %%R in (HKCU HKLM) do (
  for /f "tokens=2,*" %%A in ('reg query "%%R\Software\Python\PythonCore" /s /v ExecutablePath 2^>nul ^| findstr /i "ExecutablePath"') do (
    if exist "%%B" (
      set "PY=%%B"
      goto :pick
    )
  )
)

:notfound
echo   没有找到可用的 Python。
echo.
echo   这不是工具的问题，是系统里缺解释器，或者装了解释器但没进 PATH。
echo.
echo     1. 装一个（推荐）
echo        https://www.python.org/downloads/
echo        安装第一步务必勾选 "Add python.exe to PATH"，装完重新双击本文件
echo.
echo     2. 已经装了，但从这里找不到
echo        开始菜单搜 "py"，在打开的窗口里执行：
echo          py -3 wjx_probe_offline.pyz
echo.
echo     3. 上面两条都不行
echo        把这个窗口截图发给作者
echo.
pause
exit /b 1

:pick
rem 离线版优先：它把依赖一起带上了，不需要联网
set "TARGET="
if exist "wjx_probe_offline.pyz" set "TARGET=wjx_probe_offline.pyz"
if not defined TARGET if exist "wjx_probe_all_in_one.py" set "TARGET=wjx_probe_all_in_one.py"
if not defined TARGET (
  echo   没找到主程序文件。
  echo   wjx_probe_offline.pyz 或 wjx_probe_all_in_one.py 要和本文件放在同一目录。
  echo.
  pause
  exit /b 1
)

echo   解释器：%PY%
echo   主程序：%TARGET%
echo.
%PY% "%TARGET%" %*
set "CODE=%errorlevel%"
if not "%CODE%"=="0" (
  echo.
  echo   启动失败，退出码 %CODE%
  echo   把上面 20 行输出发给作者即可定位。
  echo.
  pause
)
exit /b %CODE%
"""


# ---------------------------------------------------------------- 依赖清单
def dep_packages():
    """从 launcher_template 读依赖清单，避免两处各维护一份。"""
    sys.path.insert(0, HERE)
    import launcher_template as lt
    pkgs = []
    for _mod, pkg, _why, _must in lt.DEPS:
        if pkg not in pkgs:
            pkgs.append(pkg)
    return pkgs


def ensure_wheels(deps_dir, refresh=False):
    """下载依赖 wheel 到缓存目录。返回 wheel 路径列表。"""
    pkgs = dep_packages()

    if refresh and os.path.isdir(deps_dir):
        print(f"      清空缓存 {deps_dir}")
        shutil.rmtree(deps_dir)

    existing = sorted(glob.glob(os.path.join(deps_dir, "*.whl")))
    if existing:
        names = {os.path.basename(p).split("-")[0].lower().replace("_", "-") for p in existing}
        want = {p.lower().replace("_", "-") for p in pkgs}
        missing = want - names
        if not missing:
            print(f"      复用已有 wheel {len(existing)} 个")
            return existing
        # 缓存和当前清单对不上（比如换了依赖），整目录清掉重下，
        # 否则多余的旧 wheel 会被一起打进包，白占体积
        print(f"      缓存缺 {', '.join(sorted(missing))}，清空重下")
        shutil.rmtree(deps_dir, ignore_errors=True)
        existing = []

    os.makedirs(deps_dir, exist_ok=True)
    print(f"      下载依赖：{' '.join(pkgs)}")
    cmd = [sys.executable, "-m", "pip", "download",
           "-d", deps_dir, "--only-binary", ":all:",
           "--disable-pip-version-check"] + pkgs
    r = subprocess.run(cmd)
    if r.returncode != 0:
        raise RuntimeError(f"pip download 失败，退出码 {r.returncode}")
    return sorted(glob.glob(os.path.join(deps_dir, "*.whl")))


# ---------------------------------------------------------------- 打包
def build_payload(wheels):
    """把源码 + wheel 压成一个 zip。返回 (bytes, 文件清单, 跳过清单)。"""
    buf = io.BytesIO()
    used, skipped = [], []
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED, compresslevel=9) as z:
        for rel in CODE_FILES + DATA_FILES:
            path = os.path.join(HERE, rel.replace("/", os.sep))
            if not os.path.exists(path):
                skipped.append(rel)
                continue
            z.write(path, rel)
            used.append(rel)
        for w in wheels:
            # wheel 本身就是 zip，再压一遍几乎没收益，直接存
            z.write(w, f"{WHEEL_SUBDIR}/{os.path.basename(w)}",
                    compress_type=zipfile.ZIP_STORED)
            used.append(f"{WHEEL_SUBDIR}/{os.path.basename(w)}")
    return buf.getvalue(), used, skipped


def render(template: str, blob_literal: str, wheel_py: str) -> str:
    out = template.replace('"@@BLOB@@"', blob_literal, 1)
    out = out.replace('"@@WHEEL_PY@@"', repr(wheel_py), 1)
    return out


def write_text(path, text):
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8", newline="\n") as f:
        f.write(text)
    os.replace(tmp, path)


def make_pyz(launcher_src: str, payload: bytes, out_path: str):
    """zipapp：外层 zip 里放 __main__.py 和 payload.zip，不经过 base64。"""
    stage = os.path.join(HERE, "_build_pkg")
    shutil.rmtree(stage, ignore_errors=True)
    os.makedirs(stage)
    try:
        with open(os.path.join(stage, "__main__.py"), "w",
                  encoding="utf-8", newline="\n") as f:
            f.write(launcher_src)
        with open(os.path.join(stage, "payload.zip"), "wb") as f:
            f.write(payload)
        if os.path.exists(out_path):
            os.remove(out_path)
        # compressed=False：payload 已是压缩包，外层再压只会拖慢启动
        zipapp.create_archive(stage, out_path, interpreter=None, compressed=False)
    finally:
        shutil.rmtree(stage, ignore_errors=True)


def make_bat():
    # bat 必须用 GBK 写：cmd 按当前代码页逐行解析批处理文件，
    # UTF-8 的中文在 936 下会在多字节边界处裂开，后半句被当成命令执行，
    # 表现就是「'xx' is not recognized」 + 退出码 9009。配合脚本里的
    # chcp 936，中文 Windows 下 100% 正确，英文系统最多显示乱码但不会截断。
    with open(OUT_BAT, "w", encoding="gbk", errors="replace", newline="\r\n") as f:
        f.write(BAT)


def size_mb(path):
    return os.path.getsize(path) / 1048576


def main():
    ap = argparse.ArgumentParser(description="打包单文件版")
    ap.add_argument("--bundle-deps", action="store_true",
                    help="把依赖 wheel 一并打包，生成离线版 .pyz")
    ap.add_argument("--refresh-deps", action="store_true",
                    help="重新下载依赖 wheel（清单变了之后用）")
    ap.add_argument("--deps-dir", default=os.path.join(HERE, "offline_deps"),
                    help="wheel 缓存目录，默认 offline_deps")
    args = ap.parse_args()

    if not os.path.exists(TEMPLATE):
        print(f"缺少模板：{TEMPLATE}")
        return 1

    tpl = open(TEMPLATE, encoding="utf-8").read()
    if '"@@BLOB@@"' not in tpl:
        print('模板里没有 BLOB = "@@BLOB@@" 占位符')
        return 1

    wheel_py = ""
    wheels = []
    if args.bundle_deps:
        print("[1/5] 准备依赖 wheel")
        try:
            wheels = ensure_wheels(args.deps_dir, refresh=args.refresh_deps)
        except Exception as e:
            print(f"      失败：{type(e).__name__}: {e}")
            return 2
        total = sum(os.path.getsize(w) for w in wheels) / 1048576
        for w in wheels:
            print(f"      + {os.path.basename(w):<52} {os.path.getsize(w) / 1048576:5.2f} MB")
        print(f"      合计 {len(wheels)} 个 / {total:.1f} MB")
        wheel_py = f"{sys.version_info[0]}.{sys.version_info[1]}"
        print(f"      wheel 面向 Python {wheel_py}（版本不同会自动改走联网安装）")
    else:
        print("[1/5] 轻量模式：不打包依赖")

    print("[2/5] 压缩源码")
    payload, used, skipped = build_payload(wheels)
    print(f"      {len(used)} 个文件 -> {len(payload) / 1048576:.2f} MB")
    for f in skipped:
        print(f"      - {f}（不存在，跳过）")
    if not used:
        print("没有可打包的文件")
        return 1

    if args.bundle_deps:
        print("[3/5] 生成离线版 .pyz")
        launcher = render(tpl, '""', wheel_py)      # BLOB 留空，从 payload.zip 读
        make_pyz(launcher, payload, OUT_PYZ)
        with open(OUT_PYZ, "rb") as f:
            head = f.read(4)
        if head[:2] != b"PK":
            print("      产物不是 zip，异常")
            return 3
        print(f"      {os.path.basename(OUT_PYZ)}  {size_mb(OUT_PYZ):.1f} MB")
        target_for_check = OUT_PYZ
    else:
        print("[3/5] 生成轻量版 .py")
        chunks = [base64.b64encode(payload).decode("ascii")]
        raw_b64 = chunks[0]
        lines = [raw_b64[i:i + CHUNK] for i in range(0, len(raw_b64), CHUNK)]
        literal = "(\n" + "\n".join(f'    "{c}"' for c in lines) + "\n)"
        write_text(OUT_PY, render(tpl, literal, ""))
        print(f"      {os.path.basename(OUT_PY)}  {size_mb(OUT_PY) * 1024:.0f} KB")
        target_for_check = OUT_PY

    print("[4/5] 生成启动入口")
    make_bat()
    with open(OUT_BAT, "rb") as f:
        raw = f.read()
    try:
        raw.decode("gbk")
        enc = "GBK"
    except Exception:
        enc = "非 GBK（异常）"
    print(f"      {os.path.basename(OUT_BAT)}  {len(raw)} B  编码 {enc}")

    print("[5/5] 语法自检")
    if target_for_check.lower().endswith(".pyz"):
        # pyz 不能直接 py_compile，单独把 __main__.py 抽出来编译
        with zipfile.ZipFile(target_for_check) as z:
            src = z.read("__main__.py")
        tmp = os.path.join(HERE, "_check_main.py")
        with open(tmp, "wb") as f:
            f.write(src)
        try:
            py_compile.compile(tmp, doraise=True)
        except py_compile.PyCompileError as e:
            print(f"      编译失败：{e}")
            return 4
        finally:
            for p in (tmp, tmp + "c"):
                if os.path.exists(p):
                    os.remove(p)
        print("      通过（__main__.py 已单独编译）")
    else:
        try:
            py_compile.compile(target_for_check, doraise=True)
        except py_compile.PyCompileError as e:
            print(f"      编译失败：{e}")
            return 4
        print("      通过")

    print()
    print("完成。运行方式：")
    print(f"      python {os.path.basename(target_for_check)}")
    print(f"      或双击 {os.path.basename(OUT_BAT)}")
    print()
    print("作者：黄智皓    本项目完全免费，禁止商用和学术造假")
    return 0


if __name__ == "__main__":
    sys.exit(main())
