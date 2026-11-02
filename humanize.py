#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
humanize.py —— 把请求特征从「脚本」拉向「浏览器」

服务端能看到的信号分五层，脚本与真人的差距从下往上递减可控性：

  1 网络层  TLS 指纹(JA3/JA4)、HTTP/2 设置、TCP 时序      ← 只有真内核能一致
  2 协议层  请求头顺序与取值、Accept-Encoding、Header 大小写
  3 资源层  页面引用的 CSS/JS/图片是否被真的拉过、埋点是否上报
  4 行为层  作答时长、鼠标悬停计数、提交时间分布
  5 内容层  答案之间的逻辑一致性

本模块负责 2/3/4 层；1 层交给 curl_cffi 的 impersonate（真 Safari 指纹）；
5 层在 wjx_autofill.random_answers 里处理。

依赖（可选）：pip install curl_cffi
未安装时自动退回 urllib，功能不变但指纹仍是 Python 的。
"""

import math
import random
import re
import time
import urllib.parse

try:
    from curl_cffi import requests as _creq
    HAS_CURL_CFFI = True
except ImportError:  # 没装就退回标准库，行为不受影响
    _creq = None
    HAS_CURL_CFFI = False

# iOS Safari 17.2 的指纹，与页面 isStrictMobile=1 要求的移动端一致
DEFAULT_IMPERSONATE = "safari17_2_ios"

# 中文环境；curl_cffi 默认给 en-US，会和问卷语言对不上
LANG_ZH = "zh-CN,zh-Hans;q=0.9"


def _pick_impersonate():
    """不同 curl_cffi 版本内置的指纹名不同，挑一个存在的。"""
    if not HAS_CURL_CFFI:
        return None
    try:
        from curl_cffi.requests.impersonate import BrowserTypeLiteral  # noqa
    except Exception:
        pass
    for name in ("safari17_2_ios", "safari17_0", "safari15_5", "chrome124"):
        try:
            # 造一个临时 session 试一次，不支持会抛错
            _creq.Session(impersonate=name)
            return name
        except Exception:
            continue
    return None


# ---------------------------------------------------------------- 会话
class HumanSession:
    """带浏览器指纹的 HTTP 会话，接口与 wjx_autofill.Session 保持一致。"""

    def __init__(self, proxy: str = None, impersonate: str = None):
        self.impersonate = impersonate or _pick_impersonate()
        kwargs = {}
        if proxy:
            kwargs["proxies"] = {"http": proxy, "https": proxy}
        self.s = _creq.Session(impersonate=self.impersonate, **kwargs)
        self.proxy = proxy
        self.assets_loaded = 0
        self.beacons_sent = 0

    # ---- 基础请求
    def get(self, url: str, referer: str = None, binary: bool = False):
        # curl_cffi 的 impersonate 默认带 en-US，中文问卷的填写者用英文环境很反常，
        # 必须显式覆盖成中文，否则这一条就能被挑出来
        headers = {"Accept-Language": LANG_ZH}
        if referer:
            headers["Referer"] = referer
            # 二级资源请求要带 sec-fetch-dest，浏览器才会这么发
            headers["Sec-Fetch-Dest"] = "script" if url.endswith(".js") else (
                "style" if url.endswith(".css") else "image")
            headers["Sec-Fetch-Mode"] = "no-cors"
            headers["Sec-Fetch-Site"] = "cross-site"
        r = self.s.get(url, headers=headers, timeout=30, allow_redirects=True)
        if binary:
            return r.content
        try:
            return r.content.decode("utf-8")
        except UnicodeDecodeError:
            return r.content.decode("gbk", "replace")

    def post(self, url: str, body: str, referer: str):
        headers = {
            "Accept-Language": LANG_ZH,
            "Referer": referer,
            "Content-Type": "application/x-www-form-urlencoded; charset=UTF-8",
            "X-Requested-With": "XMLHttpRequest",
            "Origin": "https://v.wjx.cn",
            "Sec-Fetch-Dest": "empty",
            "Sec-Fetch-Mode": "cors",
            "Sec-Fetch-Site": "same-origin",
            "Accept": "*/*",
        }
        r = self.s.post(url, data=body.encode("utf-8"), headers=headers, timeout=30)
        try:
            return r.content.decode("utf-8")
        except UnicodeDecodeError:
            return r.content.decode("gbk", "replace")


# ---------------------------------------------------------------- 资源层
ASSET_PAT = re.compile(
    r'(?:src|href)=["\'](//image\.wjx\.cn[^"\']+|//usercsscdn\.wjx\.com[^"\']+|https?://[^"\']+\.(?:css|js|png|jpg|gif))["\']'
)


def collect_assets(html: str, limit: int = 14):
    """从页面里抓出会真正被浏览器请求的静态资源（CSS/JS/图片）。"""
    seen, out = set(), []
    for m in ASSET_PAT.finditer(html):
        url = m.group(1)
        if url.startswith("//"):
            url = "https:" + url
        # 跳过统计与第三方追踪，只保留页面渲染必需的
        if any(x in url for x in ("log.aliyuncs", "hm.baidu", "google", "doubleclick")):
            continue
        if url in seen:
            continue
        seen.add(url)
        out.append(url)
        if len(out) >= limit:
            break
    return out


def warm_up(sess, html: str, page_url: str, verbose: bool = False) -> int:
    """按浏览器顺序把页面的 CSS/JS/图片拉一遍。

    真人打开页面必然产生这些请求；只 GET 一次 HTML 就提交，
    在 CDN 访问日志里是明显的空洞。
    """
    assets = collect_assets(html)
    for url in assets:
        try:
            sess.get(url, referer=page_url, binary=True)
            sess.assets_loaded += 1
            time.sleep(random.uniform(0.03, 0.18))  # 并发加载的错峰
        except Exception:
            continue
    if verbose:
        print(f"  预热资源 {sess.assets_loaded}/{len(assets)} 个")
    return sess.assets_loaded


BEACON_PAT = re.compile(r'(//[a-z0-9.\-]*aliyuncs\.com[^"\'\s<>]*track\.gif\?[^"\'\s<>]*)')


def fire_beacons(sess, html: str, page_url: str, verbose: bool = False) -> int:
    """请求页面内嵌的埋点 gif，补齐「页面浏览」侧的日志。"""
    seen = set()
    for m in BEACON_PAT.finditer(html):
        url = m.group(1)
        if url.startswith("//"):
            url = "https:" + url
        if url in seen:
            continue
        seen.add(url)
        try:
            sess.get(url, referer=page_url, binary=True)
            sess.beacons_sent += 1
        except Exception:
            continue
    if verbose:
        print(f"  埋点上报 {sess.beacons_sent} 条")
    return sess.beacons_sent


# ---------------------------------------------------------------- 行为层
def reading_time(nq: int, fast: bool = False) -> float:
    """按题量估一个真人的作答时长（秒）。

    单题停留服从对数正态（多数 4~8 秒，偶有走神十几秒），
    再叠加一次整体抖动，避免所有答卷时长都挤在同一区间。
    """
    lo, mode, hi = (2.2, 4.5, 11.0) if fast else (3.0, 6.5, 16.0)
    total = 0.0
    for _ in range(max(1, nq)):
        total += random.lognormvariate(math.log(mode), 0.45)
    # 整份问卷的额外停顿：读说明、回看、犹豫
    total += random.uniform(3.0, 12.0)
    return max(lo * nq, min(total, hi * nq))


def think_pause() -> float:
    """提交前的短停顿：真人在「最后一题」到「点提交」之间会停一下。"""
    return random.uniform(1.2, 4.5)


def human_gap(base: float, spread: float = 0.35) -> float:
    """两次操作之间的间隔，对数正态分布，避免机械等距。"""
    return max(0.05, random.lognormvariate(math.log(max(base, 0.05)), spread))


def jitter_time_of_day() -> float:
    """批次之间的长间隔：真人有喝水、走开、看手机的空档。"""
    r = random.random()
    if r < 0.72:
        return random.uniform(40, 180)
    if r < 0.94:
        return random.uniform(180, 600)
    return random.uniform(600, 1800)


# ---------------------------------------------------------------- 自检
def status() -> dict:
    return {
        "curl_cffi": HAS_CURL_CFFI,
        "impersonate": _pick_impersonate() if HAS_CURL_CFFI else None,
    }


if __name__ == "__main__":
    print("指纹能力:", status())
    print("21 题估算作答时长: %.0f 秒" % reading_time(21))
    print("提交前停顿: %.1f 秒" % think_pause())
    print("批次间隔样本:", [round(jitter_time_of_day()) for _ in range(5)])
