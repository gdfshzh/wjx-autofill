#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
wjx_browser.py —— 真浏览器驱动的问卷自动填写

用 Playwright 驱动本机 Edge/Chrome，逐题模拟人的操作：鼠标走曲线、按下抬起有间隔、
滚动分段、逐字符打字。回答的是「浏览器层面」的问题，不碰 HTTP 协议。

启动：
    python wjx_browser.py --url https://v.wjx.cn/vm/Oki2m8H.aspx --survey survey.json
    python wjx_browser.py --url ... --answers answers.json --headless

依赖：
    python -m pip install playwright
    用系统 Edge/Chrome 时无需 playwright install（脚本会自动找 channel）
"""

import argparse
import json
import math
import os
import random
import re
import sys
import time

# 打包版用 embedded Python，带 ._pth 时处于 isolated 模式：脚本目录不会
# 自动进 sys.path，PYTHONPATH 也被忽略。作为子进程被 server.py 拉起时，
# 同级模块（console / humanize / parse_wjx）会直接 ModuleNotFoundError，
# 所以这里显式补一次。源码运行时这行是空操作。
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

try:
    from playwright.sync_api import sync_playwright, TimeoutError as PWTimeout
    HAS_PW = True
except ImportError:
    HAS_PW = False

HERE = os.path.dirname(os.path.abspath(__file__))

# 页面 isStrictMobile=1，必须用移动端身份：UA、视口、触摸能力三者要配套
UA_IOS = ("Mozilla/5.0 (iPhone; CPU iPhone OS 17_2 like Mac OS X) "
          "AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.2 Mobile/15E148 Safari/604.1")

# 浏览器环境补丁：抹掉自动化痕迹，补齐真人浏览器才有的属性
STEALTH_JS = """
Object.defineProperty(navigator, 'webdriver', { get: () => undefined });
Object.defineProperty(navigator, 'languages', { get: () => ['zh-CN', 'zh', 'en'] });
Object.defineProperty(navigator, 'hardwareConcurrency', { get: () => 8 });
Object.defineProperty(navigator, 'deviceMemory', { get: () => 8 });
if (!window.chrome) window.chrome = { runtime: {}, loadTimes: function(){}, csi: function(){} };
// 通知权限查询：无头模式下常被识别
const _permQuery = window.navigator.permissions.query.bind(navigator.permissions);
window.navigator.permissions.query = (p) =>
  p && p.name === 'notifications'
    ? Promise.resolve({ state: Notification.permission })
    : _permQuery(p);
