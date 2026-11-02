#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""临时侦察：强制前端调出验证码，dump 真实 DOM 与可点元素。"""

import sys
import time

sys.path.insert(0, ".")

from playwright.sync_api import sync_playwright

from wjx_browser import STEALTH_JS, Human, handle_captcha, launch

URL = "https://v.wjx.cn/vm/Oki2m8H.aspx"


def dump(page, tag):
    print(f"\n=== {tag} ===", flush=True)
    for sel in ("#captchaWrap", "#captchaOut", "#captcha", "#captchabtn",
                "#aliyunCaptcha-window-popup", "[class*='aliyunCaptcha']"):
        try:
            loc = page.locator(sel)
            n = loc.count()
            vis = loc.first.is_visible() if n else False
            bb = loc.first.bounding_box() if n else None
            print(f"  {sel:<32} n={n} vis={vis} box={bb}")
        except Exception as e:
            print(f"  {sel:<32} err={str(e)[:50]}")

    print("\n  --- captchaWrap innerHTML ---")
    try:
        html = page.locator("#captchaWrap").first.inner_html()
        print(html[:2600])
    except Exception as e:
        print("  取不到:", str(e)[:80])

    print("\n  --- 可见候选元素 ---")
    try:
        items = page.evaluate("""() => {
            const out = [];
            document.querySelectorAll('div,span,a,button,canvas,iframe').forEach(el => {
                const r = el.getBoundingClientRect();
                const cs = getComputedStyle(el);
                if (r.width > 30 && r.height > 18 && r.top > -20 && r.top < innerHeight
                    && cs.visibility !== 'hidden' && cs.display !== 'none') {
                    out.push({tag: el.tagName,
                              cls: (el.className || '').toString().slice(0, 50),
                              text: (el.innerText || '').trim().slice(0, 16),
                              x: Math.round(r.x), y: Math.round(r.y),
                              w: Math.round(r.width), h: Math.round(r.height)});
                }
            });
            return out.filter(i => i.w < 700).slice(0, 40);
        }""")
        for it in items:
            print(f"    <{it['tag']:<6}> {it['cls'][:48]:<48} '{it['text']}' "
                  f"@({it['x']},{it['y']}) {it['w']}x{it['h']}")
    except Exception as e:
        print("  枚举失败:", str(e)[:80])


def main():
    with sync_playwright() as p:
        b = launch(p, headless=False)
        ctx = b.new_context(viewport={"width": 1280, "height": 900},
                            locale="zh-CN", timezone_id="Asia/Shanghai")
        ctx.add_init_script(STEALTH_JS)
        page = ctx.new_page()
        page.on("console", lambda m: print("  [console]", m.text[:120])
                if m.type in ("error", "warning") else None)

        print("[打开]", URL, flush=True)
        page.goto(URL, wait_until="domcontentloaded", timeout=60000)
        page.wait_for_selector("#divQuestion", timeout=30000)
        time.sleep(2)

        print("[强制调出验证码]", flush=True)
        r = page.evaluate("""() => {
            try {
                window.isCaptchaValid = false;
                window.useAliVerify = 1;
                if (typeof loadCaptchShow === 'function') {
                    loadCaptchShow(true);
                    return 'loadCaptchShow called';
                }
                return 'loadCaptchShow is ' + typeof loadCaptchShow;
            } catch (e) { return 'ERR ' + e.message; }
        }""")
        print("  执行结果:", r, flush=True)

        time.sleep(6)          # 等阿里云 SDK 加载并渲染
        dump(page, "调出后")
        page.screenshot(path="captcha_probe.png")

        print("\n[尝试自动通过]", flush=True)
        h = Human(page, slow=0.5, verbose=True)
        ok = handle_captcha(page, human=h, timeout=30)
        print("  结果:", ok, flush=True)
        time.sleep(3)
        dump(page, "点击之后")
        page.screenshot(path="captcha_after.png")

        b.close()


if __name__ == "__main__":
    main()
