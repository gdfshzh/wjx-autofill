#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
run.py —— 统一入口，把分散的模块串成一条命令

子命令：
    doctor      环境自检（依赖 / 指纹 / 网络 / 问卷可达性）
    qr          二维码解码，只取链接
    parse       抓页面并解析出 survey.json
    fill        用真浏览器逐题填写（可见，最像人）
    batch       HTTP 批量提交（快，吃 IP）
    pipeline    二维码/链接 -> 解析 -> 生成答案 -> 提交，一条龙
    gui         启动网页控制台

示例：
    python run.py doctor
    python run.py qr --image qr.jpg
    python run.py pipeline --qr qr.jpg --dry
    python run.py pipeline --url https://v.wjx.cn/vm/Oki2m8H.aspx --count 3 --gap 60
    python run.py fill --url https://v.wjx.cn/vm/Oki2m8H.aspx
"""

import argparse
import json
import os
import subprocess
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)


# ---------------------------------------------------------------- 工具
def ok(msg):
    print(f"  [OK]   {msg}")


def warn(msg):
    print(f"  [警告] {msg}")


def err(msg):
    print(f"  [失败] {msg}")


def decode_qr(image_path: str) -> str:
    """解二维码，返回其中的 URL。后端选择统一交给 qr_decode。"""
    from qr_decode import decode_file
    return decode_file(image_path)


def shortid_from(url: str) -> str:
    """统一走 wjx_autofill.normalize_shortid，避免各处各写一套提取逻辑。"""
    from wjx_autofill import normalize_shortid
    return normalize_shortid(url)


# ---------------------------------------------------------------- doctor
def cmd_doctor(args):
    print("环境自检\n")

    v = sys.version_info
    ok(f"Python {v.major}.{v.minor}.{v.micro}")

    mods = [
        ("beautifulsoup4", "bs4", "解析问卷结构"),
        ("lxml", "lxml", "解析加速"),
        ("zxing-cpp", "zxingcpp", "二维码解码（推荐）"),
        ("pillow", "PIL", "二维码图片读取"),
        ("curl_cffi", "curl_cffi", "浏览器 TLS 指纹"),
        ("playwright", "playwright", "真浏览器自动化"),
        ("psutil", "psutil", "并发探测内存测量"),
    ]
    for pkg, mod, desc in mods:
        try:
            __import__(mod)
            ok(f"{pkg:<16} {desc}")
        except ImportError:
            if pkg in ("zxing-cpp", "pillow"):
                # 这两个是二维码的推荐组合，缺了还有 OpenCV 兜底
                warn(f"{pkg:<16} {desc} —— 未安装，pip install {pkg}")
            else:
                warn(f"{pkg:<16} {desc} —— 未安装，pip install {pkg}")

    try:
        from qr_decode import backend_name
        be = backend_name()
        if be in ("zxing", "cv2"):
            ok(f"二维码后端     {be}")
        else:
            warn("二维码后端     无，上传二维码图片会失败，可直接填短链")
    except Exception:
        pass

    try:
        from humanize import status
        st = status()
        if st["curl_cffi"]:
            ok(f"TLS 指纹       {st['impersonate']}")
        else:
            warn("TLS 指纹       未启用，将退回 Python urllib")
    except Exception as e:
        warn(f"humanize 模块不可用: {e}")

    # 问卷可达性
    url = args.url or f"https://v.wjx.cn/vm/{args.shortid or 'Oki2m8H'}.aspx"
    try:
        from wjx_autofill import Session, fetch_meta
        t0 = time.time()
        s = Session(human=True)
        meta = fetch_meta(s, shortid_from(url))
        ok(f"问卷可达       {len(meta['html'])} 字节 / {time.time() - t0:.1f}s / "
           f"human={s.human}")
    except Exception as e:
        err(f"问卷不可达     {type(e).__name__}: {str(e)[:80]}")


# ---------------------------------------------------------------- qr
def cmd_qr(args):
    url = decode_qr(args.image)
    ok(f"链接  {url}")
    print(f"短链  {shortid_from(url)}")


# ---------------------------------------------------------------- parse
def cmd_parse(args):
    from wjx_autofill import Session, fetch_meta
    from parse_wjx import parse_html

    url = args.url
    sid = shortid_from(url)
    s = Session(human=True)
    meta = fetch_meta(s, sid)
    data = parse_html(meta["html"])
    data["source_url"] = f"https://v.wjx.cn/vm/{sid}.aspx"

    out = args.out or os.path.join(HERE, "survey.json")
    json.dump(data, open(out, "w", encoding="utf-8"), ensure_ascii=False, indent=2)
    ok(f"{data['question_count']} 题，必答 {data['required_count']} 题 -> {out}")

    from parse_wjx import check_compat
    c = check_compat(data)
    if c["ok"]:
        ok("题型全部支持，可直接用浏览器填写")
    else:
        warn("以下题型填写器尚未实现，会被跳过：")
        for topic, kind in c["unsupported"]:
            print(f"        Q{topic}  {kind}")
    for k, v in data["type_summary"].items() if "type_summary" in data else []:
        print(f"      {k}: {v}")


# ---------------------------------------------------------------- fill
def cmd_fill(args):
    cmd = [
        sys.executable, os.path.join(HERE, "wjx_browser.py"),
        "--url", args.url,
        "--slow", str(args.slow),
        "--retry", str(args.retry),
    ]
    if args.answers:
        cmd += ["--answers", args.answers]
    if args.headless:
        cmd.append("--headless")
    if args.no_submit:
        cmd.append("--no-submit")
    subprocess.run(cmd)


# ---------------------------------------------------------------- batch
def cmd_batch(args):
    import random as _r
    from wjx_autofill import (Session, submit_once, random_answers,
                              validate_answers, CODE_MEANING)
    from parse_wjx import parse_html

    sid = shortid_from(args.url)
    s = Session(human=True)
    meta = fetch_meta(s, sid)
    survey = parse_html(meta["html"])

    if args.answers:
        answers = json.load(open(args.answers, encoding="utf-8"))
    else:
        answers = random_answers(survey)
    problems = validate_answers(answers, survey)
    if problems:
        err("答案校验未通过：" + "；".join(problems[:3]))
        return

    print(f"问卷：{survey['title']}")
    print(f"计划：{args.count} 轮，轮间隔 {args.gap}s，代理 {args.proxy or '直连'}\n")

    ok_n = fail_n = 0
    streak = 0
    for i in range(1, args.count + 1):
        sess = Session(proxy=args.proxy, human=True)
        try:
            m = fetch_meta(sess, sid)
            if getattr(sess, "human", False) and args.warm:
                from humanize import warm_up, fire_beacons
                warm_up(sess, m["html"], m["url"])
                fire_beacons(sess, m["html"], m["url"])
            if args.think > 0:
                time.sleep(args.think)
            else:
                from humanize import reading_time
                time.sleep(reading_time(len(survey["questions"])))

            r = submit_once(sess, sid, survey, answers, meta=m,
                            think_seconds=0, verbose=False)
        except Exception as e:
            fail_n += 1
            err(f"第 {i}/{args.count} 轮 异常 {type(e).__name__}: {str(e)[:70]}")
            time.sleep(args.gap)
            continue

        code = r["code"]
        if code == 10:
            ok_n += 1
            streak = 0
            jid = r["raw"].split("joinid=")[1].split("&")[0] if "joinid=" in r["raw"] else "-"
            ok(f"第 {i}/{args.count} 轮 成功  joinid={jid}  ktimes={r['ktimes']}")
        else:
            fail_n += 1
            err(f"第 {i}/{args.count} 轮 code={code} {CODE_MEANING.get(code, '')}")
            if code == 7:
                streak += 1
                if streak >= 3:
                    err("连续 3 次被安全校验拦截，停止。建议换出口并冷却 30 分钟")
                    break
                wait = 180 * streak
                print(f"      退避 {wait // 60} 分钟后继续")
                time.sleep(wait)
                continue

        if i < args.count and args.gap > 0:
            gap = args.gap * _r.uniform(0.85, 1.35)   # 别用等距
            print(f"      等待 {gap:.0f}s")
            time.sleep(gap)

    print(f"\n完成：成功 {ok_n}，失败 {fail_n}")


# ---------------------------------------------------------------- pipeline
def cmd_pipeline(args):
    """二维码/链接 -> 解析 -> 生成答案 -> 提交。目标是「给一张图就能跑完」。"""
    print("=" * 60)
    url = args.url
    if args.qr:
        print("[1/4] 解码二维码")
        url = decode_qr(args.qr)
        ok(url)
    else:
        print("[1/4] 使用给定链接")

    sid = shortid_from(url)
    print(f"[2/4] 解析问卷（短链 {sid}）")
    from wjx_autofill import Session, fetch_meta
    from parse_wjx import parse_html, parse as parse_file
    s = Session(human=True)
    meta = fetch_meta(s, sid)
    survey = parse_html(meta["html"])
    json.dump(survey, open(os.path.join(HERE, "survey.json"), "w", encoding="utf-8"),
              ensure_ascii=False, indent=2)
    ok(f"{survey['title']} · {survey['question_count']} 题")

    print("[3/4] 生成答案")
    from wjx_autofill import random_answers, validate_answers, build_submitdata
    if args.answers:
        answers = json.load(open(args.answers, encoding="utf-8"))
        ok(f"来自 {args.answers}")
    else:
        answers = random_answers(survey)
        ok("随机生成")
    p = validate_answers(answers, survey)
    if p:
        err("校验未通过：" + "；".join(p[:3]))
        return
    print("      payload:", build_submitdata(answers, survey)[:110] + "…")

    print("[4/4] 提交")
    if args.dry:
        warn("--dry 已跳过实际提交")
        return
    if args.count > 1:
        args.answers = args.answers  # 保持同一套答案
        cmd_batch(args)
        return

    from wjx_autofill import submit_once
    from humanize import warm_up, fire_beacons, reading_time
    sess = Session(proxy=args.proxy, human=True)
    m = fetch_meta(sess, sid)
    warm_up(sess, m["html"], m["url"])
    fire_beacons(sess, m["html"], m["url"])
    wait = args.think if args.think > 0 else reading_time(len(survey["questions"]))
    print(f"      等待 {wait:.0f}s 模拟作答")
    time.sleep(wait)
    r = submit_once(sess, sid, survey, answers, meta=m, think_seconds=0, verbose=False)
    if r["code"] == 10:
        jid = r["raw"].split("joinid=")[1].split("&")[0] if "joinid=" in r["raw"] else "-"
        ok(f"提交成功 joinid={jid} ktimes={r['ktimes']}")
    else:
        err(f"code={r['code']} {r.get('meaning', '')}  {r['raw'][:80]}")


# ---------------------------------------------------------------- gui
def cmd_gui(args):
    subprocess.run([sys.executable, os.path.join(HERE, "server.py"),
                    "--port", str(args.port)])


def cmd_forward(args):
    """把子命令之后的参数原样转给目标脚本。

    不用 argparse 收集的参数：它会把 `--levels 1` 拆成「未知选项 + 位置参数」
    分到两个地方，再拼回来顺序就乱了。直接切 sys.argv 最省事。
    """
    rest = sys.argv[2:]
    if rest and rest[0] == "--":
        rest = rest[1:]
    subprocess.run([sys.executable, "-u", os.path.join(HERE, args.script)] + rest)


# ---------------------------------------------------------------- 入口
def main():
    # 统一成 UTF-8：子进程的输出会被按 UTF-8 读，不一致就会乱码
    from console import force_utf8
    force_utf8()
    ap = argparse.ArgumentParser(
        description="问卷自动化统一入口",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__)
    sub = ap.add_subparsers(dest="cmd", required=True)

    p = sub.add_parser("doctor", help="环境自检")
    p.add_argument("--url")
    p.add_argument("--shortid")
    p.set_defaults(func=cmd_doctor)

    p = sub.add_parser("qr", help="二维码解码")
    p.add_argument("--image", required=True)
    p.set_defaults(func=cmd_qr)

    p = sub.add_parser("parse", help="抓页面并解析")
    p.add_argument("--url", required=True)
    p.add_argument("--out")
    p.set_defaults(func=cmd_parse)

    p = sub.add_parser("fill", help="真浏览器填写")
    p.add_argument("--url", required=True)
    p.add_argument("--answers")
    p.add_argument("--slow", type=float, default=1.0)
    p.add_argument("--retry", type=int, default=1)
    p.add_argument("--headless", action="store_true")
    p.add_argument("--no-submit", action="store_true")
    p.set_defaults(func=cmd_fill)

    p = sub.add_parser("batch", help="HTTP 批量提交")
    p.add_argument("--url", required=True)
    p.add_argument("--answers")
    p.add_argument("--count", type=int, default=1)
    p.add_argument("--gap", type=int, default=60)
    p.add_argument("--think", type=int, default=0, help="0=按题量自动估算")
    p.add_argument("--proxy")
    p.add_argument("--warm", action="store_true", default=True)
    p.set_defaults(func=cmd_batch)

    p = sub.add_parser("pipeline", help="二维码/链接 -> 提交 一条龙")
    p.add_argument("--qr", help="二维码图片路径")
    p.add_argument("--url", help="或直接给问卷链接")
    p.add_argument("--answers")
    p.add_argument("--count", type=int, default=1)
    p.add_argument("--gap", type=int, default=60)
    p.add_argument("--think", type=int, default=0)
    p.add_argument("--proxy")
    p.add_argument("--dry", action="store_true")
    p.set_defaults(func=cmd_pipeline)

    p = sub.add_parser("gui", help="启动网页控制台")
    p.add_argument("--port", type=int, default=8765)
    p.set_defaults(func=cmd_gui)

    p = sub.add_parser("probe", help="探测本机并行浏览器上限")
    p.add_argument("rest", nargs="*",
                   help="原样转给 probe_parallel.py（如 --levels 1,2,4,6）")
    p.set_defaults(func=cmd_forward, script="probe_parallel.py")

    p = sub.add_parser("par", help="并行作答（多问卷 / 多轮）")
    p.add_argument("rest", nargs="*",
                   help="原样转给 parallel.py（如 --job OOxx:3 --workers 3）")
    p.set_defaults(func=cmd_forward, script="parallel.py")

    # 转发类子命令的参数是未知的，走 parse_known_args 收进来再拼回 rest
    args, extra = ap.parse_known_args()
    if extra:
        if getattr(args, "cmd", "") in ("probe", "par"):
            args.rest = list(getattr(args, "rest", None) or []) + extra
        else:
            ap.error("unrecognized arguments: " + " ".join(extra))
    if getattr(args, "cmd", "") == "pipeline" and not (args.qr or args.url):
        ap.error("pipeline 需要 --qr 或 --url")
    args.func(args)


if __name__ == "__main__":
    main()