// WebGL 厂商信息：默认是 SwiftShader，真机不会是
try {
  const gp = WebGLRenderingContext.prototype.getParameter;
  WebGLRenderingContext.prototype.getParameter = function (p) {
    if (p === 37445) return 'Intel Inc.';
    if (p === 37446) return 'Intel Iris OpenGL Engine';
    return gp.apply(this, arguments);
  };
} catch (e) {}
// 屏幕色深
Object.defineProperty(screen, 'colorDepth', { get: () => 24 });
"""


# 阿里云验证码的真实 DOM（实测于 2026-11-01）：
#   #aliyunCaptcha-window-popup          可见弹窗 352x143
#     ├─ .aliyunCaptcha-top              "请完成安全验证"
#     └─ .aliyun-captcha
#         └─ .checkbox  320x40           ← 整条可点
#             ├─ .aliyunCaptcha-checkbox-checked
#             └─ .initDone 112x40        ← "点击开始智能验证" 文字，人点的就是它
# 注意：wjx 自己的 #captchaWrap / #captchabtn 高度恒为 0，判断可见性会永远失败。
CAPTCHA_POPUP = "#aliyunCaptcha-window-popup"
CAPTCHA_BTNS = (".initDone", ".aliyunCaptcha-checkbox-checked", ".checkbox")


def captcha_present(page) -> bool:
    """判断验证码是否真的弹出来了。"""
    for sel in (CAPTCHA_POPUP,) + CAPTCHA_BTNS:
        try:
            loc = page.locator(sel)
            if loc.count() and loc.first.is_visible():
                return True
        except Exception:
            continue
    return False


def wait_manual_captcha(page, timeout: int = 300, verbose: bool = True) -> bool:
    """人工接管：脚本停下，等人在浏览器窗口里点完，再继续。

    当出口 IP 已被打成高风险时，SDK 不会给「一点即过」，自动点击也过不了，
    这时唯一快的办法就是让人点一下。有头模式下开着窗口，人点完脚本自动恢复。
    """
    if verbose:
        print("  [验证码] 需要人工点击 —— 请在浏览器窗口里完成验证（最多等 5 分钟）")
    end = time.time() + timeout
    while time.time() < end:
        time.sleep(1)
        try:
            if page.evaluate("() => window.isCaptchaValid === true"):
                if verbose:
                    print("  [验证码] 人工验证通过，继续")
                return True
        except Exception:
            pass
        if not captcha_present(page):
            if verbose:
                print("  [验证码] 弹窗已消失，继续")
            return True
    if verbose:
        print("  [验证码] 等待超时")
    return False


def handle_captcha(page, human=None, timeout: int = 45, verbose: bool = True,
                   manual: bool = False) -> bool:
    """处理阿里云「一点即过」智能验证。

    captchaVerifyParam 只能由页面里的 SDK 在真实环境生成（服务端只做验签），
    HTTP 层伪造不出来，必须回到浏览器点。

    点击必须带鼠标轨迹：SDK 会采集 mousemove、移动耗时、落点偏移，
    瞬移 + 中心命中大概率被判成机器，表现就是「点了没反应」。
    """
    if not captcha_present(page):
        return False

    if manual:
        return wait_manual_captcha(page, timeout=max(120, timeout), verbose=verbose)

    if verbose:
        print("  [验证码] 弹窗已出现，用人类轨迹点击")

    time.sleep(random.uniform(0.6, 1.4))   # 像人一样看一眼

    # 先制造一点「正常行为」：SDK 从初始化起就在采集，页面刚加载就点，
    # 轨迹样本太少，风险分会偏高。晃几下、滚两下再动手。
    if human:
        for _ in range(random.randint(2, 3)):
            human.scroll(random.randint(-90, 140))
            human._sleep(0.18, 0.45)
        vp = page.viewport_size or {"width": 390, "height": 844}
        for _ in range(random.randint(2, 3)):
            human.move_to(vp["width"] * random.uniform(.25, .75),
                          vp["height"] * random.uniform(.3, .75))
            human._sleep(0.12, 0.3)
        time.sleep(random.uniform(0.4, 0.9))

    def _click_sel(sel):
        loc = page.locator(sel)
        if not loc.count() or not loc.first.is_visible():
            return False
        if human:
            human.click(sel, "智能验证框")
        else:
            box = loc.first.bounding_box()
            if not box:
                return False
            page.mouse.move(box["x"] + box["width"] / 2,
                            box["y"] + box["height"] / 2, steps=14)
            page.mouse.click(box["x"] + box["width"] / 2,
                             box["y"] + box["height"] / 2)
        return True

    def _wait_pass(sec=4.0):
        end = time.time() + sec
        while time.time() < end:
            time.sleep(0.4)
            try:
                if page.evaluate("() => window.isCaptchaValid === true"):
                    return True
            except Exception:
                pass
            if not captcha_present(page):
                return True
        return False

    # 从大到小试：整条 .checkbox → 文字 .initDone → 坐标兜底
    for sel in (".checkbox", ".initDone", ".aliyunCaptcha-checkbox-checked"):
        try:
            if _click_sel(sel) and _wait_pass(4.0):
                if verbose:
                    print(f"  [验证码] 通过（{sel}）")
                return True
        except Exception:
            continue

    # 坐标兜底：弹窗下半部分是框框所在区域
    try:
        box = page.locator(CAPTCHA_POPUP).first.bounding_box()
        if box:
            for dy in (0.55, 0.62, 0.70):
                cx = box["x"] + box["width"] / 2 + random.uniform(-40, 40)
                cy = box["y"] + box["height"] * dy + random.uniform(-6, 6)
                if human:
                    human.move_to(cx, cy)
                    human._sleep(0.12, 0.28)
                else:
                    page.mouse.move(cx, cy, steps=12)
                page.mouse.click(cx, cy)
                if _wait_pass(3.0):
                    if verbose:
                        print("  [验证码] 通过（坐标）")
                    return True
    except Exception:
        pass

    # 长等一次，有些环境下 SDK 的回调比较慢
    try:
        page.wait_for_function("() => window.isCaptchaValid === true",
                               timeout=max(10, timeout) * 1000)
        if verbose:
            print("  [验证码] 通过（延迟回调）")
        return True
    except Exception:
        if verbose:
            print("  [验证码] 未通过，判断是否已变成滑块")
        return drag_slider(page, verbose=verbose)


def drag_slider(page, verbose: bool = True) -> bool:
    """拖动滑块：加速 → 减速 → 轻微回弹，接近人的手。

    阿里云滑块（nc 系列）的轨道元素在不同版本里 class 不同，
    这里按可见的「滑块把手」找，找不到就返回。
    """
    handles = (
        ".aliyunCaptcha-sliding-slider",
        "[class*='sliding'] [class*='slider']",
        "[class*='nc_1_n1z']",
        ".btn_slide",
        "#nc_1_n1z",
    )
    handle = None
    for sel in handles:
        try:
            loc = page.locator(sel)
            if loc.count() and loc.first.is_visible():
                handle = loc.first
                break
        except Exception:
            continue
    if not handle:
        if verbose:
            print("  [滑块] 没找到滑块把手")
        return False

    try:
        box = handle.bounding_box()
        if not box:
            return False
        sx = box["x"] + box["width"] / 2
        sy = box["y"] + box["height"] / 2

        # 轨道宽度：父容器宽度减去把手宽度
        track = page.evaluate(
            """el => { const p = el.closest('[class*="sliding"]') || el.parentElement;
                       return p ? p.getBoundingClientRect().width : 280; }""",
            handle.element_handle())

        page.mouse.move(sx, sy)
        time.sleep(random.uniform(0.15, 0.35))
        page.mouse.down()
        time.sleep(random.uniform(0.1, 0.2))

        # 分三段：起步慢、中段快、收尾减速 + 回弹
        dist = max(0, track - box["width"] - random.uniform(2, 6))
        cur = 0.0
        while cur < dist * 0.75:
            step = random.uniform(6, 16)
            cur = min(cur + step, dist * 0.75)
            page.mouse.move(sx + cur, sy + random.uniform(-1.5, 1.5))
            time.sleep(random.uniform(0.012, 0.03))
        while cur < dist:
            step = random.uniform(2, 6)
            cur = min(cur + step, dist)
            page.mouse.move(sx + cur, sy + random.uniform(-1, 1))
            time.sleep(random.uniform(0.02, 0.05))
        # 回弹一点，人松手前常有这个动作
        page.mouse.move(sx + dist - random.uniform(1, 4), sy)
        time.sleep(random.uniform(0.08, 0.18))
        page.mouse.up()

        page.wait_for_function("() => window.isCaptchaValid === true", timeout=15000)
        if verbose:
            print("  [滑块] 拖动通过")
        return True
    except Exception as e:
        if verbose:
            print(f"  [滑块] 拖动未通过: {type(e).__name__}")
        return False



class Human:
    """把 Playwright 的原子操作包成人类动作。

    移动端页面（isStrictMobile=1）必须走触摸事件：真人在手机上按出的是
    touchstart → touchend，如果用 page.mouse 只会产生 mousedown/mouseup，
    行为模型一眼就能看出「iPhone UA 却在用鼠标」。
    """

    def __init__(self, page, slow: float = 1.0, verbose: bool = True, touch: bool = True):
        self.page = page
        self.slow = max(0.2, slow)
        self.verbose = verbose
        self.touch = touch
        vp = page.viewport_size or {"width": 1280, "height": 800}
        self.pos = (vp["width"] * random.uniform(.3, .7), vp["height"] * random.uniform(.25, .6))

    def _sleep(self, lo, hi):
        time.sleep(random.uniform(lo, hi) * self.slow)

    def log(self, msg):
        if self.verbose:
            print(msg, flush=True)

    # ---------------------------------------------------------- 指针
    def move_to(self, x: float, y: float):
        """二次贝塞尔曲线移动，而不是瞬移。真人的手不会走直线。"""
        bx, by = self.pos
        cx = (bx + x) / 2 + random.uniform(-90, 90)
        cy = (by + y) / 2 + random.uniform(-70, 70)
        steps = random.randint(14, 28)
        for i in range(1, steps + 1):
            t = i / steps
            px = (1 - t) ** 2 * bx + 2 * (1 - t) * t * cx + t ** 2 * x
            py = (1 - t) ** 2 * by + 2 * (1 - t) * t * cy + t ** 2 * y
            self.page.mouse.move(px, py)
            time.sleep(random.uniform(0.008, 0.028) * self.slow)
        self.pos = (x, y)

    def _press(self, x: float, y: float):
        """落下去的那一下：移动端走 touchscreen.tap，桌面才用 mouse.down/up。"""
        if self.touch:
            try:
                self.page.touchscreen.tap(x, y)
                return
            except Exception:
                pass              # 环境不支持触摸，退回鼠标
        self.page.mouse.move(x, y)
        self.page.mouse.down()
        self._sleep(0.04, 0.11)
        self.page.mouse.up()

    def click(self, selector: str, desc: str = ""):
        """点可见元素的非中心位置，抬起前有按压间隔。"""
        loc = self.page.locator(selector).first
        loc.wait_for(state="visible", timeout=15000)
        box = loc.bounding_box()
        if not box:
            raise RuntimeError(f"元素无尺寸: {selector}")
        # 先把目标滚到视口中段，再移动过去
        self.page.evaluate(
            """el => { const r = el.getBoundingClientRect();
                 if (r.top < 90 || r.bottom > innerHeight - 90)
                   el.scrollIntoView({block:'center', behavior:'instant'}); }""",
            loc.element_handle(),
        )
        self._sleep(0.15, 0.45)
        box = loc.bounding_box() or box
        x = box["x"] + box["width"] * random.uniform(0.28, 0.72)
        y = box["y"] + box["height"] * random.uniform(0.34, 0.66)
        self.move_to(x, y)
        self._sleep(0.06, 0.22)          # 落点后的微调停顿
        self._press(x, y)
        if desc:
            self.log(f"    · 点击 {desc}")

    def scroll(self, total: float):
        """分段滚动，速度不均匀。"""
        remain = float(total)
        while abs(remain) > 8:
            step = remain * random.uniform(0.18, 0.42)
            self.page.mouse.wheel(0, step)
            remain -= step
            time.sleep(random.uniform(0.04, 0.16) * self.slow)

    def type_text(self, selector: str, text: str, desc: str = ""):
        """逐字符输入，字符间隔仿打字节奏，偶有停顿。"""
        loc = self.page.locator(selector).first
        loc.wait_for(state="visible", timeout=10000)
        self.click(selector)
        for ch in text:
            self.page.keyboard.type(ch)
            d = random.uniform(0.055, 0.19)
            if random.random() < 0.08:      # 偶尔想一下
                d += random.uniform(0.3, 0.9)
            time.sleep(d * self.slow)
        if desc:
            self.log(f"    · 输入 {desc}: {text[:20]}")

    def idle(self, lo=0.6, hi=1.8):
        """什么都不做，只是一次呼吸。偶尔移动一下鼠标。"""
        self._sleep(lo, hi)
        if random.random() < 0.35:
            vp = self.page.viewport_size or {"width": 1280, "height": 800}
            self.move_to(vp["width"] * random.uniform(.2, .8),
                         vp["height"] * random.uniform(.2, .75))


class WjxBrowser:
    """按题型驱动整份问卷。"""

    def __init__(self, page, survey: dict, human: Human, retry: int = 1,
                 strict: bool = False, manual_captcha: bool = False):
        self.page = page
        self.survey = survey
        self.h = human
        self.retry = retry
        self.strict = strict
        self.manual_captcha = manual_captcha
        self.errors = []
        self.done = []
        # 必答题里没填上的题号。和 errors 分开记：errors 是「点了但抛异常」，
        # 这里还包括「压根没有答案可填」。提交前必须看这个，否则点了也是白点。
        self.missing_required = []

    # ------------------------------------------------------ 题型分派
    def fill_all(self, answers: dict, option_texts: dict = None):
        if not option_texts:
            # 直接跑 CLI、没经 server 的模板链路时，至少给必填补充框兜底
            from wjx_autofill import fallback_option_texts
            option_texts = fallback_option_texts(answers, self.survey)
        option_texts = option_texts or {}
        # 先像人一样把页面从上往下扫一遍
        self.h.scroll(random.randint(300, 700))
        self.h.idle()

        last_section = None
        for q in self.survey["questions"]:
            topic = str(q["topic"])
            if q["section"] != last_section:
                last_section = q["section"]
                self.h.idle(0.8, 2.0)
                self.h.scroll(random.randint(120, 320))

            val = answers.get(topic)
            if val is None:
                if q["required"]:
                    self.missing_required.append(topic)
                    self.h.log(f"  ! Q{topic} 必答但无答案，跳过")
                continue

            texts = option_texts.get(topic) or option_texts.get(int(topic)) or {}
            ok = self._with_retry(q, val, texts)
            self.done.append(topic if ok else f"{topic}(fail)")
            if not ok and q["required"]:
                self.missing_required.append(topic)
            self.h.idle(0.35, 1.2)
            # 每 4~6 题滚动一点，模拟阅读推进
            if random.random() < 0.22:
                self.h.scroll(random.choice([-90, 120, 180, 260]))

        # 全部填完再读一遍整页。逐题验证只能发现「当场没选上」，
        # 后面题目的滚动、联动有可能把前面的答案挤掉，这里兜一次底。
        for t in self._missing_on_page():
            if t not in self.missing_required:
                self.missing_required.append(t)
                self.h.log(f"  ! Q{t} 提交前复核：页面上读不到答案")

        if self.missing_required:
            print(f"\n[警告] 必答题没填上：Q{'、Q'.join(self.missing_required)}", flush=True)
        if self.errors:
            print(f"\n[警告] {len(self.errors)} 题未成功：", flush=True)
            for e in self.errors[:10]:
                print("   -", e, flush=True)

    def _with_retry(self, q, val, texts=None):
        topic = q["topic"]
        msg = ""
        texts = texts or {}
        for attempt in range(self.retry + 1):
            try:
                self._answer_one(q, val, texts)
            except Exception as e:
                msg = f"Q{topic} 第{attempt + 1}次失败: {type(e).__name__}: {str(e)[:90]}"
            else:
                # 点完不算完：坐标点击可能落到别处，前端校验也可能静默拦下
                if self._verify_one(q, val, texts):
                    return True
                msg = f"Q{topic} 第{attempt + 1}次点完回读不一致（页面上没选上）"

            self.h.log("  ! " + msg)
            if attempt < self.retry:
                time.sleep(random.uniform(0.6, 1.6))
                continue
            self.errors.append(msg)
            if self.strict:
                raise RuntimeError(msg)
            return False
        return False

    def _answer_one(self, q, val, texts=None):
        t = q["topic"]
        kind = q["type_name"]
        texts = texts or {}

        if kind == "单选":
            self._single(t, val, texts)
        elif kind == "多选":
            self._multi(t, val, texts)
        elif kind == "矩阵量表":
            self._matrix(t, val)
        elif kind == "下拉":
            self.page.select_option(f"#div{t} select", str(val))
            self.h.log(f"    · 下拉 Q{t} = {val}")
        elif kind in ("填空(单行)", "填空(多行)"):
            sel = f"#div{t} input[type=text], #div{t} textarea"
            self.h.type_text(sel, str(val), f"Q{t}")
        else:
            # 未知题型：不猜，直接报出来
            raise RuntimeError(f"Q{t} 题型 {kind} 未实现")

    # ------------------------------------------------------ 单/多选
    def _pick_label(self, topic: int, value: str):
        """点 label 而不是 input。

        问卷星的 input 是 display:none，真正响应点击的是同一 .ui-radio / .ui-checkbox
        里的 label（或 a.jqradio）。直接点 input 会报 element is not visible。
        """
        sel = f"#div{topic} .label[for='q{topic}_{value}']"
        if self.page.locator(sel).count() == 0:
            raise RuntimeError(f"Q{topic} 找不到选项 {value} 的 label")
        self.h.click(sel, f"Q{topic} 选项 {value}")

    def _fill_option_text(self, topic: int, value, texts: dict):
        """选完带补充框的选项后，把文本填进刚显示出来的输入框。

        问卷星的补充框是 display:none，选中后才显示；每项的 rel 是
        q{topic}_{value}，id 是 tqq{topic}_{value}。两个选择器都兜一下。
        """
        text = str((texts or {}).get(str(value), "")).strip()
        if not text:
            return
        sel = (f"#div{topic} .OtherRadioText[rel='q{topic}_{value}'], "
               f"#div{topic} .OtherText[rel='q{topic}_{value}'], "
               f"#div{topic} #tqq{topic}_{value}")
        loc = self.page.locator(sel).first
        if loc.count() == 0:
            raise RuntimeError(f"Q{topic} 选项 {value} 找不到补充输入框")
        self.h.type_text(sel, text, f"Q{topic} 补充")

    def _single(self, topic: int, value, texts=None):
        self._pick_label(topic, str(value))
        self._fill_option_text(topic, value, texts)

    def _multi(self, topic: int, values, texts=None):
        vals = values if isinstance(values, list) else [values]
        # 人的勾选顺序不是升序，打乱后再点
        order = list(vals)
        random.shuffle(order)
        for v in order:
            self._pick_label(topic, str(v))
            self._fill_option_text(topic, v, texts)
            self.h.idle(0.15, 0.5)

    # ------------------------------------------------------ 矩阵
    def _matrix(self, topic: int, scores):
        """矩阵量表：每行点一个 a[dval=分值]，行与行之间停一下。"""
        rows = self.page.locator(f"#div{topic} tr[tp='d']")
        n = rows.count()
        if n == 0:
            n = len(scores)
        # 这一行是给前端的进度用的：没有它，矩阵题不会推进 current，
        # 进度条会永远停在倒数第二题
        self.h.log(f"    · 点击 Q{topic} 矩阵评分 {len(scores)} 行")
        for i, score in enumerate(scores):
            if i >= n:
                break
            row = rows.nth(i)
            cell = row.locator(f"a[dval='{int(score)}']").first
            box = cell.bounding_box()
            if not box:
                raise RuntimeError(f"矩阵第 {i + 1} 行无评分格")
            x = box["x"] + box["width"] * random.uniform(0.3, 0.7)
            y = box["y"] + box["height"] * random.uniform(0.3, 0.7)
            self.page.evaluate(
                """el => { const r = el.getBoundingClientRect();
                     if (r.top < 90 || r.bottom > innerHeight - 90)
                       el.scrollIntoView({block:'center', behavior:'instant'}); }""",
                cell.element_handle(),
            )
            box = cell.bounding_box() or box
            self.h.move_to(box["x"] + box["width"] / 2, box["y"] + box["height"] / 2)
            self.h._sleep(0.05, 0.18)
            self.page.mouse.down()
            self.h._sleep(0.04, 0.1)
            self.page.mouse.up()
            if (i + 1) % 3 == 0:
                self.h.idle(0.2, 0.8)

    # ------------------------------------------------------ 提交
    def _page_state(self):
        """点完提交后，页面到底处于什么状态。

        返回 (state, detail)：
          success     已经跳到完成页
          unanswered  有题没答（问卷星会把页面滚回那道题，并在 .errorMessage 里写字）
          error       服务端或前端的错误提示
          pending     还没反应

        关键：只看 #divQuestion 的文本，不看 page.content()。
        整页 HTML 的 <meta property="og:description"> 里带「非常感谢」，
        用 page.content() 做 in 判断会让每一份问卷点完提交都「成功」。
        """
        url = self.page.url
        if "complete" in url:
            # 必须真拿到 joinid。提交被打回时页面也可能停在完成页，
            # 但 joinid 是空的 —— 那种不能算成功。
            # 移动版完成页用 joinactivity，PC 版用 joinid，两个都认
            if re.search(r"(?:joinid|joinactivity)=(\d+)", url):
                return "success", url
            return "error", "跳到了完成页但没拿到答卷编号，可能没真正入库"

        # 每题下方都有 .errorMessage，被填了文字就是有题没答
        try:
            for loc in self.page.locator(".errorMessage").all()[:30]:
                try:
                    t = (loc.inner_text() or "").strip()
                except Exception:
                    continue
                if t:
                    return "unanswered", t[:60]
        except Exception:
            pass

        # 弹层 / 顶部错误条
        for sel in ("#ValError", "#divError", ".layui-layer-content",
                    ".layui-layer-msg", "#tiplayer"):
            try:
                loc = self.page.locator(sel).first
                if loc.count() and loc.is_visible():
                    t = (loc.inner_text() or "").strip()
                    if t:
                        return "error", t[:80]
            except Exception:
                continue

        try:
            body = self.page.locator("#divQuestion").inner_text(timeout=2000)
        except Exception:
            body = ""
        for kw in ("请完成", "未作答", "还有题目", "必答"):
            if kw in body:
                return "unanswered", kw
        return "pending", ""

    # ------------------------------------------------------ 回读校验
    def _read_page_state(self):
        """回读页面上真实选中的答案。

        为什么需要：填题用的是坐标点击，落到别处、被浮层挡住、被前端校验拦下
        都不会抛异常。「没报错」不等于「选上了」，只有读回来才能确认。

        各题型的选中标记（实测）：
          单选/多选   input[type=radio|checkbox]:checked（input 本身 display:none）
          矩阵        a 加 class「rate-on」（同时保留 rate-off）
          填空        输入框的 value
        """
        out = {}
        for q in self.survey.get("questions") or []:
            topic = q["topic"]
            tn = q["type_name"]
            try:
                if tn == "单选":
                    loc = self.page.locator(f"#div{topic} input[type=radio]:checked")
                    if loc.count():
                        v = (loc.first.get_attribute("value") or "").strip()
                        if v:
                            out[str(topic)] = v

                elif tn == "多选":
                    loc = self.page.locator(f"#div{topic} input[type=checkbox]:checked")
                    vals = [((x.get_attribute("value") or "").strip()) for x in loc.all()]
                    vals = [v for v in vals if v]
                    if vals:
                        out[str(topic)] = vals

                elif tn == "矩阵量表":
                    rows = self.page.locator(f"#div{topic} tr[tp='d']")
                    n = rows.count()
                    vals = []
                    for i in range(n):
                        on = rows.nth(i).locator("a.rate-on").first
                        if not on.count():
                            vals = []
                            break
                        d = (on.get_attribute("dval") or "").strip()
                        vals.append(int(d) if d.isdigit() else 0)
                    if vals and len(vals) == n and all(v > 0 for v in vals):
                        out[str(topic)] = vals

                elif tn.startswith("填空"):
                    el = self.page.locator(
                        f"#div{topic} input[type=text], #div{topic} textarea").first
                    if el.count():
                        v = (el.input_value() or "").strip()
                        if v:
                            out[str(topic)] = v

                elif tn == "下拉":
                    el = self.page.locator(f"#div{topic} select").first
                    if el.count():
                        v = (el.input_value() or "").strip()
                        if v:
                            out[str(topic)] = v
            except Exception:
                continue
        return out

    def _read_option_text(self, topic, value):
        """读某个选项补充框的当前值；没有这个框返回 None。"""
        sel = (f"#div{topic} .OtherRadioText[rel='q{topic}_{value}'], "
               f"#div{topic} .OtherText[rel='q{topic}_{value}'], "
               f"#div{topic} #tqq{topic}_{value}")
        loc = self.page.locator(sel).first
        if loc.count() == 0:
            return None
        try:
            return (loc.input_value() or "").strip()
        except Exception:
            return None

    def _linked_text_missing(self, q, values):
        """必答的联动选项是否还缺补充文本。"""
        vals = values if isinstance(values, list) else [values]
        optmap = {str(o.get("value")): o for o in (q.get("options") or [])}
        for v in vals:
            opt = optmap.get(str(v))
            if not opt or not opt.get("has_text"):
                continue
            if opt.get("text_required") and not self._read_option_text(q["topic"], v):
                return True
        return False

    def _verify_one(self, q, val, texts=None):
        """填完这一题后回读，确认选项和补充文本都和期望一致。"""
        topic = str(q["topic"])
        got = self._read_page_state().get(topic)
        if got is None:
            return False
        tn = q["type_name"]
        if tn == "多选":
            want = sorted(str(v) for v in (val if isinstance(val, list) else [val]))
            if sorted(str(v) for v in got) != want:
                return False
        elif tn == "矩阵量表":
            try:
                want = [int(v) for v in val]
            except Exception:
                return False
            if [int(v) for v in got] != want:
                return False
        elif str(got).strip() != str(val).strip():
            return False

        # 选项对了还要看补充框：必填空着、或者填进去的字被前端清掉，都算失败
        texts = texts or {}
        vals = val if isinstance(val, list) else [val]
        optmap = {str(o.get("value")): o for o in (q.get("options") or [])}
        for v in vals:
            opt = optmap.get(str(v))
            if not opt or not opt.get("has_text"):
                continue
            actual = self._read_option_text(topic, v)
            want_text = str(texts.get(str(v), "")).strip()
            if actual is None:
                return False
            if opt.get("text_required") and not actual:
                return False
            if want_text and actual != want_text:
                return False
        return True

    def _missing_on_page(self):
        """必答题里，页面上读不到答案（或联动补充为空）的题号。"""
        actual = self._read_page_state()
        missing = []
        for q in (self.survey.get("questions") or []):
            if not q.get("required"):
                continue
            topic = str(q["topic"])
            if topic not in actual:
                missing.append(topic)
            elif self._linked_text_missing(q, actual[topic]):
                missing.append(topic)
        return missing

    def submit(self, timeout: int = 60) -> dict:
        # 提交前自检：必答题没填上就别点，点了也必然被打回，
        # 而打回之后页面 URL 不变，很容易被误记成成功
        if self.missing_required:
            topics = "、Q".join(self.missing_required[:6])
            more = "…" if len(self.missing_required) > 6 else ""
            return {"ok": False,
                    "msg": f"必答题未填写：Q{topics}{more}"
                           f"（共 {len(self.missing_required)} 题），未提交"}

        # 再回读一次页面。填题过程中页面可能被重置（切页、联动、超时），
        # 只信 DOM 当前状态，不信 fill_all 留下的记录。
        onpage = self._missing_on_page()
        if onpage:
            more2 = "…" if len(onpage) > 6 else ""
            return {"ok": False,
                    "msg": f"提交前复核：Q{'、Q'.join(onpage[:6])}{more2}"
                           f" 页面上没有答案（共 {len(onpage)} 题），未提交"}

        self.h.log("\n[提交] 检查一遍再点")
        # 像人一样回到顶/底看一遍
        self.h.scroll(-random.randint(400, 900))
        self.h.idle(1.0, 2.4)
        self.h.scroll(random.randint(500, 1200))
        self.h.idle(0.8, 2.0)

        btn = self.page.locator("#ctlNext, .submitbtn").first
        btn.wait_for(state="visible", timeout=15000)
        self.h.click("#ctlNext, .submitbtn", "提交按钮")

        # 服务端要求验证时前端会弹窗，这里等它出现再处理
        time.sleep(2.5)
        if handle_captcha(self.page, human=self.h, manual=self.manual_captcha):
            time.sleep(random.uniform(2.5, 4.5))   # SDK 回调后前端会自己重新提交

        # 等服务端响应：成功会跳转，失败会给出错误或滚回未答题
        deadline = time.time() + timeout
        checks = 0
        while time.time() < deadline:
            time.sleep(1)
            checks += 1
            # 验证码可能晚一步弹出，循环里再兜一次
            if captcha_present(self.page):
                handle_captcha(self.page, human=self.h, manual=self.manual_captcha)
                time.sleep(2)

            state, detail = self._page_state()
            if state == "success":
                return {"ok": True, "url": detail}
            if state == "unanswered":
                return {"ok": False, "msg": f"有未作答题没答上：{detail}"}
            if state == "error":
                return {"ok": False, "msg": detail}

            # 三秒还没任何反应，才认为点击没生效并补点一次（只补一次）。
            # 放在第一次检查就补点会导致每次都提交两遍。
            if checks == 3:
                try:
                    if not btn.is_disabled():
                        self.h.log("    · 页面无响应，补点一次提交")
                        self.h.click("#ctlNext, .submitbtn", "提交按钮(重试)")
                except Exception:
                    pass
        return {"ok": False, "msg": "等待超时，页面状态未知"}


def launch(p, headless: bool, proxy: str = None):
    """优先用本机 Edge/Chrome，避免下载浏览器。"""
    args = [
        "--disable-blink-features=AutomationControlled",
        "--no-first-run",
        "--no-default-browser-check",
    ]
    for channel in ("msedge", "chrome"):
        try:
            return p.chromium.launch(channel=channel, headless=headless, args=args,
                                     proxy={"server": proxy} if proxy else None)
        except Exception:
            continue
    return p.chromium.launch(headless=headless, args=args,
                             proxy={"server": proxy} if proxy else None)


def run_once(p, args, survey, answers, option_texts=None, idx=1, total=1):
    """跑一轮：新开 context（新 cookie），填完提交，关掉。返回结果字典。"""
    browser = launch(p, args.headless, args.proxy)
    try:
        ctx = browser.new_context(
            viewport={"width": 390, "height": 844},   # iPhone 14 逻辑分辨率
            device_scale_factor=3,
            is_mobile=True,
            has_touch=True,
            user_agent=UA_IOS,
            locale="zh-CN",
            timezone_id="Asia/Shanghai",
        )
        ctx.add_init_script(STEALTH_JS)     # 抹掉自动化痕迹 + 补齐真人浏览器属性
        page = ctx.new_page()

        print(f"[第 {idx}/{total} 轮] 打开 {args.url}", flush=True)
        page.bring_to_front()          # 窗口调到最前，否则可能被其他窗口挡住
        page.goto(args.url, wait_until="domcontentloaded", timeout=60000)
        page.wait_for_selector("#divQuestion", timeout=30000)
        time.sleep(random.uniform(1.5, 3.5))

        human = Human(page, slow=args.slow)
        eng = WjxBrowser(page, survey, human, retry=args.retry, strict=args.strict,
                     manual_captcha=args.manual_captcha)
        eng.fill_all(answers, option_texts)
        print(f"[第 {idx}/{total} 轮] 已处理 {len(eng.done)} 题，失败 {len(eng.errors)} 题",
              flush=True)

        if args.no_submit:
            print("[跳过提交] 3 秒后进入下一轮", flush=True)
            time.sleep(3)
            return {"ok": False, "msg": "no-submit"}

        r = eng.submit()

        # 成功必须有 joinid：只有真的跳到完成页并带回 id，才算这一份落库。
        # 没有 id 的一律按失败计，避免「页面看着完成、其实没入库」被算成成功。
        if r.get("ok"):
            m = re.search(r"(?:joinid|joinactivity)=(\d+)", r.get("url", ""))
            if m:
                r["joinid"] = m.group(1)
            else:
                r = {"ok": False, "msg": "页面显示完成但没有答卷编号，按失败计"}

        print(f"[第 {idx}/{total} 轮] 结果 {r}", flush=True)
        # 父进程按这行 JSON 统计，不再靠字符串匹配 dict 的 repr
        print("[RESULT] " + json.dumps(r, ensure_ascii=False), flush=True)
        if args.keep_open:
            print("[保持打开] 结果页留在窗口里，看完按 Ctrl+C 结束", flush=True)
            try:
                time.sleep(1800)
            except KeyboardInterrupt:
                pass
        else:
            time.sleep(random.uniform(4, 7))   # 留几秒看清结果页
        return r
    finally:
        browser.close()


def main():
    # 子进程的输出会被 server.py 按 UTF-8 读，这里必须对齐，
    # 否则中文在这一层就变成乱码，网页日志栏跟着一起坏
    from console import force_utf8
    force_utf8()

    ap = argparse.ArgumentParser(description="真浏览器问卷自动填写")
    ap.add_argument("--url", required=True)
    ap.add_argument("--survey", default=os.path.join(HERE, "survey.json"))
    ap.add_argument("--answers", default=os.path.join(HERE, "answers.template.json"))
    ap.add_argument("--headless", action="store_true", help="无头模式，默认有头可见")
    ap.add_argument("--slow", type=float, default=1.0, help="整体放慢倍数，1=正常")
    ap.add_argument("--retry", type=int, default=1, help="单题失败重试次数")
    ap.add_argument("--strict", action="store_true", help="任一题失败立即中止")
    ap.add_argument("--proxy", help="http://host:port")
    ap.add_argument("--no-submit", action="store_true", help="只填不提交，用于调试")
    ap.add_argument("--manual-captcha", action="store_true",
                    help="遇到验证码时停下等人工点击（需有头模式）")
    ap.add_argument("--keep-open", action="store_true",
                    help="提交后不关浏览器，方便看结果")
    ap.add_argument("--count", type=int, default=1, help="跑几轮")
    ap.add_argument("--gap", type=int, default=60, help="轮间隔秒数")
    args = ap.parse_args()

    if not HAS_PW:
        sys.exit("缺少 playwright：python -m pip install playwright")

    survey = json.load(open(args.survey, encoding="utf-8"))
    raw = json.load(open(args.answers, encoding="utf-8"))

    # 两种答案文件格式：
    #   单份  {"1":"2","5":"北京"}                 → 每轮都用这一份
    #   多轮  {"rounds":[{...},{...}]}            → 第 i 轮取 rounds[i-1]
    # 多轮格式用来让填空题每轮不一样，由 server.py 预先展开好再传进来。
    if isinstance(raw, dict) and isinstance(raw.get("rounds"), list) and raw["rounds"]:
        rounds = raw["rounds"]
        text_rounds = raw.get("option_texts") or []

        def answers_of(i):
            return rounds[(i - 1) % len(rounds)]

        def texts_of(i):
            if not text_rounds:
                return {}
            return text_rounds[(i - 1) % len(text_rounds)]
    else:
        def answers_of(i):
            return raw

        def texts_of(i):
            if not isinstance(raw, dict):
                return {}
            return raw.get("option_texts") or {}
    answers = answers_of(1)

    # 换问卷后先看题型支不支持，比填到一半报错好
    try:
        from parse_wjx import check_compat
        c = check_compat(survey)
        if not c["ok"]:
            print("[警告] 以下题型尚未实现，会被跳过并计入失败：")
            for topic, kind in c["unsupported"]:
                print(f"       Q{topic}  {kind}")
        if c["with_other"]:
            print(f"[提示] 含「其他」选项的题：{c['with_other']}，如带输入框需人工确认")
        if c.get("with_option_text"):
            print(f"[提示] 含联动补充输入的题：{c['with_option_text']}，"
                  f"按 option_texts 自动填写")
    except Exception:
        pass

    ok_n = fail_n = 0
    with sync_playwright() as p:
        for i in range(1, max(1, args.count) + 1):
            try:
                r = run_once(p, args, survey, answers_of(i), texts_of(i), i, args.count)
                if r.get("ok"):
                    ok_n += 1
                else:
                    fail_n += 1
            except Exception as e:
                fail_n += 1
                print(f"[第 {i} 轮] 异常 {type(e).__name__}: {str(e)[:120]}", flush=True)

            if i < args.count and args.gap > 0:
                wait = args.gap * random.uniform(0.85, 1.4)   # 不搞等距
                print(f"[轮间隔] 等待 {wait:.0f}s", flush=True)
                time.sleep(wait)

    print(f"\n[总计] 成功 {ok_n} · 失败 {fail_n}")


if __name__ == "__main__":
    main()
