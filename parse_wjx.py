# -*- coding: utf-8 -*-
"""问卷星移动版页面解析器：提取题目类型/选项/必填/限选/页面结构，输出 JSON。

用法: python parse_wjx.py page_mobile.html out.json
"""
import json
import re
import sys
from urllib.parse import unquote

from bs4 import BeautifulSoup

QTYPE = {
    1: "填空(单行)",
    2: "填空(多行)",
    3: "单选",
    4: "多选",
    5: "量表/滑块",
    6: "矩阵量表",
    7: "下拉",
    8: "排序",
    11: "矩阵填空",
}

# 题干里的限选规则，问卷星不写在属性里，只能从文案抠。
# 必须容忍换行：题干是 get_text("\n") 拼出来的，"最多选择3项"经常被拆成
# "最多\n选择\n3项"（提示单独包了一层块级元素），原来只写 \s* 在"项"前面，
# 中间那几处换行直接让整条规则失配，max_select 变成 None，前端限项形同虚设。
LIMIT_PAT = [
    (re.compile(r"最多\s*[选择]{1,2}\s*(\d+)\s*项"), "max"),
    (re.compile(r"最多\s*(?:可以|可)?\s*选\s*(\d+)\s*项"), "max"),
    (re.compile(r"至多\s*[选择]{1,2}\s*(\d+)\s*项"), "max"),
    (re.compile(r"至少\s*[选择]{1,2}\s*(\d+)\s*项"), "min"),
    (re.compile(r"限选\s*(\d+)\s*项"), "exact"),
    (re.compile(r"选\s*(\d+)\s*项"), "exact"),
]


def clean(text: str) -> str:
    if not text:
        return ""
    t = text.replace("\u3000", " ").replace("\xa0", " ")
    t = re.sub(r"[ \t]+", " ", t)
    t = re.sub(r"\n{2,}", "\n", t)
    return t.strip()


def extract_limit(prompt: str):
    out = {}
    for pat, kind in LIMIT_PAT:
        m = pat.search(prompt)
        if m:
            n = int(m.group(1))
            if kind == "max":
                out.setdefault("max_select", n)
            elif kind == "min":
                out.setdefault("min_select", n)
            else:
                out.setdefault("exact_select", n)
            break
    return out


def parse_options(field):
    """单选/多选：从 .ui-radio / .ui-checkbox 提取 value 与显示文本。"""
    opts = []
    for box in field.select("div.ui-radio, div.ui-checkbox"):
        inp = box.find("input")
        lab = box.find("div", class_="label")
        if not inp or not lab:
            continue
        # dit 属性是 URL 编码的原始选项文本，比 label 的渲染文本更干净
        raw = lab.get("dit")
        text = unquote(raw) if raw else clean(lab.get_text())
        opts.append(
            {
                "value": inp.get("value"),
                "id": inp.get("id"),
                "text": clean(text),
            }
        )
    if opts:
        return opts
    # 量表/排序等题型不一定用 ui-radio，退一步找任何带 dit 的 label。
    # 只认 .label，避免把题干节点当选项。
    for lab in field.select("div.label[dit], span.label[dit], li.label[dit]"):
        raw = lab.get("dit")
        text = clean(unquote(raw) if raw else lab.get_text())
        if text:
            opts.append({"value": text, "id": lab.get("id"), "text": text})
    return opts


def parse_matrix(field):
    """矩阵题：表头刻度 + 每行子项。"""
    table = field.find("table")
    if not table:
        return None
    # 移动端矩阵表会为每若干行重复插入一个 trlabel 表头行（滚动时吸顶用），
    # 刻度只取第一个表头，否则会重复 N 次
    first_label = table.select_one("tr.trlabel")
    scale = [clean(th.get_text()) for th in first_label.find_all("th")] if first_label else []
    rows = []
    for tr in table.select("tr.rowtitle"):
        title_cell = tr.find("td", class_="title")
        rows.append(clean(title_cell.get_text()) if title_cell else "")
    # 每行的评分单元格：fid=q21_0 这类
    rate_rows = []
    for tr in table.find_all("tr"):
        fid = tr.get("fid")
        if not fid:
            continue
        cells = []
        for a in tr.find_all("a"):
            cells.append({"dval": a.get("dval"), "state": a.get("class", [""])[0]})
        rate_rows.append({"fid": fid, "rowIndex": tr.get("rowIndex"), "cells": cells})
    return {
        "scale": scale,
        "rows": rows,
        "rate_rows": rate_rows,
        "answer_format": "每行一个 q{topic}_{rowIndex} = 刻度值",
    }


def parse(html_path: str, source_url: str = "") -> dict:
    html = open(html_path, encoding="utf-8").read()
    return parse_html(html, source_url)


