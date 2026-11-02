#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
wjx_autofill.py —— 问卷星移动版自动化填写 / 提交模块

流程：GET 页面取 nonce 与 starttime -> 构造 submitdata -> 计算 jqsign -> POST 提交
零第三方依赖（标准库 urllib + cookiejar）。

用法：
  # 1) 先抓页面 + 解析出结构（用 parse_wjx.py 生成 survey.json）
  python parse_wjx.py page_mobile.html survey.json

  # 2) 从结构生成答案模板，手工填或改
  python wjx_autofill.py --shortid Oki2m8H --survey survey.json --make-template answers.json

  # 3) 按模板提交
  python wjx_autofill.py --shortid Oki2m8H --survey survey.json --answers answers.json

  # 4) 随机作答（压力/取样用）
  python wjx_autofill.py --shortid Oki2m8H --survey survey.json --random --count 5

参数全部外置，见 argparse。
"""

import argparse
import json
import random
import re
import sys
import time
import urllib.parse
import urllib.request
import http.cookiejar

BASE = "https://v.wjx.cn"
SHORTID_RE = re.compile(r"^[A-Za-z0-9]{4,24}$")


def normalize_shortid(raw: str) -> str:
    """把用户输入的任何形式收敛成短链本身。

    接受：Oki2m8H / https://v.wjx.cn/vm/Oki2m8H.aspx /
          v.wjx.cn/vm/Oki2m8H.aspx?x=1 / https://v.wjx.cn/jq/123456.aspx

    不做这一步的后果很直接：输入整条 URL 时会拼出
    https://v.wjx.cn/vm/https://v.wjx.cn/vm/Oki2m8H.aspx.aspx
    这种畸形地址，服务端当成不存在的问卷，报出来的错还像「被要求验证」。
    """
    s = (raw or "").strip()
    if not s:
        return ""

    m = re.findall(r"/(?:vm|jq|m)/([A-Za-z0-9]+)", s)
    # 取最后一个：畸形串 https://v.wjx.cn/vm/https://v.wjx.cn/vm/X.aspx.aspx
    # 里第一个 /vm/ 后面跟的是 "https"，不是短链
    m = [x for x in m if x.lower() not in ("http", "https", "www")]
    if m:
        return m[-1]

    s = re.split(r"[?#]", s)[0].rstrip("/")
    if s.lower().endswith(".aspx"):
        s = s[:-5]
    if "/" in s:
        s = s.rsplit("/", 1)[-1]
    return s
# 移动版页面 isStrictMobile=1，桌面 UA 会被判成非目标端，这里固定用 iOS Safari
UA_MOBILE = (
    "Mozilla/5.0 (iPhone; CPU iPhone OS 17_2 like Mac OS X) "
    "AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.2 Mobile/15E148 Safari/604.1"
)

# 服务端返回码 -> 含义（从 jqmobo2.js 的 afterSubmit 分支提取）
CODE_MEANING = {
    5: "其它错误",
    7: "需要安全校验（多选超限/高频，被风控拦截）",
    9: "答案不符合要求（n[1]=出错题号）",
    10: "提交成功",
    11: "跳转类响应",
    12: "分页/继续",
    13: "需登录/关注",
    16: "答案不合规（唯一性冲突等）",
    22: "服务端要求验证码",
    23: "答案不合规",
    35: "数字链接不可用",
}


class Session:
    """HTTP 会话。cookie 必须跨 GET/POST 保持，服务端用它绑定 nonce。

    human=True 时走 curl_cffi 的浏览器指纹（TLS + header 顺序与 iOS Safari 一致）；
    未安装 curl_cffi 则自动退回 urllib，功能不变，指纹仍是 Python 的。
    """

    def __init__(self, proxy: str = None, human: bool = True):
        from humanize import HAS_CURL_CFFI, HumanSession

        self.human = bool(human and HAS_CURL_CFFI)
        self.assets_loaded = 0
        self.beacons_sent = 0
        if self.human:
            self._h = HumanSession(proxy=proxy)
            self.jar = None
            self.opener = None
        else:
            self.jar = http.cookiejar.CookieJar()
            handlers = [urllib.request.HTTPCookieProcessor(self.jar)]
            if proxy:
                handlers.append(urllib.request.ProxyHandler({"http": proxy, "https": proxy}))
            self.opener = urllib.request.build_opener(*handlers)

    def get(self, url: str, referer: str = None, binary: bool = False):
        if self.human:
            return self._h.get(url, referer, binary)
        req = urllib.request.Request(url, method="GET")
        req.add_header("User-Agent", UA_MOBILE)
        req.add_header("Accept", "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8")
        req.add_header("Accept-Language", "zh-CN,zh;q=0.9")
        if referer:
            req.add_header("Referer", referer)
        with self.opener.open(req, timeout=30) as r:
            raw = r.read()
        if binary:
            return raw
        # 页面声明 UTF-8，个别老页面是 GBK，做一次兜底
        try:
            return raw.decode("utf-8")
        except UnicodeDecodeError:
            return raw.decode("gbk", "replace")

    def post(self, url: str, body: str, referer: str) -> str:
        if self.human:
            return self._h.post(url, body, referer)
        data = body.encode("utf-8")
        req = urllib.request.Request(url, data=data, method="POST")
        req.add_header("User-Agent", UA_MOBILE)
        req.add_header("Content-Type", "application/x-www-form-urlencoded; charset=UTF-8")
        req.add_header("X-Requested-With", "XMLHttpRequest")
        req.add_header("Origin", BASE)
        req.add_header("Referer", referer)
        req.add_header("Accept", "*/*")
        with self.opener.open(req, timeout=30) as r:
            raw = r.read()
        try:
            return raw.decode("utf-8")
        except UnicodeDecodeError:
            return raw.decode("gbk", "replace")


def _diagnose_page(html: str, shortid: str) -> str:
    """抓不到问卷页时，从返回内容里判断到底是哪种情况。

    原来的错误信息把「没拿到 divQuestion」一律归成「shortid 错误或问卷已关闭」，
    但实际更多是限流返回了一个提示页 —— 分不清原因就没法决定是重试还是换 IP。
    """
    html = html or ""
    t = re.search(r"<title>(.*?)</title>", html, re.S)
    title = (t.group(1).strip() if t else "").strip()
    text = re.sub(r"<[^>]+>", " ", html)
    text = re.sub(r"\s+", " ", text)[:4000]

    checks = [
        ("已停止", "问卷已停止收集"),
        ("已关闭", "问卷已关闭"),
        ("不存在", "问卷不存在，链接可能失效"),
        ("密码", "该问卷需要访问密码"),
        ("验证码", "被要求输入验证码"),
        ("频繁", "请求过于频繁，被限流"),
        ("安全校验", "触发了安全校验"),
        ("请用手机", "该问卷限制访问设备"),
    ]
    for kw, why in checks:
        if kw in text or kw in title:
            return why

    if len(html) < 800:
        return f"只返回了 {len(html)} 字节的空页面，多半被拦在网关"
    if not title:
        return f"返回 {len(html)} 字节但没有标题，不是问卷页"
    return f"页面打开了（标题「{title[:24]}」，{len(html)} 字节）但没有题目容器"


def fetch_meta(sess: Session, shortid: str, ua_profile: str = "mobile",
               retry: int = 2):
    """抓问卷页，返回提交所需的三件套：jqnonce / starttime / activityId。

    带重试：同一条链接刚才能打开、隔几十秒失败一次是常有的事
    （限流、CDN 回源抖动）。一次失败就抛错会让整个任务崩掉，
    而这时候通常重试一次就过了。
    """
    shortid = normalize_shortid(shortid)
    if not SHORTID_RE.match(shortid):
        # 在发请求前拦住：格式不对时重试三次也没意义，
        # 而且服务端对畸形路径的返回会被误判成「需要验证码」
        raise RuntimeError(
            f"短链格式不对：{shortid!r} —— 应该是 /vm/ 后面那串字母数字，例如 Oki2m8H")

    url = f"{BASE}/vm/{shortid}.aspx"
    html = ""
    last_err = None
    for attempt in range(retry + 1):
        try:
            html = sess.get(url)
        except Exception as e:          # 网络层异常也重试
            last_err = e
            html = ""
        if html and "divQuestion" in html:
            break
        if attempt < retry:
            time.sleep(random.uniform(1.0, 2.4) * (attempt + 1))

    if not html or "divQuestion" not in html:
        why = _diagnose_page(html, shortid)
        tail = f"；底层异常 {type(last_err).__name__}: {str(last_err)[:60]}" if last_err else ""
        raise RuntimeError(f"抓不到问卷页（{shortid}，已试 {retry + 1} 次）：{why}{tail}")

    m = re.search(r'var\s+jqnonce\s*=\s*"([^"]+)"', html)
    nonce = m.group(1) if m else ""
    # <input type="hidden" value="2026/11/1 15:06:38" id="starttime" .../>
    m = re.search(r'value="([^"]+)"\s+id="starttime"', html) or re.search(
        r'id="starttime"[^>]*value="([^"]+)"', html
    )
    starttime = m.group(1) if m else time.strftime("%Y/%m/%d %H:%M:%S")

    # activityId = DecodeId(x) = x ^ 2130030173
    m = re.search(r"DecodeId\((\d+)\)", html)
    activity_id = (int(m.group(1)) ^ 2130030173) if m else None

    m = re.search(r"var\s+totalPage\s*=\s*(\d+)", html)
    total_page = int(m.group(1)) if m else 1

    m = re.search(r"var\s+useAliVerify\s*=\s*(\d+)", html)
    use_ali = int(m.group(1)) if m else 0

    m = re.search(r"var\s+captchaType\s*=\s*'?(\d+)'?", html)
    captcha_type = int(m.group(1)) if m else 0

    m = re.search(r'var\s+rndnum\s*=\s*"([^"]*)"', html)
    rndnum = m.group(1) if m else f"{random.randint(1, 999999999)}.{random.randint(10000000, 99999999)}"

    return {
        "url": url,
        "html": html,
        "jqnonce": nonce,
        "starttime": starttime,
        "activity_id": activity_id,
        "total_page": total_page,
        "use_ali_verify": use_ali,
        "captcha_type": captcha_type,
        "rndnum": rndnum,
    }


def jqsign(nonce: str, ktimes: int) -> str:
    """dataenc：逐字符 XOR，密钥 = ktimes % 10，0 时取 1（与前端一致）。"""
    t = ktimes % 10
    if t == 0:
        t = 1
    return "".join(chr(ord(c) ^ t) for c in nonce)


# 问卷星前端 replace_specialChar 的映射：这些字符是 submitdata 的分隔符，
# 出现在选项补充文本里必须换成全角，否则会把一条答案拆成两段。
_SP_CHARS = str.maketrans({
    "$": "ξ", "}": "｝", "^": "ˆ", "|": "¦", "!": "！", "<": "＜",
})


def _safe_text(value) -> str:
    """补充文本只保留前 3000 字，并转义协议分隔符（与前端一致）。"""
    return str(value or "").translate(_SP_CHARS)[:3000]


def _option_map(q):
    return {str(o.get("value")): o for o in (q.get("options") or [])}


def build_submitdata(answers: dict, survey: dict, option_texts: dict = None) -> str:
    """拼答案串：题号$值，题间用 } 分隔。

    单选 -> "1"
    多选 -> "1|2|5"      （spChars[3] = '|'）
    矩阵 -> "1!5,2!4,..." （行号 + '!' + 分值，逗号分隔）

    带补充输入的选项（选完该项才显示的输入框）：
      单选 -> "4^具体机构名"
      多选 -> "1^甲|4^乙"
    分隔符是 spChars[2] = '^'，与 jqmobo2.js 的 getAnswer 分支一致。
    option_texts 结构：{"题号": {"选项值": "补充文本"}}，按轮次传入。

    多选值必须按选项在问卷里的原始顺序排列：前端永远按 DOM 顺序生成 "1|5"，
    如果这里输出 "5|1"，服务端虽仍接受（返回 10），但后台按选项组合归类时
    会当成另一个组合，表现为「统计里看不到变化」。
    """
    types = {str(q["topic"]): q for q in survey["questions"]}
    texts = option_texts or {}
    parts = []
    for topic in sorted(answers, key=lambda x: int(x)):
        q = types.get(str(topic))
        val = answers[topic]
        text_map = texts.get(str(topic)) or texts.get(topic) or {}
        if not isinstance(text_map, dict):
            text_map = {}
        else:
            text_map = {str(k): v for k, v in text_map.items()}

        if q and q["type_name"] == "矩阵量表":
            if not isinstance(val, list):
                raise ValueError(f"Q{topic} 是矩阵题，答案必须是分值列表")
            rows = q["matrix"]["rows"]
            if len(val) != len(rows):
                raise ValueError(f"Q{topic} 需要 {len(rows)} 个分值，给了 {len(val)}")
            seg = ",".join(f"{i + 1}!{v}" for i, v in enumerate(val))
        elif isinstance(val, list):
            order = {str(o["value"]): i for i, o in enumerate(q["options"])} if q else {}
            omap = _option_map(q) if q else {}
            pieces = []
            for v in sorted((str(x) for x in val), key=lambda x: order.get(x, 999)):
                piece = v
                opt = omap.get(v) or {}
                # 只给「该选项确实带补充框」的答案拼 ^，避免把普通选项文本也塞进去
                if opt.get("has_text") and text_map.get(v):
                    piece += "^" + _safe_text(text_map[v])
                pieces.append(piece)
            seg = "|".join(pieces)
        else:
            seg = str(val)
            if q:
                opt = _option_map(q).get(str(val)) or {}
                if opt.get("has_text") and text_map.get(str(val)):
                    seg += "^" + _safe_text(text_map[str(val)])
        parts.append(f"{topic}${seg}")
    return "}".join(parts)


def validate_answers(answers: dict, survey: dict) -> list:
    """提交前本地校验：必答覆盖、多选限项、矩阵行数。返回问题列表。"""
    problems = []
    for q in survey["questions"]:
        topic = str(q["topic"])
        if q["required"] and topic not in answers:
            problems.append(f"Q{topic} 必答但未提供")
            continue
        if topic not in answers:
            continue
        val = answers[topic]
        if q["type_name"] in ("单选", "多选") and isinstance(val, list):
            if q.get("max_select") and len(val) > q["max_select"]:
                problems.append(f"Q{topic} 最多选 {q['max_select']} 项，给了 {len(val)}")
            if q.get("exact_select") and len(val) != q["exact_select"]:
                problems.append(f"Q{topic} 限选 {q['exact_select']} 项，给了 {len(val)}")
            valid = {o["value"] for o in q["options"]}
            for v in val:
                if str(v) not in valid:
                    problems.append(f"Q{topic} 选项值 {v} 不在选项表内")
        if q["type_name"] == "矩阵量表":
            n = len(q["matrix"]["rows"])
            if not isinstance(val, list) or len(val) != n:
                problems.append(f"Q{topic} 矩阵需 {n} 个分值")
    # 反向：答案里有没有不存在的题号
    known = {str(q["topic"]) for q in survey["questions"]}
    for t in answers:
        if str(t) not in known:
            problems.append(f"答案里有不存在的题号 {t}")
    return problems


def random_answers(survey: dict) -> dict:
    """按题型约束随机作答：单选随机、多选满足限项、矩阵每行随机分值。

    多选的项数在 [下限, 上限] 内随机取，而不是固定 1 项：
      上限 = 题干里的「最多选N项」，没有则用选项总数
      下限 = 题干里的「至少选N项」，没有则 1
      「限选N项」直接固定为 N
    """
    out = {}
    for q in survey["questions"]:
        topic = str(q["topic"])
        if q["type_name"] == "单选":
            if q["options"]:
                out[topic] = random.choice(q["options"])["value"]

        elif q["type_name"] == "多选":
            opts = [o["value"] for o in q["options"]]
            if not opts:
                continue
            if q.get("exact_select"):
                n = q["exact_select"]
            else:
                lo = max(1, q.get("min_select") or 1)
                hi = min(q.get("max_select") or len(opts), len(opts))
                lo = min(lo, hi)          # 题干自相矛盾时以下限为准
                # 均匀分布会让「8 个选项勾 7 个」这种极端值频繁出现，
                # 而真人勾选通常集中在两三选项。峰值放在区间 1/3 处的三角分布，
                # 既保留长尾又不至于把多选当成全选。
                if hi - lo >= 2:
                    n = int(round(random.triangular(lo, hi, lo + (hi - lo) / 3)))
                else:
                    n = random.randint(lo, hi)
                n = max(lo, min(hi, n))
            n = max(1, min(n, len(opts)))
            out[topic] = random.sample(opts, n)

        elif q["type_name"] == "矩阵量表":
            n_rows = len(q["matrix"]["rows"])
            n_scale = len(q["matrix"]["scale"])
            # 中间偏右分布：真实答卷很少全选两端
            weights = [max(1, i + 1) for i in range(n_scale)]
            out[topic] = random.choices(range(1, n_scale + 1), weights=weights, k=n_rows)
    return out



_OPT_PREFIX = re.compile(r"^\s*(?:[（(]\s*[A-Za-z]\s*[)）]|[A-Za-z])\s*[.、．:：)）]\s*")


def fallback_option_texts(answers: dict, survey: dict) -> dict:
    """没配模板时的兜底：必填补充框用选项名填，避免空着被服务端打回。

    server.py 有完整的「模板 → 手填 → 兜底」链路；这个函数只服务
    wjx_autofill.py 的 CLI / run.py，保证它们不传 option_texts 也能提交。
    """
    out = {}
    for q in survey.get("questions") or []:
        topic = str(q.get("topic"))
        val = answers.get(topic)
        if val is None:
            continue
        vals = val if isinstance(val, list) else [val]
        optmap = {str(o.get("value")): o for o in (q.get("options") or [])}
        per = {}
        for v in vals:
            opt = optmap.get(str(v))
            if not opt or not opt.get("has_text") or not opt.get("text_required"):
                continue
            text = re.sub(r"\s+", " ", str(opt.get("text") or "")).strip()
            text = _OPT_PREFIX.sub("", text, count=1).strip() or text or "未填写"
            per[str(v)] = text
        if per:
            out[topic] = per
    return out

def submit_once(sess: Session, shortid: str, survey: dict, answers: dict,
                think_seconds: int = None, verbose: bool = True,
                meta: dict = None, elapsed: int = None, warm: bool = True,
                option_texts: dict = None) -> dict:
    """完整跑一轮：取 nonce -> 预热资源 -> 等待 -> 签名 -> 提交。返回结果字典。

    meta         : 外部已抓好的页面元数据，传入则跳过重复 GET
    elapsed      : 实际已等待秒数，仅用于日志
    warm         : 自己抓页面时，是否按浏览器顺序加载静态资源并上报埋点
    option_texts : 带补充输入框的选项文本，{"题号": {"选项值": "文本"}}
    """
    from humanize import reading_time, think_pause

    own_meta = meta is None
    if own_meta:
        meta = fetch_meta(sess, shortid)
        if warm and getattr(sess, "human", False):
            # 真人打开页面必然拉 CSS/JS/图片并触发埋点，只 GET 一次 HTML 是明显的空洞
            from humanize import fire_beacons, warm_up
            warm_up(sess, meta["html"], meta["url"], verbose)
            fire_beacons(sess, meta["html"], meta["url"], verbose)
    t0 = time.time()

    nq = len(survey.get("questions") or [])
    human = bool(getattr(sess, "human", False))
    if think_seconds is None:
        # 真人作答时长与题量相关（21 题约 100~200 秒），不是固定值
        think_seconds = reading_time(nq) if human else random.randint(90, 240)
    if think_seconds > 0:
        time.sleep(think_seconds)
        if human:
            time.sleep(think_pause())   # 最后一题到点提交之间的自然停顿

    # ktimes 在前端是「鼠标划过题目的累计次数」（每个 .field 的 mouseover +1），
    # 不是作答秒数。传 6 这类秒数等于宣称「只划过 6 次就交卷」，是风控眼里的异常样本。
    # 与题量挂钩才符合真实作答：21 题通常划过 25~60 次。
    ktimes = max(nq, int(nq * random.uniform(1.2, 2.8)))
    if ktimes < 1:
        ktimes = 1

    if option_texts is None:
        # CLI / run.py 没有模板链路，至少给必填补充框一个非空兜底
        option_texts = fallback_option_texts(answers, survey)
    submitdata = build_submitdata(answers, survey, option_texts)
    params = {
        "shortid": shortid,
        "starttime": meta["starttime"],
        "source": "directphone",
        "ktimes": str(ktimes),
        "rn": meta["rndnum"],
        "t": str(int(time.time() * 1000)),
        "jqnonce": meta["jqnonce"],
    }
    # captchaType=2 时前端无条件带 capt=2，即便 useAliVerify=0 也要带上，否则服务端按旧协议处理
    if meta["captcha_type"]:
        params["capt"] = str(meta["captcha_type"])
    if meta["jqnonce"]:
        params["jqsign"] = jqsign(meta["jqnonce"], ktimes)

    url = f"{BASE}/joinnew/processjq.ashx?" + urllib.parse.urlencode(params)
    body = "submitdata=" + urllib.parse.quote(submitdata, safe="")

    raw = sess.post(url, body, referer=meta["url"])
    raw = raw.strip()
    code_str, _, rest = raw.partition("〒")
    try:
        code = int(code_str)
    except ValueError:
        code = -1

    result = {
        "code": code,
        "meaning": CODE_MEANING.get(code, "未知"),
        "raw": raw[:400],
        "ktimes": ktimes,
        "payload": submitdata,
    }
    if verbose:
        print(f"  -> code={code} ({result['meaning']}) ktimes={ktimes}s")
        print(f"     raw: {raw[:200]}")
    return result


def make_template(survey: dict) -> dict:
    """生成带注释的答案模板：单选/多选占位，矩阵给满值数组。"""
    tpl = {}
    for q in survey["questions"]:
        k = str(q["topic"])
        if q["type_name"] == "单选":
            tpl[k] = q["options"][0]["value"]
        elif q["type_name"] == "多选":
            n = q.get("exact_select") or q.get("min_select") or 1
            tpl[k] = [o["value"] for o in q["options"][:n]]
        elif q["type_name"] == "矩阵量表":
            tpl[k] = [len(q["matrix"]["scale"])] * len(q["matrix"]["rows"])
    return tpl


def main():
    ap = argparse.ArgumentParser(description="问卷星移动版自动化填写")
    ap.add_argument("--shortid", required=True, help="问卷短链，如 Oki2m8H")
    ap.add_argument("--survey", required=True, help="parse_wjx.py 生成的 survey.json")
    ap.add_argument("--answers", help="答案 JSON")
    ap.add_argument("--make-template", metavar="OUT", help="导出答案模板后退出")
    ap.add_argument("--random", action="store_true", help="随机作答，忽略 --answers")
    ap.add_argument("--count", type=int, default=1, help="提交次数")
    ap.add_argument("--delay", type=int, default=0, help="两轮之间的间隔秒数")
    ap.add_argument("--think", type=int, default=None, help="单轮模拟作答秒数，默认 90-240 随机")
    ap.add_argument("--proxy", help="http://host:port")
    ap.add_argument("--dry-run", action="store_true", help="只构造 payload 不提交")
    args = ap.parse_args()

    survey = json.load(open(args.survey, encoding="utf-8"))

    if args.make_template:
        tpl = make_template(survey)
        with open(args.make_template, "w", encoding="utf-8") as f:
            json.dump(tpl, f, ensure_ascii=False, indent=2)
        print(f"[OK] 模板已写入 {args.make_template}，共 {len(tpl)} 题")
        return

    if args.random:
        answers = random_answers(survey)
    elif args.answers:
        answers = json.load(open(args.answers, encoding="utf-8"))
    else:
        sys.exit("需要 --answers 或 --random（或用 --make-template 生成模板）")

    problems = validate_answers(answers, survey)
    if problems:
        print("[校验未通过]")
        for p in problems:
            print("  -", p)
        sys.exit(2)

    if args.dry_run:
        print("[dry-run] submitdata:")
        print(build_submitdata(answers, survey, fallback_option_texts(answers, survey)))
        return

    ok = 0
    for i in range(args.count):
        print(f"[{i + 1}/{args.count}] 提交中…")
        sess = Session(proxy=args.proxy)  # 每轮独立 cookie，nonce 不复用
        try:
            r = submit_once(sess, args.shortid, survey, answers, think_seconds=args.think)
            if r["code"] == 10:
                ok += 1
        except Exception as e:
            print(f"  !! 异常: {type(e).__name__}: {e}")
        if i < args.count - 1 and args.delay:
            time.sleep(args.delay)

    print(f"\n完成：{ok}/{args.count} 成功")


if __name__ == "__main__":
    main()
