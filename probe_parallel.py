#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""并行能力探测：这台机器最多能同时跑多少个隔离的浏览器会话。

两种隔离级别，开销差一个量级：

  context  同一个 browser 进程开 N 个 new_context()
           —— cookie / storage 各自独立，问卷星会认成不同会话
           —— 共享渲染进程池，内存增量小

  browser  开 N 个独立 browser 进程
           —— 隔离最彻底，但每个都带一套 GPU/网络/渲染进程
           —— 内存增量大约是 context 的 3~5 倍

用法：

    python probe_parallel.py                       # 测本地页面，只看资源上限
    python probe_parallel.py --url https://v.wjx.cn/vm/OOxx.aspx --real
    python probe_parallel.py --mode browser --max 6

判定「这一档可用」要同时满足：
  · 每个页面都成功等到目标选择器（不是进程起来就算）
  · 单页平均加载时间 < --max-wait
  · 剩余可用内存 > --min-free（GB）
  · 没有进程崩溃

一档不合格就停，取上一档作为可用并发数。
"""

import argparse
import json
import os
import sys
import time

try:
    import psutil
except ImportError:
    sys.exit("缺 psutil：python -m pip install psutil")

try:
    from playwright.sync_api import sync_playwright
except ImportError:
    sys.exit("缺 playwright：python -m pip install playwright")

HERE = os.path.dirname(os.path.abspath(__file__))
UA_IOS = ("Mozilla/5.0 (iPhone; CPU iPhone OS 17_2 like Mac OS X) "
          "AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.2 "
          "Mobile/15E148 Safari/604.1")

# 判定页面「真的渲染出来了」的依据。本地页面没有 #divQuestion，给个兜底选择器。
LOCAL_URL = "http://127.0.0.1:8765/"
LOCAL_SEL = "#questions, #dock"
REAL_SEL = "#divQuestion"


def browser_mem_mb():
    """所有 Edge/Chrome 进程的常驻内存之和（MB）。"""
    total = 0
    for pr in psutil.process_iter(["name", "memory_info"]):
        try:
            name = (pr.info.get("name") or "").lower()
            if name in ("msedge.exe", "chrome.exe"):
                total += pr.info["memory_info"].rss
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            continue
    return total / 1024 / 1024


def free_mem_gb():
    vm = psutil.virtual_memory()
    return vm.available / 1024 / 1024 / 1024


def launch_browser(p, headless):
    args = ["--disable-blink-features=AutomationControlled",
            "--no-first-run", "--no-default-browser-check"]
    for channel in ("msedge", "chrome"):
        try:
            return p.chromium.launch(channel=channel, headless=headless, args=args)
        except Exception:
            continue
    return p.chromium.launch(headless=headless, args=args)


def new_ctx(browser):
    return browser.new_context(
        viewport={"width": 390, "height": 844},
        device_scale_factor=2,
        is_mobile=True,
        has_touch=True,
        user_agent=UA_IOS,
        locale="zh-CN",
        timezone_id="Asia/Shanghai",
    )


def run_level(p, level, mode, url, sel, headless, timeout):
    """开 level 个会话，全部加载 url。返回统计。"""
    t0 = time.time()
    browsers, pages, closers = [], [], []

    if mode == "context":
        b = launch_browser(p, headless)
        browsers.append(b)
        for _ in range(level):
            ctx = new_ctx(b)
            pages.append(ctx.new_page())
            closers.append(ctx)
    else:
        for _ in range(level):
            b = launch_browser(p, headless)
            browsers.append(b)
            ctx = new_ctx(b)
            pages.append(ctx.new_page())
            closers.append(ctx)

    launch_s = time.time() - t0

    # 先全部触发导航（commit 就返回），让浏览器自己并发加载，
    # 再统一等选择器 —— 顺序 goto 会低估并发能力
    t_nav = time.time()
    for pg in pages:
        try:
            pg.goto(url, wait_until="commit", timeout=timeout * 1000)
        except Exception:
            pass

    ok, waits = 0, []
    for pg in pages:
        t = time.time()
        try:
            pg.wait_for_selector(sel, timeout=timeout * 1000)
            ok += 1
            waits.append(time.time() - t)
        except Exception:
            waits.append(timeout)

    total_s = time.time() - t0

    # 必须在关闭之前测：close 之后进程就回收了，测出来是 0
    mem_abs = browser_mem_mb()

    for c in closers:
        try:
            c.close()
        except Exception:
            pass
    for b in browsers:
        try:
            b.close()
        except Exception:
            pass

    return {
        "level": level,
        "ok": ok,
        "launch_s": round(launch_s, 2),
        "avg_wait": round(sum(waits) / len(waits), 2) if waits else 0,
        "total_s": round(total_s, 2),
        "nav_s": round(time.time() - t_nav - sum(waits), 2),
        "mem_abs": mem_abs,
    }


def main():
    # 统一成 UTF-8：子进程的输出会被按 UTF-8 读，不一致就会乱码
    from console import force_utf8
    force_utf8()
    ap = argparse.ArgumentParser(description="探测本机并行浏览器上限")
    ap.add_argument("--url", default=LOCAL_URL, help="探测用的页面")
    ap.add_argument("--sel", default="", help="判定加载成功的选择器")
    ap.add_argument("--mode", choices=["context", "browser"], default="context")
    ap.add_argument("--levels", default="1,2,3,4,6,8", help="阶梯加压的档位")
    ap.add_argument("--max", type=int, default=0, help="最高档位上限")
    ap.add_argument("--headless", action="store_true", help="无头模式（省资源）")
    ap.add_argument("--timeout", type=int, default=25, help="单页加载超时秒数")
    ap.add_argument("--max-wait", type=float, default=15.0, help="单页平均加载超过这个秒数就算不合格")
    ap.add_argument("--min-free", type=float, default=1.0, help="剩余可用内存低于这个 GB 就停")
    ap.add_argument("--out", default=os.path.join(HERE, "parallel_report.json"))
    args = ap.parse_args()

    sel = args.sel or (REAL_SEL if "wjx.cn" in args.url else LOCAL_SEL)
    levels = [int(x) for x in args.levels.split(",") if x.strip()]
    if args.max:
        levels = [x for x in levels if x <= args.max]

    print(f"页面   : {args.url}")
    print(f"选择器 : {sel}")
    print(f"模式   : {args.mode}（{'各开独立进程' if args.mode == 'browser' else '同进程多 context'}）")
    print(f"机器   : {psutil.cpu_count()} 核 / 总内存 {psutil.virtual_memory().total/1024**3:.1f} GB"
          f" / 可用 {free_mem_gb():.1f} GB")
    print()

    base_mem = browser_mem_mb()
    print(f"启动前浏览器内存基线: {base_mem:.0f} MB\n")

    rows, best = [], 0
    with sync_playwright() as p:
        for lv in levels:
            r = run_level(p, lv, args.mode, args.url, sel, args.headless, args.timeout)
            r["mem_mb"] = round(r.pop("mem_abs") - base_mem, 0)
            free = free_mem_gb()
            r["mem_per"] = round(r["mem_mb"] / lv, 0) if lv else 0
            r["free_gb"] = round(free, 2)
            rows.append(r)

            flag = "OK "
            if r["ok"] < lv:
                flag = "FAIL"
            elif r["avg_wait"] > args.max_wait:
                flag = "SLOW"
            elif free < args.min_free:
                flag = "LOWMEM"
            print(f"[{flag}] {lv:>2} 个会话 | 全部就绪 {r['ok']}/{lv} | "
                  f"平均等待 {r['avg_wait']:>5.1f}s | 启动 {r['launch_s']:>4.1f}s | "
                  f"内存 +{r['mem_mb']:>5.0f}MB ({r['mem_per']:.0f}MB/个) | 剩余 {free:.1f}GB")

            if flag != "OK ":
                print(f"       → 这一档不合格（{flag}），停在上一档")
                break
            best = lv
            # 一档做完歇一下，别让上一档的进程回收影响下一档测量
            time.sleep(2)

    print()
    print(f"可用并发数: {best}（{args.mode} 模式）")
    if rows:
        per = sum(x["mem_per"] for x in rows if x["mem_per"]) / max(1, len([x for x in rows if x["mem_per"]]))
        print(f"每个会话约 {per:.0f} MB，按当前可用内存 {free_mem_gb():.1f} GB 估算，"
              f"还能再开约 {int(free_mem_gb() * 1024 / per)} 个（理论值，受 CPU 与站点限流影响）")

    json.dump({"url": args.url, "mode": args.mode, "best": best,
               "levels": rows, "ts": time.strftime("%Y-%m-%d %H:%M:%S")},
              open(args.out, "w", encoding="utf-8"), ensure_ascii=False, indent=2)
    print(f"\n报告已写 {args.out}")


if __name__ == "__main__":
    main()