def parse_html(html: str, source_url: str = "") -> dict:
    """直接吃 HTML 字符串，GUI 抓完页面后不用落盘就能解析。

    source_url 必须由调用方传：页面里没有稳定的短链标识
    （只有分享按钮里可能夹带别人的 /vm/xxx），猜出来的值会张冠李戴。
    """
    soup = BeautifulSoup(html, "lxml")

    title = clean(soup.title.get_text()) if soup.title else ""
    desc_node = soup.find(id="divDesc")
    description = clean(desc_node.get_text("\n")) if desc_node else ""

    # 分段标题与题目按 DOM 顺序走，才能知道每题属于哪一部分
    sections = []
    questions = []
    container = soup.find(id="divQuestion")
    if container is None:
        raise SystemExit("未找到 #divQuestion，页面结构可能已变更")

    current_section = "（未分组）"
    for node in container.find_all(["div"], recursive=True):
        cls = node.get("class") or []
        if "cutfield" in cls:
            txt = clean(node.get_text())
            if txt:
                current_section = txt
                sections.append(txt)
            continue
        if "field" not in cls:
            continue
        topic = node.get("topic")
        if not topic:
            continue
        qtype = int(node.get("type", "0") or 0)
        req = node.get("req") == "1"
        label = node.find("div", class_="field-label")
        prompt_node = label.find("div", class_="topichtml") if label else None
        # 题型提示（【多选题】【最多选择3项】）单独包在 span.qtypetip 里，
        # 先取文本再从树里摘掉，否则会跟着 get_text("\n") 混进题干
        tip = prompt_node.find("span", class_="qtypetip") if prompt_node else None
        tip_text = clean(tip.get_text()) if tip else ""
        if tip is not None:
            tip.extract()
        prompt = clean(prompt_node.get_text("\n")) if prompt_node else ""

        q = {
            "topic": int(topic),
            "section": current_section,
            "type": qtype,
            "type_name": QTYPE.get(qtype, f"未知({qtype})"),
            "required": req,
            "prompt": prompt,
            "type_hint": tip_text,
        }
        # 限项可能写在题干里，也可能只在提示角标里，两边都喂进去
        q.update(extract_limit(prompt + " " + tip_text))

        if qtype in (3, 4):
            q["options"] = parse_options(node)
            q["option_count"] = len(q["options"])
            # 有“其他”选项时通常伴随补充输入框
            q["has_other_input"] = any("其他" in o["text"] for o in q["options"])
        elif qtype in (5, 8):
            # 量表/滑块与排序题在移动端同样用可选项列表渲染，
            # 之前没取 options，复制题目时这两类只能输出题干。
            q["options"] = parse_options(node)
            q["option_count"] = len(q["options"])
        elif qtype in (6, 11):
            q["matrix"] = parse_matrix(node)
        elif qtype in (1, 2):
            q["has_other_input"] = False
        questions.append(q)

    return {
        "source_url": source_url or "",
        "title": title,
        "description": description,
        "sections": sections,
        "question_count": len(questions),
        "required_count": sum(1 for q in questions if q["required"]),
        "type_summary": {},
        "questions": questions,
    }


# 浏览器填写器已经实现的题型；其余题型会在填题时抛错，需要补代码
SUPPORTED_TYPES = {"单选", "多选", "矩阵量表", "下拉", "填空(单行)", "填空(多行)"}


def check_compat(survey: dict) -> dict:
    """检查这份问卷能不能被现有填写器完整处理。

    返回 {ok, unsupported:[(题号, 题型)], with_other:[题号]}。
    换问卷后先跑一次，比填到一半报错要好。
    """
    unsupported, with_other = [], []
    for q in survey.get("questions", []):
        if q.get("type_name") not in SUPPORTED_TYPES:
            unsupported.append((q.get("topic"), q.get("type_name")))
        if q.get("has_other_input"):
            with_other.append(q.get("topic"))
    return {
        "ok": not unsupported,
        "unsupported": unsupported,
        "with_other": with_other,
    }


def main():
    # 用法：python parse_wjx.py <页面html> [输出json] [问卷链接]
    src = sys.argv[1] if len(sys.argv) > 1 else "page_mobile.html"
    dst = sys.argv[2] if len(sys.argv) > 2 else "survey.json"
    url = sys.argv[3] if len(sys.argv) > 3 else ""
    data = parse(src, url)
    for q in data["questions"]:
        k = q["type_name"]
        data["type_summary"][k] = data["type_summary"].get(k, 0) + 1
    with open(dst, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
    print(f"[OK] {data['question_count']} 题 -> {dst}")
    print("题型分布:", data["type_summary"])


if __name__ == "__main__":
    main()
