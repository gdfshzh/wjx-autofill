#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""并行作答执行器：两种并行模型，取舍不同。

先说清楚为什么只有两种：

    Playwright 的同步 API 不是线程安全的 —— browser / context / page 都绑在
    创建它的那个 greenlet 上，换个线程去调就报
        error: Cannot switch to a different thread
    所以「一个浏览器进程 + 多线程各开一个 context」这条路走不通。
    要真并行，只能多进程；要省内存，只能单进程里顺序来。

mode=context（默认，省内存）
    一个 browser 进程，N 个独立 context（cookie 各自独立），
    页面**并行加载**，填写**顺序进行**。
    省的是浏览器启动开销和加载等待，不省填写时间。

mode=process（真并行）
    每个任务一个 wjx_browser.py 子进程，各自带浏览器。
    填写真的同时进行，代价是每份约 450MB 内存（实测）。

实测数据（16 核 / 15.3GB，headless，本地页面）：
    context : 8 个会话 +1045MB → 边际约 60~80MB/个
    process : 4 个会话 +1855MB → 每份约 450MB
"""

import argparse
import json
import os
import queue
import random
import re
import subprocess
import sys
import threading
import time

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

from wjx_autofill import normalize_shortid, Session, fetch_meta      # noqa: E402
from parse_wjx import parse_html                                    # noqa: E402


# ---------------------------------------------------------------- 公共
def load_survey(shortid):
    sess = Session()
    meta = fetch_meta(sess, shortid)
    return parse_html(meta["html"], f"https://v.wjx.cn/vm/{shortid}.aspx")


def build_jobs(jobs_spec, answers_map):
    jobs = []
    for sid, n in jobs_spec:
        for r in range(1, n + 1):
            jobs.append({
                "shortid": sid,
                "url": f"https://v.wjx.cn/vm/{sid}.aspx",
                "answers": answers_map[sid],
                "round": r,
                "tag": f"[{sid}#{r}]",
            })
    return jobs


class Stats:
    def __init__(self):
        self.lock = threading.Lock()
        self.rows = []

    def add(self, row):
        with self.lock:
            self.rows.append(row)
            n_ok = sum(1 for x in self.rows if x.get("ok"))
            print(f"    进度：成功 {n_ok} / 完成 {len(self.rows)}", flush=True)

    def summary(self):
        with self.lock:
            rows = list(self.rows)
        return sum(1 for x in rows if x.get("ok")), \
               sum(1 for x in rows if not x.get("ok")), rows


# ---------------------------------------------------------------- context 模式
def run_context_mode(jobs, cfg, stats):
    """单进程多 context：并行加载、顺序填写。"""
    from playwright.sync_api import sync_playwright
    from wjx_browser import STEALTH_JS, Human, UA_IOS, WjxBrowser, launch

    with sync_playwright() as p:
        browser = launch(p, cfg["headless"])
        opened = []
        try:
            # 1) 先把所有页面并行拉起来：goto 只等 commit，不等渲染完
            for job in jobs:
                ctx = browser.new_context(
                    viewport={"width": 390, "height": 844},
                    device_scale_factor=3,
                    is_mobile=True,
                    has_touch=True,
                    user_agent=UA_IOS,
                    locale="zh-CN",
                    timezone_id="Asia/Shanghai",
                )
                ctx.add_init_script(STEALTH_JS)
                page = ctx.new_page()
                try:
                    page.goto(job["url"], wait_until="commit", timeout=45000)
                except Exception as e:
                    print(f"  {job['tag']} 打开失败 {type(e).__name__}", flush=True)
                opened.append((job, ctx, page))
            print(f"  {len(opened)} 个页面已同时发起加载\n", flush=True)

            # 2) 逐个填、逐个交。这一步没法并行 —— 同步 API 阻塞在单个 page 上
            for job, ctx, page in opened:
                t0 = time.time()
                try:
                    page.bring_to_front()
                    page.wait_for_selector("#divQuestion", timeout=30000)
                    time.sleep(random.uniform(1.0, 2.2))

                    human = Human(page, slow=cfg["slow"], verbose=True)
                    eng = WjxBrowser(page, job["survey"], human,
                                     retry=cfg["retry"], strict=False,
                                     manual_captcha=False)
                    eng.fill_all(job["answers"])
                    r = eng.submit(timeout=cfg["submit_timeout"])

                    jid = ""
                    if r.get("ok"):
                        m = re.search(r"joinactivity=(\d+)", r.get("url") or "")
                        jid = m.group(1) if m else ""
                    row = {"tag": job["tag"], "shortid": job["shortid"],
                           "round": job["round"], "ok": bool(r.get("ok")),
                           "joinid": jid, "msg": (r.get("msg") or "")[:80],
                           "secs": round(time.time() - t0, 1)}
                    print(f"{job['tag']} {'成功' if row['ok'] else '失败'} "
                          f"{('joinid=' + jid) if jid else row['msg']}  "
                          f"{row['secs']}s", flush=True)
                except Exception as e:
                    row = {"tag": job["tag"], "shortid": job["shortid"],
                           "round": job["round"], "ok": False,
                           "msg": f"{type(e).__name__}: {str(e)[:70]}",
                           "joinid": "", "secs": round(time.time() - t0, 1)}
                    print(f"{job['tag']} 异常 {row['msg']}", flush=True)
                stats.add(row)
                try:
                    ctx.close()
                except Exception:
                    pass
        finally:
            try:
                browser.close()
            except Exception:
                pass


# ---------------------------------------------------------------- process 模式
def run_process_mode(jobs, cfg, stats):
    """每个任务一个子进程，真并行。"""
    workdir = os.path.join(HERE, "_par")
    os.makedirs(workdir, exist_ok=True)

    q = queue.Queue()
    for job in jobs:
        q.put(job)

    def worker(n):
        while True:
            try:
                job = q.get_nowait()
            except queue.Empty:
                return
            t0 = time.time()
            ans_path = os.path.join(workdir, f"ans_{job['shortid']}_{job['round']}.json")
            json.dump(job["answers"], open(ans_path, "w", encoding="utf-8"),
                      ensure_ascii=False)
            # 每份问卷有自己的结构文件：子进程不能共用 survey.json，
            # 否则第二份问卷会按第一份的题号去点
            survey_path = os.path.join(workdir, f"survey_{job['shortid']}.json")
            if not os.path.exists(survey_path):
                json.dump(job["survey"], open(survey_path, "w", encoding="utf-8"),
                          ensure_ascii=False)
            cmd = [sys.executable, "-u", os.path.join(HERE, "wjx_browser.py"),
                   "--url", job["url"],
                   "--survey", survey_path,
                   "--answers", ans_path,
                   "--count", "1",
                   "--slow", str(cfg["slow"])]
            if cfg["headless"]:
                cmd.append("--headless")
            try:
                out = subprocess.run(cmd, capture_output=True, text=True,
                                     encoding="utf-8", errors="replace",
                                     timeout=cfg["submit_timeout"] + 300, cwd=HERE)
                text = out.stdout or ""
            except subprocess.TimeoutExpired:
                text = ""
            finally:
                try:
                    os.remove(ans_path)
                except Exception:
                    pass

            ok = "结果 {'ok': True" in text
            m = re.search(r"joinactivity=(\d+)", text)
            jid = m.group(1) if m else ""
            # 失败原因从「结果」那行里抠出来
            msg = ""
            if not ok:
                mm = re.search(r"结果 \{'ok': False, 'msg': '([^']{0,80})", text)
                msg = mm.group(1) if mm else (text.strip().splitlines()[-1][:70]
                                              if text.strip() else "无输出")

            row = {"tag": job["tag"], "shortid": job["shortid"],
                   "round": job["round"], "ok": ok, "joinid": jid, "msg": msg,
                   "secs": round(time.time() - t0, 1)}
            print(f"{job['tag']} {'成功' if ok else '失败'} "
                  f"{('joinid=' + jid) if jid else msg}  {row['secs']}s", flush=True)
            stats.add(row)

    threads = [threading.Thread(target=worker, args=(i,), daemon=True)
               for i in range(min(cfg["workers"], len(jobs)))]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=3600)


# ---------------------------------------------------------------- main
def main():
    # 统一成 UTF-8：子进程的输出会被按 UTF-8 读，不一致就会乱码
    from console import force_utf8
    force_utf8()
    ap = argparse.ArgumentParser(description="并行作答执行器")
    ap.add_argument("--mode", choices=["context", "process"], default="context",
                    help="context=单进程多会话(省内存) process=多进程(真并行)")
    ap.add_argument("--shortid", help="单份问卷短链")
    ap.add_argument("--count", type=int, default=1)
    ap.add_argument("--job", action="append", default=[],
                    help="多份问卷，格式 短链:轮数，可重复")
    ap.add_argument("--answers", default="", help="单份问卷的答案 JSON")
    ap.add_argument("--answers-dir", default=HERE)
    ap.add_argument("--workers", type=int, default=0,
                    help="并发数；context 模式是同时打开的页面数，process 模式是进程数")
    ap.add_argument("--slow", type=float, default=0.5)
    ap.add_argument("--retry", type=int, default=1)
    ap.add_argument("--submit-timeout", type=int, default=60)
    ap.add_argument("--headless", action="store_true")
    ap.add_argument("--report", default=os.path.join(HERE, "parallel_run.json"))
    args = ap.parse_args()

    # ---------- 任务清单
    jobs_spec = []
    if args.shortid:
        jobs_spec.append((normalize_shortid(args.shortid), args.count))
    for j in args.job:
        sid, _, n = j.partition(":")
        jobs_spec.append((normalize_shortid(sid), int(n or 1)))
    if not jobs_spec:
        sys.exit("需要 --shortid 或至少一个 --job")

    answers_map = {}
    if args.shortid and args.answers:
        answers_map[normalize_shortid(args.shortid)] = json.load(
            open(args.answers, encoding="utf-8"))
    for sid, _ in jobs_spec:
        if sid in answers_map:
            continue
        p = os.path.join(args.answers_dir, f"{sid}.json")
        if os.path.exists(p):
            answers_map[sid] = json.load(open(p, encoding="utf-8"))
    missing = [s for s, _ in jobs_spec if s not in answers_map]
    if missing:
        sys.exit(f"缺答案文件：{missing}（放到 {args.answers_dir}/{{短链}}.json）")

    # ---------- 并发数
    workers = args.workers
    if not workers:
        try:
            rep = json.load(open(os.path.join(HERE, "parallel_report.json"),
                                encoding="utf-8"))
            workers = max(1, int(rep.get("best") or 2))
            print(f"并发数取自探测报告：{workers}")
        except Exception:
            workers = 2
            print("没找到探测报告，并发默认 2（建议先跑 probe_parallel.py）")

    jobs = build_jobs(jobs_spec, answers_map)
    if args.mode == "process":
        workers = min(workers, 6)      # 每份约 450MB，别把机器压死
    print(f"模式 {args.mode} | {len(jobs_spec)} 份问卷 / {len(jobs)} 个任务 | 并发 {workers}")

    # ---------- 解析问卷（每份一次）
    surveys = {}
    for sid, _ in jobs_spec:
        if sid in surveys:
            continue
        print(f"解析 {sid} …", flush=True)
        surveys[sid] = load_survey(sid)
    for job in jobs:
        job["survey"] = surveys[job["shortid"]]

    cfg = {"workers": workers, "slow": args.slow, "retry": args.retry,
           "submit_timeout": args.submit_timeout, "headless": args.headless}
    stats = Stats()

    t0 = time.time()
    if args.mode == "context":
        run_context_mode(jobs, cfg, stats)
    else:
        run_process_mode(jobs, cfg, stats)
    total_s = time.time() - t0

    ok, fail, rows = stats.summary()
    print(f"\n完成：成功 {ok} · 失败 {fail} · 用时 {total_s:.0f}s")
    for r in rows:
        print(f"  {r['tag']:<20} {'OK  ' if r['ok'] else 'FAIL'} "
              f"{(r.get('joinid') or r.get('msg') or '')[:44]}  {r['secs']}s")

    json.dump({"mode": args.mode, "workers": workers,
               "elapsed": round(total_s, 1), "ok": ok, "fail": fail,
               "rows": rows, "ts": time.strftime("%Y-%m-%d %H:%M:%S")},
              open(args.report, "w", encoding="utf-8"), ensure_ascii=False, indent=2)
    print(f"\n报告已写 {args.report}")


if __name__ == "__main__":
    main()
