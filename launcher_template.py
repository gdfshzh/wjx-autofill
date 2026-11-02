#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""问卷星批量作答控制台 —— 单文件版

一个文件装下：后端服务、前端页面、页面解析器、HTTP 提交器、浏览器驱动、并行执行器。
首次运行自动检测并安装缺失依赖；之后每次启动只做增量同步，已有问卷与模板不会被覆盖。

用法：
    python wjx_probe_all_in_one.py                  默认 8765，自动开浏览器
    python wjx_probe_all_in_one.py --port 9000
    python wjx_probe_all_in_one.py --no-browser
    python wjx_probe_all_in_one.py --doctor         只做环境自检
    python wjx_probe_all_in_one.py --rebuild        强制重写运行时里的代码文件
    python wjx_probe_all_in_one.py --reset-data     连 survey.json 与模板一起重置
    python wjx_probe_all_in_one.py --clean          清掉运行时（保留问卷与模板）后退出

作者：黄智皓
本项目完全免费，禁止商用和学术造假
"""

import argparse
import base64
import importlib.util
import io
import os
import shutil
import socket
import subprocess
import sys
import zipfile

APP_NAME = "问卷星批量作答控制台"
APP_VERSION = "1.3.0"
AUTHOR = "黄智皓"
NOTICE = "本项目完全免费，禁止商用和学术造假"

# 打包时替换成压缩后的项目快照（base64 的 zip）
BLOB = "@@BLOB@@"

# (导入名, pip 包名, 用途, 是否必需)
DEPS = [
    ("bs4", "beautifulsoup4", "解析问卷 HTML", True),
    ("lxml", "lxml", "解析加速", True),
    ("curl_cffi", "curl_cffi", "浏览器 TLS 指纹", False),
    ("playwright", "playwright", "真浏览器作答", False),
    ("PIL", "pillow", "二维码图片读取", False),
    ("zxingcpp", "zxing-cpp", "二维码解码", False),
    ("psutil", "psutil", "并发探测内存测量", False),
]

# 打包时写入的 Python 版本，例如 "3.13"。
# lxml / zxing-cpp 这些带 C 扩展，wheel 与 cpXX 绑定，版本对不上装了也用不了，
# 所以版本不一致时直接跳过内置 wheel 走联网，别让 pip 报一堆看不懂的错。
WHEEL_PY = "@@WHEEL_PY@@"

# 内置 wheel 在这里（相对运行时目录）
WHEEL_DIRNAME = "wheels"

# 这几份是用户数据，已经存在就不动
DATA_FILES = ("survey.json", "fill_template.json", "choice_weights.json")

RUNTIME_DIRNAME = "_runtime"


# ---------------------------------------------------------------- 输出
def hr(ch="─", n=64):
    print(ch * n)


def say(msg=""):
    print(msg, flush=True)


def step(i, total, title):
    say()
    print(f"[{i}/{total}] {title}", flush=True)


def ok(msg):
    print(f"      [OK]   {msg}", flush=True)


def warn(msg):
    print(f"      [警告] {msg}", flush=True)


def bad(msg):
    print(f"      [失败] {msg}", flush=True)


# ---------------------------------------------------------------- 路径
def base_dir():
    """单文件所在目录。打包成 exe 时用 exe 所在目录。"""
    if getattr(sys, "frozen", False):
        return os.path.dirname(os.path.abspath(sys.executable))
    here = os.path.dirname(os.path.abspath(__file__))
    if os.path.isdir(here):
        return here
    # 跑在 .pyz 里时 __file__ 形如 D:\x\pkg.pyz\__main__.py，dirname 不是真目录
    return os.path.dirname(os.path.abspath(sys.argv[0]))


def self_archive():
    """如果自己在 .pyz 里跑，返回那个 zip 的路径，否则 None。"""
    cands = []
    try:
        a = getattr(__loader__, "archive", None)   # zipimporter.archive
        if a:
            cands.append(a)
    except Exception:
        pass
    if sys.argv and sys.argv[0]:
        cands.append(os.path.abspath(sys.argv[0]))
    for c in cands:
        try:
            if c and os.path.isfile(c) and zipfile.is_zipfile(c):
                return c
        except Exception:
            continue
    return None


def payload_bytes():
    """取出内嵌的项目快照（zip 字节）。

    两种形态：
      .py  —— base64 常量 BLOB 里
      .pyz —— 同一个 zip 文件里的 payload.zip，不用 base64，体积不膨胀 33%
    """
    if BLOB and not BLOB.startswith("@@"):
        return base64.b64decode(BLOB)
    arc = self_archive()
    if not arc:
        raise RuntimeError("找不到内嵌数据：既没有内置 base64，也不在 .pyz 包里")
    with zipfile.ZipFile(arc) as z:
        return z.read("payload.zip")


def runtime_dir(create=True):
    """运行时目录。放在单文件旁边便于查日志；不可写时退到用户目录。"""
    cand = os.path.join(base_dir(), RUNTIME_DIRNAME)
    if create:
        try:
            os.makedirs(cand, exist_ok=True)
            probe = os.path.join(cand, ".w")
            with open(probe, "w", encoding="utf-8") as f:
                f.write("1")
            os.remove(probe)
            return cand
        except Exception:
            pass
    # 只读目录（U 盘、Program Files）退到用户数据目录
    home = os.environ.get("LOCALAPPDATA") or os.path.expanduser("~")
    fallback = os.path.join(home, "wjx_probe_runtime")
    if create:
        os.makedirs(fallback, exist_ok=True)
    return fallback


def is_data_file(name):
    base = os.path.basename(name)
    return base in DATA_FILES or base.startswith("answers")


# ---------------------------------------------------------------- 解包
def unpack(rt, force=False, reset_data=False):
    """把内嵌快照同步到运行时目录。

    代码文件内容不同才写（省 IO，也能让更新立即生效）；
    数据文件已存在就保留，避免每次启动把用户解析的问卷冲掉；
    wheel 只要文件在就跳过——几十 MB 的包每次都读一遍比对，启动会明显变慢。
    """
    raw = payload_bytes()
    zf = zipfile.ZipFile(io.BytesIO(raw))
    written = kept = 0
    for info in zf.infolist():
        if info.is_dir():
            continue
        name = info.filename
        dst = os.path.join(rt, name.replace("/", os.sep))
        parent = os.path.dirname(dst)
        if parent:
            os.makedirs(parent, exist_ok=True)

        if name.startswith(WHEEL_DIRNAME + "/") and os.path.exists(dst) and not force:
            kept += 1
            continue

        data = zf.read(name)

        if is_data_file(name) and os.path.exists(dst) and not reset_data:
            kept += 1
            continue
        if os.path.exists(dst) and not force:
            try:
                with open(dst, "rb") as f:
                    if f.read() == data:
                        kept += 1
                        continue
            except Exception:
                pass
        with open(dst, "wb") as f:
            f.write(data)
        written += 1
    return written, kept


def clean_runtime(rt):
    """删掉运行时的代码与静态资源，保留问卷、模板与答案文件。"""
    if not os.path.isdir(rt):
        return 0
    n = 0
    for name in os.listdir(rt):
        p = os.path.join(rt, name)
        if is_data_file(name):
            continue
        try:
            if os.path.isdir(p):
                shutil.rmtree(p)
            else:
                os.remove(p)
            n += 1
        except Exception:
            pass
    return n


# ---------------------------------------------------------------- 依赖
def has_module(mod):
    try:
        return importlib.util.find_spec(mod) is not None
    except Exception:
        return False


def scan_deps():
    missing = []
    for mod, pkg, why, must in DEPS:
        missing.append((mod, pkg, why, must, has_module(mod)))
    return missing


def pip_install(pkgs):
    """先走默认源，失败再走清华镜像。返回 True 表示全部装好。"""
    base = [sys.executable, "-m", "pip", "install",
            "--disable-pip-version-check", "--no-input", "--progress-bar", "off"]
    attempts = [
        (base + pkgs, "默认源"),
        (base + ["-i", "https://pypi.tuna.tsinghua.edu.cn/simple"] + pkgs, "清华镜像"),
    ]
    for cmd, tag in attempts:
        say(f"      pip 安装（{tag}）：{' '.join(pkgs)}")
        try:
            r = subprocess.run(cmd)
        except Exception as e:
            warn(f"{tag} 调用失败：{e}")
            continue
        if r.returncode == 0:
            return True
        warn(f"{tag} 返回 {r.returncode}")
    return False


def wheel_dir(rt):
    d = os.path.join(rt, WHEEL_DIRNAME)
    return d if os.path.isdir(d) else ""


def offline_ok(rt):
    """内置 wheel 能不能直接用：得存在，且打包时的 Python 版本与当前一致。"""
    if not wheel_dir(rt):
        return False, "包内没有内置 wheel"
    if not WHEEL_PY or WHEEL_PY.startswith("@@"):
        return False, "打包时没记录 Python 版本"
    cur = f"{sys.version_info[0]}.{sys.version_info[1]}"
    if cur != WHEEL_PY:
        return False, f"内置 wheel 是 Python {WHEEL_PY} 的，当前是 {cur}"
    return True, ""


def pip_install_offline(rt, pkgs):
    """用内置 wheel 装，全程不联网。返回 True 表示成功。

    --no-index 关掉索引、--find-links 指向本地目录，
    缺失的依赖（playwright 要 pyee/greenlet，curl_cffi 要 cffi）也会一起从本地解析。
    """
    wd = wheel_dir(rt)
    n = len([x for x in os.listdir(wd) if x.endswith(".whl")]) if wd else 0
    cmd = [sys.executable, "-m", "pip", "install",
           "--disable-pip-version-check", "--no-input", "--progress-bar", "off",
           "--no-index", "--find-links", wd] + pkgs
    say(f"      离线安装（内置 {n} 个 wheel，不联网）：{' '.join(pkgs)}")
    try:
        r = subprocess.run(cmd)
    except Exception as e:
        warn(f"离线安装调用失败：{e}")
        return False
    if r.returncode != 0:
        warn(f"离线安装返回 {r.returncode}")
        return False
    return True


def ensure_deps(rt, skip=False):
    """检测并安装。返回 (缺失且装不上的必需包, 仍缺的可选包)。"""
    rows = scan_deps()
    need = [r for r in rows if not r[4]]
    if not need:
        ok(f"{len(rows)} 个依赖全部就位")
        return [], []

    for mod, pkg, why, must, _ in need:
        warn(f"{pkg:<16} 缺失（{why}）{'· 必需' if must else '· 可选'}")

    if skip:
        warn("按 --skip-install 跳过安装")
        return [r for r in need if r[3]], [r for r in need if not r[3]]

    pkgs = []
    for _, pkg, _, _, _ in need:
        if pkg not in pkgs:
            pkgs.append(pkg)

    say()
    usable, why_not = offline_ok(rt)
    installed = False
    if usable:
        installed = pip_install_offline(rt, pkgs)
        if not installed:
            warn("内置 wheel 装不上，转联网安装")
    elif wheel_dir(rt):
        warn(f"{why_not}，跳过内置 wheel")
    else:
        say("      首次安装含 playwright 时可能要 1~3 分钟，请勿关闭窗口")

    if not installed:
        pip_install(pkgs)

    still = [r for r in need if not has_module(r[0])]
    for mod, pkg, why, must, _ in need:
        if has_module(mod):
            ok(f"{pkg} 已安装")
    for mod, pkg, why, must, _ in still:
        bad(f"{pkg} 仍未装上（{why}）")

    return [r for r in still if r[3]], [r for r in still if not r[3]]


def browser_hint():
    """playwright 走系统 Edge/Chrome，不需要 playwright install。这里只做提示。"""
    if not has_module("playwright"):
        return
    cands = [
        r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe",
        r"C:\Program Files\Microsoft\Edge\Application\msedge.exe",
        r"C:\Program Files\Google\Chrome\Application\chrome.exe",
        r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe",
    ]
    for p in cands:
        if os.path.exists(p):
            ok(f"浏览器通道可用：{os.path.basename(os.path.dirname(os.path.dirname(p)))}")
            return
    warn("没找到系统 Edge / Chrome，浏览器作答会退回 HTTP 模式")


# ---------------------------------------------------------------- 端口
def free_port(start):
    for p in range(start, start + 40):
        s = socket.socket()
        try:
            s.bind(("127.0.0.1", p))
            return p
        except OSError:
            continue
        finally:
            s.close()
    return start


# ---------------------------------------------------------------- 主流程
def main():
    # 语法层面 3.6 就能跑，但 zipfile 的 compresslevel、f-string 调试写法
    # 之类分散在各处，统一按 3.8 兜住，免得报一堆看不懂的 SyntaxError
    if sys.version_info < (3, 8):
        print(f"需要 Python 3.8 或更高版本，当前是 {sys.version.split()[0]}")
        print("下载：https://www.python.org/downloads/")
        return 1

    ap = argparse.ArgumentParser(
        prog="wjx_probe_all_in_one",
        description=f"{APP_NAME} 单文件版（作者：{AUTHOR}）")
    ap.add_argument("--port", type=int, default=8765, help="监听端口，默认 8765")
    ap.add_argument("--host", default="127.0.0.1", help="监听地址，默认仅本机")
    ap.add_argument("--no-browser", action="store_true", help="启动后不自动打开浏览器")
    ap.add_argument("--rebuild", action="store_true", help="强制重写运行时里的代码文件")
    ap.add_argument("--reset-data", action="store_true", help="连 survey.json、模板与概率配置一起重置")
    ap.add_argument("--clean", action="store_true", help="清掉运行时（保留问卷与模板）后退出")
    ap.add_argument("--skip-install", action="store_true", help="不自动安装依赖")
    ap.add_argument("--doctor", action="store_true", help="只做环境自检，不启动服务")
    args = ap.parse_args()

    if BLOB.startswith("@@"):
        print("这是打包模板，不是可直接运行的单文件。")
        print("先执行：python build_single.py")
        print("再运行生成的 wjx_probe_all_in_one.py")
        return 1

    # Windows 控制台默认 GBK，中文会乱码
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except Exception:
            pass

    say()
    hr("═")
    say(f"  {APP_NAME}   v{APP_VERSION}")
    say(f"  作者：{AUTHOR}")
    say(f"  {NOTICE}")
    hr("═")

    rt = runtime_dir()
    say(f"  Python {sys.version.split()[0]}   {sys.executable}")
    say(f"  运行时 {rt}")

    if args.clean:
        step(1, 1, "清理运行时")
        n = clean_runtime(rt)
        ok(f"已删除 {n} 项，问卷与模板保留")
        return 0

    # ① 解包
    # 必须先于依赖检查：内置 wheel 要先落到 _runtime/wheels/，离线安装才找得到
    step(1, 3, "准备运行时")
    try:
        w, k = unpack(rt, force=args.rebuild, reset_data=args.reset_data)
    except Exception as e:
        bad(f"解包失败：{type(e).__name__}: {e}")
        return 3
    ok(f"写入 {w} 个文件，复用 {k} 个")
    if args.reset_data:
        warn("按 --reset-data 重置了问卷与模板")

    if wheel_dir(rt):
        usable, why_not = offline_ok(rt)
        n_wheel = len([x for x in os.listdir(wheel_dir(rt)) if x.endswith(".whl")])
        if usable:
            ok(f"内置依赖包 {n_wheel} 个 wheel，缺依赖时离线安装，不用联网")
        elif not WHEEL_PY or WHEEL_PY.startswith("@@"):
            # 轻量版本身不带 wheel，这里是之前跑离线版留下的
            say(f"      运行时里有 {n_wheel} 个上次留下的 wheel，本版本不使用，--clean 可清掉")
        else:
            warn(f"内置依赖包 {n_wheel} 个 wheel，但{why_not}，缺依赖时走联网")

    server_py = os.path.join(rt, "server.py")
    if not os.path.exists(server_py):
        bad("运行时缺少 server.py，加 --rebuild 重试")
        return 3

    # ② 依赖
    step(2, 3, "检查依赖")
    missing_must, missing_opt = ensure_deps(rt, skip=args.skip_install)
    if missing_must:
        say()
        bad("必需依赖缺失，无法解析问卷："
            + "、".join(r[1] for r in missing_must))
        say("      手动装一次再运行：")
        say(f"      {sys.executable} -m pip install "
            + " ".join(r[1] for r in missing_must))
        return 2
    if missing_opt:
        warn("以下可选依赖缺失，对应功能会降级："
             + "、".join(r[1] for r in missing_opt))
    browser_hint()

    if args.doctor:
        step(3, 3, "环境自检")
        ok("解析器 parse_wjx.py")
        ok("提交器 wjx_autofill.py")
        ok("二维码解码 qr_decode.py")
        ok("浏览器驱动 wjx_browser.py" if has_module("playwright")
           else "浏览器驱动 缺少 playwright，仅 HTTP 可用")
        ok("前端 web/index.html" if os.path.exists(os.path.join(rt, "web", "index.html"))
           else "前端缺失")
        say()
        ok("自检结束，可以直接启动")
        return 0

    # ③ 启动
    step(3, 3, "启动服务")
    port = free_port(args.port)
    if port != args.port:
        warn(f"端口 {args.port} 被占用，改用 {port}")
    say(f"      地址 http://{args.host}:{port}/")
    say("      Ctrl+C 结束")

    cmd = [sys.executable, "-u", server_py, "--port", str(port), "--host", args.host]
    if args.no_browser:
        cmd.append("--no-browser")

    try:
        return subprocess.call(cmd, cwd=rt)
    except KeyboardInterrupt:
        say("\n已停止")
        return 0


if __name__ == "__main__":
    sys.exit(main())
