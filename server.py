#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
server.py —— 问卷星批量答题器（Web 版后端）

零第三方依赖：http.server + threading 起本地服务，浏览器做界面。
提交逻辑复用 wjx_autofill.py（握手 / 签名 / 提交），parse_wjx.py 负责解析。

启动：
    python server.py                 # 默认 http://127.0.0.1:8765
    python server.py --port 9000 --no-browser
"""

import argparse
import copy
import hashlib
import json
import mimetypes
import os
import random
import re
import sys
import threading
import time
import urllib.parse
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from parse_wjx import parse_html  # noqa: E402
from humanize import fire_beacons, reading_time, warm_up  # noqa: E402
from survey_text import format_survey  # noqa: E402
from choice_weights import (  # noqa: E402
    CHOICE_TYPES,
    build_round_samplers,
    config_stats,
    empty_store,
    load_store,
    preview_question,
    save_store,
    validate_config,
)
from wjx_autofill import (  # noqa: E402
    Session,
    build_submitdata,
    fetch_meta,
    normalize_shortid,
    random_answers,
    submit_once,
    validate_answers,
)

HERE = os.path.dirname(os.path.abspath(__file__))
WEB_DIR = os.path.join(HERE, "web")
SURVEY_PATH = os.path.join(HERE, "survey.json")
FILL_TPL_PATH = os.path.join(HERE, "fill_template.json")
WEIGHT_PATH = os.path.join(HERE, "choice_weights.json")

# ---------------------------------------------------------------- 项目信息
# 单文件版与散装版共用这一份，前端弹窗从 /api/about 取，不在页面里写死多份
APP_NAME = "问卷星批量作答控制台"
APP_VERSION = "1.3.0"
AUTHOR = "黄智皓"
AUTHOR_NOTE = "本项目完全免费，禁止商用和学术造假"
ABOUT = {
    "app": APP_NAME,
    "version": APP_VERSION,
    "author": AUTHOR,
    "free": True,
    "notice": AUTHOR_NOTE,
}

LOCK = threading.Lock()
# 概率配置的落盘锁：HTTP 线程可能同时在保存不同轮次，不能只靠改内存的那把锁
WEIGHT_LOCK = threading.Lock()

# 静态资源 MIME 表（不依赖 Windows 注册表）
EXT_TYPES = {
    ".html": "text/html; charset=utf-8",
    ".css": "text/css; charset=utf-8",
    ".js": "application/javascript; charset=utf-8",
    ".json": "application/json; charset=utf-8",
    ".svg": "image/svg+xml",
    ".png": "image/png",
    ".ico": "image/x-icon",
}


class State:
    """全局运行状态。所有读写都过 LOCK，HTTP 线程与批量线程共享。"""

    def __init__(self):
        self.survey = None
        self.shortid = ""
        self.running = False
        self.paused = False
        self.stop_event = threading.Event()
        self.pause_event = threading.Event()
        self.round = 0
        self.total = 0
        self.stage = "空闲"
        self.ok = 0
        self.fail = 0
        self.records = []
        self.logs = []
        self.log_seq = 0
        self.cheat_streak = 0   # 连续被风控(7)拦截的次数，成功一轮清零
        self.proc = None        # 浏览器子进程句柄，停止时直接 terminate
                                # （验证码等待期间子进程不产出 stdout，只靠 stop_event 停不下来）

        # 一键运行：步骤状态机
        self.quick_running = False
        self.quick_steps = [
            {"key": "env", "label": "检查环境", "state": "pending", "detail": ""},
            {"key": "parse", "label": "解析问卷", "state": "pending", "detail": ""},
            {"key": "answer", "label": "生成答案", "state": "pending", "detail": ""},
            {"key": "submit", "label": "提交答卷", "state": "pending", "detail": ""},
        ]
        self.quick_result = None

        # 浏览器模式的实时状态，供前端画进度
        self.browser_state = {
            "running": False,
            "phase": "idle",      # idle/opening/filling/submitting/captcha/done
            "current": 0,
            "total": 0,
            "round": 0,
            "rounds": 0,
            "qtext": "",
            "captcha": False,
            "captcha_mode": "",   # auto=脚本正在点  manual=需要人工点
        }

        # 填空题模板：{topic: [候选1, 候选2, ...]}，空列表表示该题不走模板
        self.fill_template = {}
        self.fill_random = False      # True=每轮从候选里随机取，False=每轮取候选[0]
        self.fill_tpl_sig = ""        # 模板属于哪份问卷，换问卷后用来提示失效
        # 选择题「选完选项再补充填写」的候选：{topic: {选项值: [候选文本]}}
        self.fill_option_texts = {}

        # 选择题概率配置：按「轮次 → 题号 → 配置」存，结构见 choice_weights.py
        self.choice_weights = empty_store()

    def log(self, msg, level="dim"):
        with LOCK:
            self.log_seq += 1
            self.logs.append({
                "id": self.log_seq,
                "t": time.strftime("%H:%M:%S"),
                "msg": msg,
                "level": level,
            })
            # 只留最近 500 条，前端按 id 增量取
            if len(self.logs) > 500:
                self.logs = self.logs[-500:]

    def set_step(self, key, state, detail=None):
        with LOCK:
            for s in self.quick_steps:
                if s["key"] == key:
                    s["state"] = state
                    if detail is not None:
                        s["detail"] = detail
                    break

    def reset_steps(self):
        with LOCK:
            for s in self.quick_steps:
                s["state"] = "pending"
                s["detail"] = ""
            self.quick_result = None

    def quick_snapshot(self):
        with LOCK:
            return {
                "running": self.quick_running,
                "steps": [dict(x) for x in self.quick_steps],
                "result": self.quick_result,
            }

    def snapshot(self, since=0):
        with LOCK:
            return {
                "running": self.running,
                "paused": self.paused,
                "round": self.round,
                "total": self.total,
                "stage": self.stage,
                # 字段名避开 ok：外层响应已用 ok 表示调用是否成功
                "success": self.ok,
                "fail": self.fail,
                "pct": round(self.round / self.total * 100, 1) if self.total else 0,
                "logs": [x for x in self.logs if x["id"] > since],
                "shortid": self.shortid,
                "title": self.survey["title"] if self.survey else "",
            }


S = State()


def load_fill_template():
    """加载填空题模板。结构：{"random": false, "sig": "...", "items": {"5": ["北京","上海"]}}"""
    if not os.path.exists(FILL_TPL_PATH):
        return
    try:
        data = json.load(open(FILL_TPL_PATH, encoding="utf-8"))
        items = {}
        for k, v in (data.get("items") or {}).items():
            vals = [str(x).strip() for x in (v if isinstance(v, list) else [v]) if str(x).strip()]
            if vals:
                items[str(k)] = vals
        option_texts = {}
        for k, per in (data.get("option_texts") or {}).items():
            if not isinstance(per, dict):
                continue
            cleaned = {}
            for val, cands in per.items():
                arr = [str(x).strip() for x in (cands if isinstance(cands, list) else [cands])
                       if str(x).strip()]
                if arr:
                    cleaned[str(val)] = arr
            if cleaned:
                option_texts[str(k)] = cleaned
        with LOCK:
            S.fill_template = items
            S.fill_random = bool(data.get("random"))
            S.fill_tpl_sig = data.get("sig") or ""
            S.fill_option_texts = option_texts
    except Exception as e:
        S.log(f"读取 fill_template.json 失败：{e}", "err")


def save_fill_template():
    data = {
        "sig": S.fill_tpl_sig,
        "random": S.fill_random,
        "items": S.fill_template,
        "option_texts": S.fill_option_texts,
    }
    tmp = FILL_TPL_PATH + ".tmp"
    json.dump(data, open(tmp, "w", encoding="utf-8"), ensure_ascii=False, indent=2)
    os.replace(tmp, FILL_TPL_PATH)     # 原子替换，写一半断电不会留下坏文件


def load_weights():
    """加载选择题概率配置。文件缺失/损坏都回落空配置，不影响服务启动。"""
    store = load_store(WEIGHT_PATH)
    with LOCK:
        S.choice_weights = store
    if (store.get("rounds") or {}):
        st = config_stats(store)
        S.log(f"已加载选项概率配置：{st['pairs']} 处 / {len(st['configured_rounds'])} 个轮次", "dim")


def weights_snapshot():
    with LOCK:
        return copy.deepcopy(S.choice_weights)


def weights_commit(store):
    """落盘后替换内存。先写文件再改内存：写失败时内存里还是旧配置，不会出现
    「界面显示保存成功、重启后丢配置」这种两套状态不一致。"""
    with WEIGHT_LOCK:
        save_store(WEIGHT_PATH, store)
    with LOCK:
        S.choice_weights = store


def load_survey_file():
    if not os.path.exists(SURVEY_PATH):
        return None
    try:
        data = json.load(open(SURVEY_PATH, encoding="utf-8"))
        S.survey = data
        if not S.shortid:
            url = data.get("source_url", "")
            if "/vm/" in url:
                S.shortid = url.split("/vm/")[-1].split(".")[0]
        return data
    except Exception as e:
        S.log(f"读取 survey.json 失败：{e}", "err")
        return None


# ---------------------------------------------------------------- 批量执行
def _interruptible_wait(seconds, prefix):
    """可被停止/暂停打断的倒计时，每秒刷新一次 stage。"""
    end = time.time() + seconds
    while True:
        if S.stop_event.is_set():
            return False
        while S.pause_event.is_set():
            if S.stop_event.is_set():
                return False
            time.sleep(0.2)
        left = end - time.time()
        if left <= 0:
            return True
        with LOCK:
            S.stage = f"{prefix} · 剩余 {int(left) + 1}s"
        time.sleep(min(1.0, left))


def _wait_if_paused():
    while S.pause_event.is_set():
        if S.stop_event.is_set():
            return False
        time.sleep(0.2)
    return not S.stop_event.is_set()


def batch_worker(answers, count, gap, think, proxy, shortid, survey, samplers=None,
                 option_pools=None, manual_texts=None):
    text_bags = {}
    try:
        for i in range(1, count + 1):
            if not _wait_if_paused():
                break

            # 每轮提交前按该轮的概率配置重新抽选择题。
            # samplers 为空时 round_answers 就是原答案，行为与旧版一致。
            round_answers = dict(answers)
            for t, smp in (samplers or {}).get(i, {}).items():
                round_answers[t] = smp.draw()
            round_texts = resolve_option_texts(
                survey, round_answers, option_pools, manual_texts,
                S.fill_random, text_bags)

            with LOCK:
                S.round = i - 1
                S.stage = f"第 {i}/{count} 轮 · 建立会话"

            sess = Session(proxy=proxy)
            try:
                meta = fetch_meta(sess, shortid)
                if getattr(sess, "human", False):
                    # 先按浏览器顺序把页面静态资源拉一遍，补上真人才会产生的请求
                    warm_up(sess, meta["html"], meta["url"])
                    fire_beacons(sess, meta["html"], meta["url"])
            except Exception as e:
                with LOCK:
                    S.fail += 1
                S.log(f"第 {i}/{count} 轮 抓页失败 {type(e).__name__}: {e}", "err")
                if i < count and gap > 0:
                    if not _interruptible_wait(gap, f"第 {i}/{count} 轮完成 · 轮间隔"):
                        break
                continue

            wait_s = think if think > 0 else (
                reading_time(len(survey["questions"]))
                if getattr(sess, "human", False) else random.randint(5, 12)
            )
            if not _interruptible_wait(wait_s, f"第 {i}/{count} 轮 · 作答中"):
                break

            with LOCK:
                S.stage = f"第 {i}/{count} 轮 · 提交中"

            try:
                r = submit_once(sess, shortid, survey, round_answers,
                                think_seconds=0, verbose=False,
                                meta=meta, elapsed=wait_s,
                                option_texts=round_texts)
            except Exception as e:
                with LOCK:
                    S.fail += 1
                S.log(f"第 {i}/{count} 轮 请求异常 {type(e).__name__}: {e}", "err")
                if i < count and gap > 0:
                    if not _interruptible_wait(gap, f"第 {i}/{count} 轮完成 · 轮间隔"):
                        break
                continue

            code = r.get("code")
            with LOCK:
                S.round = i
                if code == 10:
                    S.ok += 1
                    raw = r.get("raw", "")
                    joinid = raw.split("joinid=")[1].split("&")[0] if "joinid=" in raw else ""
                    S.records.append({"round": i, "code": 10, "joinid": joinid,
                                      "ktimes": r.get("ktimes"), "time": time.strftime("%H:%M:%S")})
                else:
                    S.fail += 1
                    S.records.append({"round": i, "code": code,
                                      "raw": r.get("raw", "")[:120],
                                      "time": time.strftime("%H:%M:%S")})

            if code == 10:
                with LOCK:
                    S.cheat_streak = 0
                joinid = S.records[-1].get("joinid", "")
                S.log(f"第 {i}/{count} 轮 成功  joinid={joinid or '-'}  "
                      f"ktimes={r.get('ktimes')}", "ok")
            elif code == 7:
                # 安全校验：同 IP 高频提交触发的风控。原地重试只会加深标记，
                # 必须按倍数退避；连续 3 次就中止，等换出口再继续。
                with LOCK:
                    S.cheat_streak += 1
                    streak = S.cheat_streak
                if streak >= 3:
                    S.log(f"连续 {streak} 次被安全校验(7)拦截，任务中止。"
                          f"建议换代理出口并冷却 30 分钟后再试", "err")
                    break
                backoff = 180 * streak
                S.log(f"第 {i}/{count} 轮 触发安全校验(7)，第 {streak} 次，"
                      f"退避 {backoff // 60} 分钟后继续", "warn")
                if not _interruptible_wait(backoff, "风控退避中"):
                    break
                continue
            elif code == 22:
                S.log(f"第 {i}/{count} 轮 触发验证码(22)，建议换代理并放慢节奏", "err")
            else:
                S.log(f"第 {i}/{count} 轮 失败 code={code} {r.get('meaning', '')} "
                      f"{r.get('raw', '')[:80]}", "err")

            if i < count and gap > 0:
                if not _interruptible_wait(gap, f"第 {i}/{count} 轮完成 · 轮间隔"):
                    break
            elif i < count and not _wait_if_paused():
                break
    finally:
        with LOCK:
            S.running = False
            S.paused = False
            S.stage = f"已结束 · 成功 {S.ok} · 失败 {S.fail}"
            ok, fail = S.ok, S.fail
        S.log(f"批量结束：成功 {ok}，失败 {fail}",
              "ok" if fail == 0 else "warn")


# ---------------------------------------------------------------- 一键运行
def check_deps():
    """返回缺失的必需依赖包名。

    二维码解码不在这里：它只在「上传图片」这条路上用，缺了应当降级而不是拦住整个流程。
    真要用时由 qr_decode 自己抛带安装命令的错误。
    """
    missing = []
    for mod, pkg in (("bs4", "beautifulsoup4"), ("lxml", "lxml")):
        try:
            __import__(mod)
        except ImportError:
            missing.append(pkg)
    return missing


def decode_qr_base64(data_url: str) -> str:
    """从 data:image/...;base64,xxx 里解出二维码内容。

    解码后端统一交给 qr_decode：优先 zxing-cpp，回退 OpenCV。
    """
    import base64

    from qr_decode import decode_bytes

    raw = base64.b64decode(data_url.split(",")[-1])
    return decode_bytes(raw)


def run_browser_engine(shortid, survey, rounds, count, gap, proxy, headless, slow,
                       round_texts=None):
    """用真浏览器跑批量。子进程执行 wjx_browser.py，stdout 逐行转成日志。

    rounds 是每轮一份的完整答案列表，length 与 count 对齐；
    round_texts 是对齐的联动补充文本 {"题号": {"选项值": "文本"}}。
    子进程按轮次索引取，所以「每轮填空不同」「每轮补充文本不同」
    由这里决定，而不是在浏览器里现抽 —— 这样日志里能看到每轮到底填了什么。
    """
    import subprocess

    ans_path = os.path.join(HERE, "_answers_run.json")
    json.dump({"rounds": rounds, "option_texts": round_texts or []},
              open(ans_path, "w", encoding="utf-8"), ensure_ascii=False)

    url = f"https://v.wjx.cn/vm/{shortid}.aspx"
    cmd = [sys.executable, "-u", os.path.join(HERE, "wjx_browser.py"),
           "--url", url,
           "--survey", SURVEY_PATH,
           "--answers", ans_path,
           "--count", str(count),
           "--gap", str(gap),
           "--slow", str(slow)]
    if proxy:
        cmd += ["--proxy", proxy]
    if headless:
        cmd.append("--headless")
    else:
        # 有头模式说明有人看着，验证码弹出来直接让他点一下最省事
        cmd.append("--manual-captcha")

    S.log(f"浏览器模式启动（slow={slow}，{'无头' if headless else '有头'}）", "info")
    if S.stop_event.is_set():
        return 0, 0
    total_q = len(survey.get("questions") or [])
    with LOCK:
        S.browser_state.update(running=True, phase="opening", current=0,
                               total=total_q, captcha=False, qtext="")

    ok = fail = 0
    proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                            text=True, encoding="utf-8", errors="replace", cwd=HERE)
    with LOCK:
        S.proc = proc
    try:
        for line in proc.stdout:
            line = line.rstrip()
            if not line:
                continue

            # 结构化结果行。父进程按 JSON 统计，不再匹配 dict 的 repr ——
            # 之前用 "结果 {'ok': True" 做子串匹配，格式一变统计就全错，
            # 结果「提交失败」也被算成成功。
            if line.startswith("[RESULT]"):
                try:
                    r = json.loads(line[len("[RESULT]"):].strip())
                except Exception:
                    r = {}
                with LOCK:
                    rn = S.browser_state.get("round", 0)
                    tt = S.browser_state.get("rounds", count)
                if r.get("ok"):
                    ok += 1
                    S.log(f"第 {rn}/{tt} 轮 已入库  joinid={r.get('joinid', '-')}", "ok")
                else:
                    fail += 1
                    S.log(f"第 {rn}/{tt} 轮 未成功：{str(r.get('msg', ''))[:70]}", "err")
                with LOCK:
                    S.browser_state.update(phase="done", captcha=False)
                continue

            level = "dim"
            # 「失败 0 题」也含"失败"二字，得先判掉，否则成功摘要会被刷成红色
            if "结果 {'ok': True" in line or "失败 0 题" in line \
                    or ("成功" in line and "总计" in line):
                level = "ok"
            elif "异常" in line or "失败" in line or "未通过" in line:
                level = "err"
            elif "验证码" in line:
                level = "warn"
            elif line.startswith("[第") or line.startswith("[轮间隔]"):
                level = "info"
            S.log(line, level)

            # 解析 stdout 里的进度，写进 browser_state 供前端画图
            bs = S.browser_state
            if line.startswith("[第") and "打开" in line:
                m2 = re.search(r"第 (\d+)/(\d+) 轮", line)
                with LOCK:
                    bs.update(running=True, phase="opening",
                              round=int(m2.group(1)) if m2 else 1,
                              rounds=int(m2.group(2)) if m2 else 1,
                              current=0, captcha=False)
            elif "· 点击 Q" in line:
                m2 = re.search(r"点击 Q(\d+)", line)
                if m2:
                    with LOCK:
                        bs.update(phase="filling", current=int(m2.group(1)))
            elif line.startswith("[提交]"):
                with LOCK:
                    bs.update(phase="submitting")
            elif "需要人工点击" in line:
                with LOCK:
                    bs.update(phase="captcha", captcha=True, captcha_mode="manual")
            elif "验证码" in line and ("通过" in line or "弹窗已消失" in line):
                with LOCK:
                    bs.update(phase="submitting", captcha=False, captcha_mode="")
            elif "验证码" in line:
                # 正在自动尝试过验证：先亮灯，让人知道提交被拦下了。
                # 否则界面一直显示「提交中」，用户看着像卡死。
                with LOCK:
                    bs.update(phase="captcha", captcha=True, captcha_mode="auto")
            elif "结果 {" in line:
                with LOCK:
                    bs.update(phase="done", captcha=False, current=total_q)
            elif "轮间隔" in line:
                with LOCK:
                    bs.update(phase="waiting", captcha=False)

            m = re.search(r"第 (\d+)/(\d+) 轮", line)
            if m:
                S.set_step("submit", "running", f"{m.group(1)}/{m.group(2)}")
            if S.stop_event.is_set():
                proc.terminate()
                break
    finally:
        try:
            proc.wait(timeout=3)     # terminate 后一般 1 秒内退出，等太久会让「停止」显得没反应
        except Exception:
            proc.kill()
        with LOCK:
            S.proc = None
            S.browser_state.update(running=False, captcha=False)
            # done 要保留进度（界面显示「共 N 题已提交」），其余收尾时清干净，
            # 否则下次一进来就看到上一轮的 current=1、rounds=3
            if S.browser_state["phase"] != "done":
                S.browser_state.update(phase="idle", current=0, round=0, rounds=0)
        try:
            os.remove(ans_path)
        except Exception:
            pass
    return ok, fail


def quick_worker(cfg):
    """一键运行：环境 -> 解析 -> 答案 -> 提交，每步都回写状态。"""
    from humanize import fire_beacons, reading_time, warm_up

    try:
        # ---------- ① 环境
        S.set_step("env", "running")
        S.log("① 检查运行环境", "info")
        time.sleep(0.3)
        missing = check_deps()
        if missing:
            S.set_step("env", "fail", "缺 " + "、".join(missing))
            S.log("缺少依赖：" + "、".join(missing) + "，请先 pip install", "err")
            with LOCK:
                S.quick_result = {"ok": False, "error": "缺少依赖",
                                  "missing": missing}
            return
        try:
            from humanize import status as hstatus
            fp = hstatus()
            env_detail = f"指纹 {fp['impersonate']}" if fp["curl_cffi"] else "依赖齐全（未启用浏览器指纹）"
        except Exception:
            env_detail = "依赖齐全"
        S.set_step("env", "done", env_detail)

        # ---------- ② 解析
        if S.stop_event.is_set():
            S.log("已手动停止", "warn")
            return
        S.set_step("parse", "running")
        # 三种输入都要能收敛成短链：手填短链、粘整条链接、二维码里的链接。
        # 之前这里只 strip 了一下，粘完整 URL 就会拼出畸形地址。
        shortid = normalize_shortid(cfg.get("shortid") or "")
        if not shortid and cfg.get("qr_base64"):
            S.log("识别上传的二维码…", "info")
            shortid = normalize_shortid(decode_qr_base64(cfg["qr_base64"]))
            S.log(f"二维码识出短链 {shortid}", "ok")
        if not shortid:
            raise RuntimeError("既没有填短链，也没有可识别的二维码")

        proxy = (cfg.get("proxy") or "").strip() or None
        sess = Session(proxy=proxy)
        meta = fetch_meta(sess, shortid)
        survey = parse_html(meta["html"])
        # 页面里没有稳定的短链标识，来源只能由这里补。
        # 不写的话落盘后 survey.json 的 source_url 是空的，下次启动顶栏短链就丢了。
        survey["source_url"] = f"https://v.wjx.cn/vm/{shortid}.aspx"
        with LOCK:
            S.survey = survey
            S.shortid = shortid
        json.dump(survey, open(SURVEY_PATH, "w", encoding="utf-8"),
                  ensure_ascii=False, indent=2)
        S.set_step("parse", "done",
                   f"{survey['question_count']} 题 · 必答 {survey['required_count']}")
        S.log(f"解析完成：{survey['title']}（{survey['question_count']} 题）", "ok")

        # ---------- ③ 答案
        # 停止要在这几个阶段之间也检查：子进程还没起来时只能靠这里退出
        if S.stop_event.is_set():
            S.log("已手动停止", "warn")
            return
        S.set_step("answer", "running")
        time.sleep(0.3)
        # 界面上勾的答案优先，缺的题按题型随机补齐。
        # 网页与脚本必须用同一份答案，否则「界面看到的」和「实际提交的」
        # 可以是两套内容，前后台数据就对不上了。
        given = {}
        for k, v in (cfg.get("answers") or {}).items():
            if v not in (None, "", []):
                given[str(k)] = v

        # 界面上的问卷指纹与刚解析出来的不一致 → 旧答案整份作废。
        # 只比题号不够：换问卷后 Q5 仍叫 Q5，选项却整组换了，会静默错位。
        ui_sig = (cfg.get("survey_sig") or "").strip()
        cur_sig = survey_sig(survey)
        stale = bool(given) and bool(ui_sig) and ui_sig != cur_sig
        if stale:
            S.log(f"界面答案来自另一份问卷（{ui_sig} ≠ {cur_sig}），已全部弃用", "warn")
            given = {}

        # 模板同理：换了问卷后 Q5 还是 Q5，但题干完全可能是另一道题，
        # 把旧问卷的「北京/上海」填进新问卷的 Q5 是静默错位，比报错更糟
        tpl = S.fill_template
        otpl = S.fill_option_texts
        if (tpl or otpl) and S.fill_tpl_sig and S.fill_tpl_sig != cur_sig:
            S.log(f"填空题模板属于另一份问卷（{S.fill_tpl_sig} ≠ {cur_sig}），"
                  f"本次不套用，请在模板面板里核对后重新保存", "warn")
            tpl = {}
            otpl = {}

        # 界面上给联动选项手填的补充文本。模板优先；模板没配到该选项时，
        # 这里手填的值才会被用上。
        manual_texts = {}
        for k, per in (cfg.get("option_texts") or {}).items():
            if not isinstance(per, dict):
                continue
            cleaned = {}
            for v, text in per.items():
                if isinstance(text, str) and text.strip():
                    cleaned[str(v)] = text.strip()
            if cleaned:
                manual_texts[str(k)] = cleaned
        if stale:
            manual_texts = {}

        answers, used, dropped = merge_answers(survey, given)
        # 模板里的填空值要在校验之前注入，否则必答填空会被判成「未提供」
        _pre, _, ftopics, _pre_texts = build_round_answers(
            survey, answers, tpl, False, 1, option_pools=otpl,
            manual_texts=manual_texts)
        answers = _pre[0]
        problems = validate_answers(answers, survey)
        if problems:
            S.set_step("answer", "fail", problems[0][:40])
            S.log("答案校验失败：" + "；".join(problems[:2]), "err")
            with LOCK:
                S.quick_result = {"ok": False, "error": "答案校验失败",
                                  "problems": problems, "sig": cur_sig}
            return
        total_q = len(survey["questions"])
        multi = sum(1 for q in survey["questions"] if q["type_name"] == "多选")
        tpl_n = len(ftopics)
        if used == 0 and not tpl_n:
            detail = f"随机生成 {len(answers)} 题"
            S.log(f"未采用界面答案，随机生成 {len(answers)} 题（多选 {multi} 道）", "ok")
        elif used < total_q:
            detail = f"界面 {used} 题 + 补齐 {total_q - used} 题"
            if tpl_n:
                detail += f"（含模板 {tpl_n} 题）"
            S.log(f"沿用界面答案 {used} 题，其余 {total_q - used} 题随机补齐", "ok")
        else:
            detail = f"沿用界面答案 {len(answers)} 题"
            S.log(f"沿用界面答案：{len(answers)} 题，多选 {multi} 道", "ok")
        if dropped:
            S.log(f"有 {len(dropped)} 题（Q{'、Q'.join(dropped[:6])}）与当前问卷不符，已改用随机值", "warn")
        S.set_step("answer", "done", detail)

        # ---------- ④ 提交
        count = max(1, int(cfg.get("count", 1)))
        gap = max(0, int(cfg.get("gap", 60)))
        think = int(cfg.get("think", 0))
        engine = (cfg.get("engine") or "browser").lower()

        # 两个引擎共用同一份展开结果：选择题重抽 + 填空按模板
        choice_random = bool(cfg.get("choice_random"))
        # 概率配置在服务端重新读盘校验，不信任前端传来的配置：
        # 换过问卷、题目选项变过、手工改过 JSON，都在这里被拦下。
        wstore = weights_snapshot()
        samplers, wissues = build_round_samplers(survey, wstore, cur_sig, count)
        for msg in wissues[:6]:
            S.log(f"概率配置：{msg}", "warn")
        if len(wissues) > 6:
            S.log(f"概率配置：另有 {len(wissues) - 6} 条提示未显示", "dim")
        if samplers:
            pairs = sum(len(v) for v in samplers.values())
            rounds_desc = "、".join(f"第{r}轮" for r in sorted(samplers)[:6])
            S.log(f"选项概率配置生效：{pairs} 处 · {rounds_desc}"
                  f"{'…' if len(samplers) > 6 else ''}", "info")
            for r in sorted(samplers)[:3]:
                S.log(f"  第 {r} 轮 " + "、".join(
                    f"Q{t}" for t in sorted(samplers[r], key=lambda x: int(x))), "dim")
        extra_rounds = sorted(set(config_stats(wstore)["configured_rounds"])
                              - set(range(1, count + 1)))
        if extra_rounds:
            S.log(f"第 {'、'.join(str(x) for x in extra_rounds[:8])} 轮的配置"
                  f"超出本次 {count} 轮，不会生效", "warn")
        rounds, trace, ftopics, round_texts = build_round_answers(
            survey, answers, tpl, S.fill_random, count, choice_random, samplers,
            option_pools=otpl, manual_texts=manual_texts)

        if choice_random:
            kinds = {}
            for q in survey["questions"]:
                if q["type_name"] in ("单选", "多选", "矩阵量表"):
                    kinds[q["type_name"]] = kinds.get(q["type_name"], 0) + 1
            desc = "、".join(f"{k} {v} 道" for k, v in kinds.items())
            S.log(f"选择题每轮重抽：{desc}（答题区勾选仅供本轮参考）", "info")

        if ftopics:
            mode = "每轮随机" if S.fill_random else "每轮固定取首项"
            S.log(f"填空题模板生效（{mode}）：{len(ftopics)} 题", "info")
            for i, picked in enumerate(trace[:5], 1):
                desc = "、".join(f"Q{t}={picked[t]}" for t in ftopics)
                S.log(f"  第 {i} 轮 {desc}", "dim")
            if count > 5:
                S.log(f"  …共 {count} 轮，此处只列前 5 轮", "dim")

        linked_total = sum(len(x) for x in round_texts)
        if linked_total:
            mode = "每轮随机" if S.fill_random else "每轮固定取首项"
            S.log(f"联动选项补充文本生效（{mode}）：{linked_total} 处", "info")
            for i, txts in enumerate(round_texts[:3], 1):
                desc = "、".join(
                    f"Q{t}.{v}=「{text[:18]}」"
                    for t, per in txts.items() for v, text in per.items())
                S.log(f"  第 {i} 轮 {desc}", "dim")
            if count > 3:
                S.log(f"  …共 {count} 轮，此处只列前 3 轮", "dim")
        elif any(q.get("has_option_text") for q in survey["questions"]):
            S.log("这份问卷有联动补充题，但本轮没解析出补充文本，"
                  "请到「填空题模板 → 联动补充文本」里配置", "warn")

        if engine == "browser":
            S.set_step("submit", "running", f"0/{count}")
            ok_n, fail_n = run_browser_engine(
                shortid, survey, rounds, count, gap, proxy,
                bool(cfg.get("headless", False)),
                float(cfg.get("slow", 0.55)), round_texts)
            stopped = S.stop_event.is_set()
            S.set_step("submit", "done" if fail_n == 0 and not stopped else "fail",
                       f"成功 {ok_n} · 失败 {fail_n}")
            with LOCK:
                res = {"ok": ok_n > 0, "success": ok_n, "fail": fail_n,
                       "title": survey["title"], "shortid": shortid,
                       # 回填第 1 轮实际提交的内容，而不是展开前那份基准
                       "answers": rounds[0] if rounds else answers,
                       "option_texts": round_texts[0] if round_texts else {},
                       "engine": "browser", "sig": cur_sig}
                if stopped and ok_n == 0:
                    # 区分「被停下」和「跑失败了」：前者不是错误，只是没跑完
                    res.update(ok=False, error="已手动停止", stopped=True)
                S.quick_result = res
            S.log(f"一键运行结束（浏览器）：成功 {ok_n}，失败 {fail_n}",
                  "ok" if fail_n == 0 else "warn")
            return

        S.set_step("submit", "running", f"0/{count}")

        ok_n = fail_n = 0
        streak = 0
        for i in range(1, count + 1):
            if S.stop_event.is_set():
                S.log("已手动停止", "warn")
                break
            S.set_step("submit", "running", f"{i - 1}/{count}")
            S.log(f"第 {i}/{count} 轮：提交中", "info")

            s2 = Session(proxy=proxy)
            try:
                m2 = fetch_meta(s2, shortid)
                if getattr(s2, "human", False):
                    warm_up(s2, m2["html"], m2["url"])
                    fire_beacons(s2, m2["html"], m2["url"])
                wait = think if think > 0 else reading_time(len(survey["questions"]))
                end = time.time() + wait
                while time.time() < end and not S.stop_event.is_set():
                    left = int(end - time.time())
                    S.set_step("submit", "running", f"第 {i}/{count} 轮 · 作答 {left}s")
                    time.sleep(1)
                r = submit_once(s2, shortid, survey,
                                rounds[i - 1] if rounds else answers, meta=m2,
                                think_seconds=0, verbose=False,
                                option_texts=(round_texts[i - 1]
                                              if i - 1 < len(round_texts) else None))
            except Exception as e:
                fail_n += 1
                S.log(f"第 {i}/{count} 轮 异常 {type(e).__name__}: {str(e)[:80]}", "err")
                if i < count and gap:
                    time.sleep(gap)
                continue

            if r["code"] == 10:
                ok_n += 1
                streak = 0
                jid = r["raw"].split("joinid=")[1].split("&")[0] if "joinid=" in r["raw"] else "-"
                S.log(f"第 {i}/{count} 轮 成功  joinid={jid}", "ok")
            else:
                fail_n += 1
                S.log(f"第 {i}/{count} 轮 code={r['code']} {r.get('meaning', '')}", "err")
                if r["code"] == 22:
                    # captchaVerifyParam 只能由页面 SDK 在浏览器里生成，HTTP 侧伪造不出来
                    S.log("验证码只能由浏览器生成，建议用「浏览器模式」提交这一条", "warn")
                if r["code"] == 7:
                    streak += 1
                    if streak >= 3:
                        S.log("连续 3 次被安全校验拦截，已中止", "err")
                        break
                    back = 180 * streak
                    S.log(f"退避 {back // 60} 分钟", "warn")
                    for _ in range(back):
                        if S.stop_event.is_set():
                            break
                        time.sleep(1)

            S.set_step("submit", "running", f"{i}/{count}")
            if i < count and gap and not S.stop_event.is_set():
                for _ in range(gap):
                    if S.stop_event.is_set():
                        break
                    time.sleep(1)

        state = "done" if fail_n == 0 else "fail"
        S.set_step("submit", state, f"成功 {ok_n} · 失败 {fail_n}")
        with LOCK:
            S.quick_result = {"ok": ok_n > 0, "success": ok_n, "fail": fail_n,
                              "title": survey["title"], "shortid": shortid,
                              "answers": rounds[0] if rounds else answers,
                              "option_texts": round_texts[0] if round_texts else {},
                              "sig": cur_sig}   # 回填第 1 轮实际内容
        S.log(f"一键运行结束：成功 {ok_n}，失败 {fail_n}",
              "ok" if fail_n == 0 else "warn")

    except Exception as e:
        # RuntimeError / ValueError 的消息是给用户看的，前面不需要挂类型名
        brief = str(e) if isinstance(e, (RuntimeError, ValueError)) \
            else f"{type(e).__name__}: {e}"
        S.log(f"一键运行中断：{brief}", "err")
        with LOCK:
            for st in S.quick_steps:
                if st["state"] == "running":
                    st["state"] = "fail"
                    st["detail"] = brief[:40]
            S.quick_result = {"ok": False, "error": brief}
    finally:
        with LOCK:
            # 被手动停下时不写结果的话，前端会回到「未开始」，看着像没跑过
            if S.stop_event.is_set() and not S.quick_result:
                S.quick_result = {"ok": False, "error": "已手动停止", "stopped": True}
            # 关键：异常可能发生在浏览器子进程启动之前（环境检查/解析/生成答案），
            # 那时 browser_state 已被 _api_quick_start 置为 running=True & opening，
            # 而没有 run_browser_engine 的 finally 来复位。漏掉这两行，
            # 前端 busy 判定就永远是 true —— 界面卡在「正在打开浏览器」且停不下来。
            S.browser_state.update(running=False, captcha=False, captcha_mode="")
            if S.browser_state["phase"] != "done":
                S.browser_state.update(phase="idle", current=0, round=0, rounds=0)
            S.proc = None
            S.quick_running = False


# ---------------------------------------------------------------- HTTP
class Handler(BaseHTTPRequestHandler):
    server_version = "WjxBatch/1.0"

    def log_message(self, fmt, *args):
        pass  # 静音默认访问日志，避免刷屏

    # ---------- 工具
    def _json(self, obj, code=200):
        body = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _body(self):
        n = int(self.headers.get("Content-Length") or 0)
        if not n:
            return {}
        raw = self.rfile.read(n)
        try:
            return json.loads(raw.decode("utf-8"))
        except Exception:
            return {}

    def _file(self, rel):
        path = os.path.normpath(os.path.join(WEB_DIR, rel.lstrip("/")))
        if not path.startswith(WEB_DIR) or not os.path.isfile(path):
            self.send_error(404, "Not Found")
            return
        # Windows 注册表会把 .js 映射成 text/plain，这里显式指定，避免浏览器拒绝执行
        ext = os.path.splitext(path)[1].lower()
        ctype = EXT_TYPES.get(ext) or mimetypes.guess_type(path)[0] or "application/octet-stream"
        if ctype.startswith("text/") and "charset" not in ctype:
            ctype += "; charset=utf-8"
        data = open(path, "rb").read()
        self.send_response(200)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(data)

    # ---------- GET
    def do_GET(self):
        p = urllib.parse.urlparse(self.path)
        path = p.path
        qs = urllib.parse.parse_qs(p.query)

        if path == "/":
            return self._file("index.html")
        if path == "/api/survey":
            if S.survey is None:
                load_survey_file()
            sig = survey_sig(S.survey) if S.survey else ""
            return self._json({"ok": True, "survey": S.survey, "shortid": S.shortid,
                               "sig": sig})
        if path == "/api/batch/status":
            since = int(qs.get("since", ["0"])[0])
            return self._json({"ok": True, **S.snapshot(since)})
        if path == "/api/quick/status":
            return self._json({"ok": True, **S.quick_snapshot()})
        if path == "/api/browser/status":
            with LOCK:
                return self._json({"ok": True, **S.browser_state})
        if path == "/api/fill/template":
            cur = survey_sig(S.survey) if S.survey else ""
            with LOCK:
                items = {k: list(v) for k, v in S.fill_template.items()}
                option_texts = {k: {v: list(c) for v, c in per.items()}
                                for k, per in S.fill_option_texts.items()}
                rnd = S.fill_random
                tpl_sig = S.fill_tpl_sig
            linked = sum(len(per) for per in option_texts.values())
            return self._json({
                "ok": True, "items": items, "option_texts": option_texts,
                "random": rnd, "sig": tpl_sig, "current_sig": cur,
                "linked_options": linked,
                # 模板是按旧问卷配的，题号可能已经对不上
                "stale": bool(tpl_sig and cur and tpl_sig != cur),
            })
        if path == "/api/survey/export":
            # 把当前已解析的问卷整理成纯文本，前端一键复制。
            # 不重新抓页面：抓取/解析是另一个按钮的事，这里只做格式化，
            # 避免复制动作把正在作答的 survey.json 覆盖掉。
            if S.survey is None:
                load_survey_file()
            if not S.survey:
                return self._json({"ok": False, "error": "未加载问卷"}, 400)
            res = format_survey(S.survey)
            return self._json({
                "ok": True,
                "title": S.survey.get("title") or "",
                "shortid": S.shortid,
                **res,
            })
        if path == "/api/weights":
            if S.survey is None:
                load_survey_file()
            cur = survey_sig(S.survey) if S.survey else ""
            store = weights_snapshot()
            st = config_stats(store)
            return self._json({
                "ok": True,
                "sig": store.get("sig") or "",
                "current_sig": cur,
                "stale": bool(store.get("sig") and cur and store["sig"] != cur),
                "rounds": store.get("rounds") or {},
                "updated": store.get("updated") or "",
                "title": (S.survey or {}).get("title") or "",
                **st,
            })
        if path == "/api/about":
            # 前端启动弹窗的内容来源；作者与授权只在这里维护一份
            return self._json({"ok": True, **ABOUT})
        if path == "/api/echo":
            # 回显请求头，用来核对「服务端眼中的你」长什么样
            return self._json({"ok": True, "headers": dict(self.headers)})
        if path == "/api/answers/template":
            if not S.survey:
                return self._json({"ok": False, "error": "未加载问卷"}, 400)
            return self._json({"ok": True, "answers": _default_answers(S.survey)})
        if path == "/api/answers/random":
            # 每次请求都重新抽样，前端连点两次拿到的答案不同
            if not S.survey:
                return self._json({"ok": False, "error": "未加载问卷"}, 400)
            return self._json({"ok": True, "answers": random_answers(S.survey)})
        return self._file(path)

    # ---------- POST
    def do_POST(self):
        p = urllib.parse.urlparse(self.path).path
        body = self._body()

        if p == "/api/survey":
            return self._api_fetch(body)
        if p == "/api/batch/start":
            return self._api_start(body)
        if p == "/api/quick/start":
            return self._api_quick_start(body)
        if p == "/api/quick/stop":
            return self._api_quick_stop()
        if p == "/api/fill/template":
            return self._api_fill_template(body)
        if p == "/api/weights":
            return self._api_weights(body)
        if p == "/api/weights/preview":
            return self._api_weight_preview(body)
        if p == "/api/echo":
            return self._json({"ok": True, "headers": dict(self.headers)})
        if p == "/api/batch/pause":
            return self._api_pause()
        if p == "/api/batch/stop":
            return self._api_stop()
        if p == "/api/answers/preview":
            return self._api_preview(body)
        return self._json({"ok": False, "error": "not found"}, 404)

    # ---------- 业务
    # ---------- 选择题概率配置
    def _qmap(self):
        return {str(q["topic"]): q for q in (S.survey or {}).get("questions") or []}

    def _api_weights(self, body):
        """概率配置的增删改与轮次复制。

        action=set     {round, topic, config}   config 为空/clear=true 等于删除
        action=clear   {round, topic} / {round} / {all:true}
        action=copy    {from, to:[...]} 或 {from, to:"all", count:N}

        每次只改目标轮次的目标题目；其他轮次是独立对象，不会被连带修改。
        """
        if not S.survey:
            return self._json({"ok": False, "error": "未加载问卷"}, 400)

        cur_sig = survey_sig(S.survey)
        action = str(body.get("action") or "set").lower()
        qmap = self._qmap()

        store = weights_snapshot()
        rotated = bool(store.get("sig") and store.get("sig") != cur_sig)
        if store.get("sig") != cur_sig:
            # 换问卷后旧配置的题号/选项都可能对不上，整份作废比逐条猜安全
            store = empty_store()
        store["sig"] = cur_sig
        rounds = store.setdefault("rounds", {})
        warnings = []
        touched = ""

        def round_no(raw):
            try:
                n = int(raw)
            except (TypeError, ValueError):
                return None
            return n if 1 <= n <= 999 else None

        if action == "set":
            rn = round_no(body.get("round"))
            if not rn:
                return self._json({"ok": False, "error": "round 必须是 1-999 的整数"}, 400)
            topic = str(body.get("topic") or "")
            q = qmap.get(topic)
            if not q:
                return self._json({"ok": False, "error": f"Q{topic} 不在当前问卷里"}, 400)
            if q["type_name"] not in CHOICE_TYPES:
                return self._json({"ok": False,
                                   "error": f"Q{topic} 是{q['type_name']}，不支持概率配置"}, 400)

            cfg = body.get("config")
            if cfg is None or (isinstance(cfg, dict) and cfg.get("clear")):
                r = rounds.get(str(rn)) or {}
                r.pop(topic, None)
                if r:
                    rounds[str(rn)] = r
                else:
                    rounds.pop(str(rn), None)
                touched = f"第 {rn} 轮 Q{topic} 已恢复默认均匀"
            else:
                clean, errs, warns = validate_config(q, cfg, normalize=True)
                if errs:
                    return self._json({"ok": False, "error": errs[0],
                                       "errors": errs}, 400)
                rounds.setdefault(str(rn), {})[topic] = clean
                warnings.extend(warns)
                touched = f"第 {rn} 轮 Q{topic} 概率已保存"

        elif action == "clear":
            if body.get("all"):
                rounds.clear()
                touched = "已清空全部概率配置"
            else:
                rn = round_no(body.get("round"))
                if not rn:
                    return self._json({"ok": False, "error": "round 必须是 1-999 的整数"}, 400)
                topic = str(body.get("topic") or "")
                if topic:
                    r = rounds.get(str(rn)) or {}
                    r.pop(topic, None)
                    if r:
                        rounds[str(rn)] = r
                    else:
                        rounds.pop(str(rn), None)
                    touched = f"第 {rn} 轮 Q{topic} 已恢复默认均匀"
                else:
                    rounds.pop(str(rn), None)
                    touched = f"第 {rn} 轮的概率配置已清空"

        elif action == "copy":
            src = round_no(body.get("from") if body.get("from") is not None
                           else body.get("from_round"))
            if not src:
                return self._json({"ok": False, "error": "from 必须是 1-999 的整数"}, 400)
            src_cfg = rounds.get(str(src)) or {}
            if not src_cfg:
                return self._json({"ok": False,
                                   "error": f"第 {src} 轮还没有可复制的配置"}, 400)

            # 带 topic 时只复制这一题，目标轮次的其他题保持原样。
            # 不带 topic 才是整轮复制（调用方需要明确知道会覆盖目标轮）。
            topic = str(body.get("topic") or "")
            if topic:
                if topic not in src_cfg:
                    return self._json({"ok": False,
                                       "error": f"第 {src} 轮 Q{topic} 没有配置"}, 400)
                unit = {topic: src_cfg[topic]}
            else:
                unit = src_cfg

            to = body.get("to")
            targets = []
            if to == "all":
                n = round_no(body.get("count"))
                if not n:
                    return self._json({"ok": False,
                                       "error": "套用到全部轮次时 count 必须是 1-999"}, 400)
                targets = list(range(1, n + 1))
            elif isinstance(to, list):
                targets = [round_no(x) for x in to]
            elif to not in (None, ""):
                targets = [round_no(to)]
            else:
                return self._json({"ok": False, "error": "缺少 to"}, 400)

            targets = [x for x in targets if x and x != src]
            if not targets:
                return self._json({"ok": False, "error": "没有需要复制的目标轮次"}, 400)
            if len(targets) > 200:
                return self._json({"ok": False, "error": "一次最多复制到 200 个轮次"}, 400)

            for t in targets:
                if topic:
                    rounds.setdefault(str(t), {}).update(copy.deepcopy(unit))
                else:
                    rounds[str(t)] = copy.deepcopy(src_cfg)
            touched = (f"第 {src} 轮 Q{topic} 已复制到 {len(targets)} 个轮次"
                       if topic else
                       f"第 {src} 轮配置已复制到 {len(targets)} 个轮次")

        else:
            return self._json({"ok": False, "error": f"未知 action：{action}"}, 400)

        try:
            weights_commit(store)
        except Exception as e:
            return self._json({"ok": False, "error": f"写入失败：{e}"}, 500)

        st = config_stats(store)
        extra = f"，{len(warnings)} 条自动修正" if warnings else ""
        S.log(touched + extra, "ok")
        if rotated:
            S.log("问卷已更换，上一份问卷的概率配置已整体作废", "warn")
        return self._json({
            "ok": True,
            "sig": cur_sig,
            "current_sig": cur_sig,
            "stale": False,
            "rounds": store.get("rounds") or {},
            "updated": store.get("updated") or "",
            "warnings": warnings,
            **st,
        })

    def _api_weight_preview(self, body):
        """按给定配置（未保存也行）跑一次蒙特卡洛，返回目标 vs 实际分布。"""
        if not S.survey:
            return self._json({"ok": False, "error": "未加载问卷"}, 400)
        topic = str(body.get("topic") or "")
        q = self._qmap().get(topic)
        if not q:
            return self._json({"ok": False, "error": f"Q{topic} 不在当前问卷里"}, 400)
        if q["type_name"] not in CHOICE_TYPES:
            return self._json({"ok": False,
                               "error": f"Q{topic} 是{q['type_name']}，不支持概率配置"}, 400)

        cfg = body.get("config")
        if cfg is None:
            try:
                rn = int(body.get("round") or 1)
            except (TypeError, ValueError):
                rn = 1
            store = weights_snapshot()
            cfg = ((store.get("rounds") or {}).get(str(rn)) or {}).get(topic)
            if not cfg:
                return self._json({"ok": False,
                                   "error": f"第 {rn} 轮 Q{topic} 尚未配置"}, 400)

        clean, errs, warns = validate_config(q, cfg, normalize=True)
        if errs:
            return self._json({"ok": False, "error": errs[0], "errors": errs}, 400)

        samples = body.get("samples") or 800
        try:
            samples = int(samples)
        except (TypeError, ValueError):
            samples = 800
        pv = preview_question(q, clean, samples=samples,
                              seed=body.get("seed"))
        return self._json({
            "ok": True,
            "topic": topic,
            "config": clean,
            "warnings": warns,
            **pv,
        })

    def _api_fill_template(self, body):
        """保存模板。

        items        填空候选：{题号: [候选...]}
        option_texts 联动选项补充候选：{题号: {选项值: [候选...]}}
        空数组等于删掉该题/该选项。
        """
        if not S.survey:
            return self._json({"ok": False, "error": "未加载问卷"}, 400)

        raw = body.get("items") or {}
        if not isinstance(raw, dict):
            return self._json({"ok": False, "error": "items 必须是对象"}, 400)

        items = {}
        for k, v in raw.items():
            vals = [str(x).strip() for x in (v if isinstance(v, list) else [v])]
            vals = [x for x in vals if x]
            if vals:
                items[str(k)] = vals

        raw_opts = body.get("option_texts") or {}
        if not isinstance(raw_opts, dict):
            return self._json({"ok": False, "error": "option_texts 必须是对象"}, 400)

        qmap = {str(q["topic"]): q for q in S.survey.get("questions") or []}
        option_texts = {}
        for topic, per in raw_opts.items():
            topic = str(topic)
            q = qmap.get(topic)
            if not q:
                return self._json({"ok": False,
                                   "error": f"Q{topic} 不在当前问卷里"}, 400)
            if not (q.get("has_option_text") or any(
                    o.get("has_text") for o in (q.get("options") or []))):
                return self._json({"ok": False,
                                   "error": f"Q{topic} 没有联动补充选项"}, 400)
            if not isinstance(per, dict):
                return self._json({"ok": False,
                                   "error": f"Q{topic} 的 option_texts 必须是对象"}, 400)
            omap = {str(o["value"]): o for o in (q.get("options") or [])}
            cleaned = {}
            for val, cands in per.items():
                val = str(val)
                opt = omap.get(val)
                if not opt:
                    return self._json({"ok": False,
                                       "error": f"Q{topic} 没有选项 {val}"}, 400)
                if not opt.get("has_text"):
                    return self._json({"ok": False,
                                       "error": f"Q{topic} 选项 {val} 没有补充输入框"}, 400)
                arr = [str(x).strip() for x in (cands if isinstance(cands, list) else [cands])]
                arr = [x for x in arr if x]
                if arr:
                    cleaned[val] = arr
            if cleaned:
                option_texts[topic] = cleaned

        with LOCK:
            S.fill_template = items
            S.fill_option_texts = option_texts
            S.fill_random = bool(body.get("random"))
            S.fill_tpl_sig = survey_sig(S.survey)
        try:
            save_fill_template()
        except Exception as e:
            return self._json({"ok": False, "error": f"写入失败：{e}"}, 500)

        total = sum(len(v) for v in items.values())
        linked = sum(len(per) for per in option_texts.values())
        S.log(f"模板已保存：填空 {len(items)} 题 / {total} 个候选；"
              f"联动补充 {linked} 个选项，"
              f"{'每轮随机' if S.fill_random else '固定取首项'}", "ok")
        return self._json({"ok": True, "items": items,
                           "option_texts": option_texts,
                           "linked_options": linked,
                           "random": S.fill_random,
                           "sig": S.fill_tpl_sig})

    def _api_fetch(self, body):
        shortid = normalize_shortid(body.get("shortid") or "")
        if not shortid:
            return self._json({"ok": False, "error": "缺少短链"}, 400)
        proxy = (body.get("proxy") or "").strip() or None
        S.log(f"抓取 https://v.wjx.cn/vm/{shortid}.aspx", "info")
        try:
            sess = Session(proxy=proxy)
            meta = fetch_meta(sess, shortid)
            data = parse_html(meta["html"])
            data["source_url"] = f"https://v.wjx.cn/vm/{shortid}.aspx"
            with LOCK:
                S.survey = data
                S.shortid = shortid
            json.dump(data, open(SURVEY_PATH, "w", encoding="utf-8"),
                      ensure_ascii=False, indent=2)
            S.log(f"解析完成：{data['question_count']} 题，必答 {data['required_count']} 题", "ok")
            return self._json({"ok": True, "survey": data, "shortid": shortid})
        except Exception as e:
            brief = str(e) if isinstance(e, (RuntimeError, ValueError)) \
                else f"{type(e).__name__}: {e}"
            S.log(f"抓取失败：{brief}", "err")
            return self._json({"ok": False, "error": brief}, 500)

    def _api_start(self, body):
        if S.running:
            return self._json({"ok": False, "error": "任务已在运行"}, 409)
        if not S.survey:
            return self._json({"ok": False, "error": "未加载问卷"}, 400)

        answers = body.get("answers") or {}
        problems = validate_answers(answers, S.survey)
        if problems:
            return self._json({"ok": False, "error": "答案校验未通过", "problems": problems}, 400)

        try:
            count = int(body.get("count", 1))
            gap = int(body.get("gap", 60))
            think = int(body.get("think", 8))
        except Exception:
            return self._json({"ok": False, "error": "轮数/间隔必须为整数"}, 400)
        if count < 1:
            return self._json({"ok": False, "error": "提交轮数至少为 1"}, 400)
        if gap < 0 or think < 0:
            return self._json({"ok": False, "error": "间隔不能为负"}, 400)

        proxy = (body.get("proxy") or "").strip() or None

        cur_sig = survey_sig(S.survey)
        samplers, wissues = build_round_samplers(
            S.survey, weights_snapshot(), cur_sig, count)
        for msg in wissues[:4]:
            S.log(f"概率配置：{msg}", "warn")
        if samplers:
            S.log(f"选项概率配置生效：{sum(len(v) for v in samplers.values())} 处",
                  "info")

        with LOCK:
            S.stop_event.clear()
            S.pause_event.clear()
            S.running = True
            S.paused = False
            S.ok = 0
            S.fail = 0
            S.round = 0
            S.total = count
            S.stage = "启动中"
            S.records = []
            S.cheat_streak = 0

        S.log(f"开始批量提交：{count} 轮，轮间隔 {gap}s，单轮作答 "
              f"{think or '随机 5-12'}s，代理 {proxy or '直连'}", "info")
        threading.Thread(
            target=batch_worker,
            args=(answers, count, gap, think, proxy, S.shortid, S.survey, samplers,
                  S.fill_option_texts, body.get("option_texts") or {}),
            daemon=True,
        ).start()
        return self._json({"ok": True})

    def _api_quick_start(self, body):
        if S.quick_running:
            return self._json({"ok": False, "error": "任务正在进行中"}, 409)
        S.reset_steps()
        S.stop_event.clear()
        with LOCK:
            S.quick_running = True
            # 立刻进入「准备中」，否则前端在新任务的头几百毫秒里还画着上一次的结果
            S.browser_state.update(running=True, phase="opening", current=0,
                                   round=0, rounds=0, captcha=False)
        threading.Thread(target=quick_worker, args=(body,), daemon=True).start()
        return self._json({"ok": True})

    def _api_quick_stop(self):
        if not S.quick_running:
            # 兜底：任务已经因为异常结束，但 browser_state 还挂着 running=True。
            # 前端 busy = quick.running || browser.running，这种残留会让界面
            # 永远停在「正在打开浏览器」、开始按钮禁用、停止按钮却报 400。
            with LOCK:
                stuck = bool(S.browser_state.get("running"))
                if stuck:
                    S.browser_state.update(running=False, phase="idle", current=0,
                                           round=0, rounds=0, captcha=False,
                                           captcha_mode="")
            if stuck:
                S.log("已清理残留的运行状态（任务此前已异常结束）", "warn")
                return self._json({"ok": True, "cleaned": True})
            return self._json({"ok": False, "error": "当前没有运行中的任务"}, 400)

        S.stop_event.set()
        # 验证码等待期间子进程不产出 stdout，只设 stop_event 要等到超时才停，
        # 所以这里直接 terminate，让「停止」按下去就生效。
        with LOCK:
            proc = S.proc
        if proc and proc.poll() is None:
            try:
                proc.terminate()
                S.log("已终止浏览器子进程", "warn")
            except Exception:
                pass
        S.log("收到停止信号", "warn")
        return self._json({"ok": True})

    def _api_pause(self):
        if not S.running:
            return self._json({"ok": False, "error": "没有运行中的任务"}, 400)
        if S.pause_event.is_set():
            S.pause_event.clear()
            with LOCK:
                S.paused = False
            S.log("已恢复", "info")
            return self._json({"ok": True, "paused": False})
        S.pause_event.set()
        with LOCK:
            S.paused = True
        S.log("已暂停，当前等待结束后挂起", "warn")
        return self._json({"ok": True, "paused": True})

    def _api_stop(self):
        if not S.running:
            return self._json({"ok": False, "error": "没有运行中的任务"}, 400)
        S.stop_event.set()
        S.pause_event.clear()
        S.log("停止信号已发出", "warn")
        return self._json({"ok": True})

    def _api_preview(self, body):
        if not S.survey:
            return self._json({"ok": False, "error": "未加载问卷"}, 400)
        try:
            payload = build_submitdata(body.get("answers") or {}, S.survey,
                                       body.get("option_texts") or {})
            return self._json({"ok": True, "payload": payload})
        except Exception as e:
            return self._json({"ok": False, "error": str(e)}, 400)


def survey_sig(survey):
    """问卷指纹：题干 + 题型 + 选项全算进去。

    用来判断「界面上的答案还对得上现在这份问卷吗」。只比题号不够 ——
    换问卷后 Q5 还是 Q5，但选项可能整组换掉，答案会静默错位。
    """
    h = hashlib.md5()
    h.update((survey.get("title") or "").encode("utf-8"))
    for q in survey.get("questions") or []:
        h.update(f"{q['topic']}|{q['type_name']}|{q['prompt']}".encode("utf-8"))
        for o in q.get("options") or []:
            h.update(f"{o['value']}:{o['text']}".encode("utf-8"))
        if q.get("matrix"):
            h.update(("|".join(q["matrix"]["rows"])).encode("utf-8"))
    return h.hexdigest()[:12]


def _answer_ok(q, val):
    """单题答案是否落在该题的合法取值内。界面上可能残留上一份问卷的答案。"""
    kind = q["type_name"]
    if kind == "单选":
        return str(val) in {str(o["value"]) for o in q["options"]}
    if kind == "多选":
        if not isinstance(val, list) or not val:
            return False
        pool = {str(o["value"]) for o in q["options"]}
        if any(str(v) not in pool for v in val):
            return False
        hi = q.get("max_select") or len(pool)
        lo = q.get("exact_select") or q.get("min_select") or 1
        return lo <= len(val) <= min(hi, len(pool))
    if kind == "矩阵量表":
        n = len(q["matrix"]["rows"])
        top = len(q["matrix"]["scale"])
        if not isinstance(val, list) or len(val) != n:
            return False
        try:
            return all(1 <= int(v) <= top for v in val)
        except (TypeError, ValueError):
            return False
    if kind.startswith("填空"):
        # 之前这里直接落到末尾的 return False，等于把界面上填的填空答案
        # 全部判成非法丢掉，必答填空就永远「未提供」
        return isinstance(val, (str, int, float)) and str(val).strip() != ""
    if kind == "下拉":
        return str(val) in {str(o["value"]) for o in (q.get("options") or [])}
    # 不认识的题型不在这里拦，交给 validate_answers 统一判
    return True


def merge_answers(survey, given):
    """界面答案优先，缺的或非法的题随机补齐。

    为什么不直接报「答案不全」：用户勾了 15 题就点开始，期望的是
    「剩下 2 题随便」，不是被拦下来再点一次随机填充。
    非法值（换问卷后的残留）也走这条链路丢掉，避免整单失败。
    返回 (answers, 采用界面的题数, 丢弃的题号)。
    """
    base = random_answers(survey)
    out = dict(base)
    used = 0
    dropped = []
    qmap = {str(q["topic"]): q for q in survey["questions"]}
    for topic, val in given.items():
        q = qmap.get(topic)
        if not q:
            dropped.append(topic)
            continue
        if _answer_ok(q, val):
            out[topic] = val
            used += 1
        else:
            dropped.append(topic)
    return out, used, dropped


_OPT_PREFIX = re.compile(r"^\s*(?:[（(]\s*[A-Za-z]\s*[)）]|[A-Za-z])\s*[.、．:：)）]\s*")


def _plain_option(text):
    """选项文本去掉 A./B、/(C) 前缀，用作补充文本的兜底值。"""
    s = re.sub(r"\s+", " ", str(text or "")).strip()
    out = _OPT_PREFIX.sub("", s, count=1).strip()
    return out or s


def resolve_option_texts(survey, answers, pools=None, manual=None,
                         random_fill=False, bags=None):
    """给「选完选项还要补充填写」的题逐轮生成补充文本。

    优先级：
      1. 模板候选（fill_template.json 的 option_texts，可按轮次随机）
      2. 界面上针对该选项手填的文本
      3. 选项名兜底（必填时不能留空，否则提交必被打回）

    关键：这一步在选项抽样之后执行，只读取已经选中的值，不会反过来影响
    选项分布。所以概率配置的边缘概率保持不变，补充文本只是给选中的选项
    配一段说明。
    """
    pools = pools or {}
    manual = manual or {}
    bags = bags if bags is not None else {}
    out = {}

    for q in survey.get("questions") or []:
        has_linked = q.get("has_option_text") or any(
            o.get("has_text") for o in (q.get("options") or []))
        if not has_linked:
            continue
        topic = str(q["topic"])
        val = answers.get(topic)
        if val is None:
            continue
        vals = val if isinstance(val, list) else [val]
        optmap = {str(o.get("value")): o for o in (q.get("options") or [])}
        per = {}

        for v in vals:
            v = str(v)
            opt = optmap.get(v)
            if not opt or not opt.get("has_text"):
                continue

            cands = list((pools.get(topic) or {}).get(v) or [])
            text = ""
            if cands:
                if random_fill:
                    key = (topic, v)
                    bag = bags.get(key) or []
                    if not bag:
                        bag = list(cands)
                        random.shuffle(bag)
                    text = bag.pop()
                    bags[key] = bag
                else:
                    text = cands[0]
            else:
                one = (manual.get(topic) or {}).get(v)
                if isinstance(one, str) and one.strip():
                    text = one.strip()
                elif opt.get("text_required"):
                    # 必填补充框没配模板也没手填时，用选项名兜底。
                    # 宁可语义一般，也不能交一份空着的必填项。
                    text = _plain_option(opt.get("text")) or "未填写"

            if text:
                per[v] = text

        if per:
            out[topic] = per

    return out


def build_round_answers(survey, base, template, random_fill, count,
                        choice_random=False, samplers=None,
                        option_pools=None, manual_texts=None):
    """把一份基础答案展开成 count 份。

    四个随机维度互相独立：
      samplers       → 按「轮次 + 题目」的概率配置抽，优先级最高
      choice_random  → 未配置概率的选择题（单选/多选/矩阵）每轮重新抽样
      random_fill    → 填空题按模板的候选池逐轮取值
      option_pools   → 联动选项的补充文本，按选中值逐轮取候选

    补充文本在选项确定之后解析：概率只决定选哪个选项，文本不会反向
    影响选项分布；多选的目标边缘概率也不会因为补文本而改变。

    返回 (rounds, trace, used_topics, round_texts)。
    """
    topics = {str(q["topic"]) for q in survey.get("questions") or []}
    fill_topics = {
        str(q["topic"]) for q in survey.get("questions") or []
        if str(q.get("type_name") or "").startswith("填空")
    }
    pools = {}
    for t, vals in (template or {}).items():
        t = str(t)
        pool = [str(v).strip() for v in (vals if isinstance(vals, list) else [vals])
                if str(v).strip()]
        # 只认填空题：模板里万一残留了选择题题号，不能把概率抽样结果覆盖成文本
        if t in topics and t in fill_topics and pool:
            pools[t] = pool

    # 参与「每轮重抽」的题型：填空题不在其中，它走候选池
    choice_topics = {
        str(q["topic"]) for q in survey.get("questions") or []
        if q.get("type_name") in ("单选", "多选", "矩阵量表")
    }

    n = max(1, int(count))
    rounds, trace, bags, round_texts = [], [], {}, []
    text_bags = {}
    for idx in range(n):
        rnd = idx + 1
        ans = dict(base)
        if choice_random and choice_topics:
            fresh = random_answers(survey)
            for t in choice_topics:
                if t in fresh:
                    ans[t] = fresh[t]
        # 概率配置最后覆盖：即使关掉「每轮随机」，已配置的题目也按配置抽，
        # 这是「按轮次启用」的含义；未配置的题目仍沿用上面的开关行为。
        for t, smp in (samplers or {}).get(rnd, {}).items():
            ans[t] = smp.draw()
        picked = {}
        for t, pool in pools.items():
            if random_fill:
                bag = bags.get(t) or []
                if not bag:
                    bag = list(pool)
                    random.shuffle(bag)
                val = bag.pop()
                bags[t] = bag
            else:
                val = pool[0]
            ans[t] = val
            picked[t] = val
        rounds.append(ans)
        trace.append(picked)
        round_texts.append(resolve_option_texts(
            survey, ans, option_pools, manual_texts, random_fill, text_bags))
    return rounds, trace, sorted(pools), round_texts


def _default_answers(survey):
    """生成默认答案：单选首项、多选满足下限、矩阵中间值。"""
    out = {}
    for q in survey["questions"]:
        t = str(q["topic"])
        if q["type_name"] == "单选":
            out[t] = q["options"][0]["value"]
        elif q["type_name"] == "多选":
            n = q.get("exact_select") or q.get("min_select") or 1
            out[t] = [o["value"] for o in q["options"][:n]]
        elif q["type_name"] == "矩阵量表":
            mid = len(q["matrix"]["scale"]) // 2 + 1
            out[t] = [mid] * len(q["matrix"]["rows"])
    return out


class Server(ThreadingHTTPServer):
    daemon_threads = True
    # Windows 的 SO_REUSEADDR 语义允许两个进程绑同一端口，旧进程会继续应答，
    # 表现为「代码改了但页面没变」。关掉它，端口被占时直接报错更省事。
    allow_reuse_address = False


def main():
    # 统一成 UTF-8：子进程的输出会被按 UTF-8 读，不一致就会乱码
    from console import force_utf8
    force_utf8()
    ap = argparse.ArgumentParser(description="问卷星作答器 Web 版")
    ap.add_argument("--port", type=int, default=8765)
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--no-browser", action="store_true")
    args = ap.parse_args()

    load_survey_file()
    load_fill_template()
    load_weights()
    url = f"http://{args.host}:{args.port}/"

    if not args.no_browser:
        threading.Timer(0.8, lambda: webbrowser.open(url)).start()

    try:
        srv = Server((args.host, args.port), Handler)
    except OSError as e:
        print(f"端口 {args.port} 被占用（{e}）。", flush=True)
        print("先结束旧进程，或换端口：python server.py --port 8766", flush=True)
        return

    print(f"服务已启动：{url}", flush=True)
    print(f"问卷：{S.survey['title'] if S.survey else '未加载（在页面顶部填短链抓取）'}",
          flush=True)
    print("Ctrl+C 结束", flush=True)

    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        print("\n已停止", flush=True)


if __name__ == "__main__":
    main()
