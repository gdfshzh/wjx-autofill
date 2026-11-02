# -*- coding: utf-8 -*-
"""
survey_text.py —— 把已解析的问卷转成「题号 + 题型标注 + 选项」的纯文本

给「复制题目」功能用：一键把当前问卷整理成可直接粘贴到外部导入框的格式。
不依赖网络，只吃 survey.json 的结构，方便单独测试。

格式约定（按用户给的样例）：
  1.题干 [单选题]
  A.选项一
  B.选项二
  2.量表题干 [量表题]
  很不满意
  不满意
  3.矩阵题干 [矩阵题]
  很不满意 不满意 一般 满意 很满意
  寝室里面用电安全管理
  食堂就餐安全管理
  4.排序题干 [排序题]
  用电安全
  食堂就餐安全
  5.填空题题干

题型标注：
  单选/多选/量表/矩阵/排序/下拉/矩阵填空 都标；
  填空(单行/多行) 不标 —— 没有选项，导入端能直接识别为填空。

题号统一重排为 1..N：
  原题干自带「1.」「（2）」「第3题」会被剥掉，避免分段后重复出现 1.。
"""

import re

# type_name -> 标注。填空不在这里，保持无标注。
TYPE_TAGS = {
    "单选": "单选题",
    "多选": "多选题",
    "量表/滑块": "量表题",
    "矩阵量表": "矩阵题",
    "排序": "排序题",
    "下拉": "下拉题",
    "矩阵填空": "矩阵填空题",
}

# 题干结尾可能已经带了题型角标（问卷星偶尔会把提示留在题干里），先摘掉再统一加
TRAILING_TAG = re.compile(
    r"\s*\[(?:单选题|多选题|量表题|矩阵题|排序题|填空题|下拉题|矩阵填空题)\]\s*$")

# 题干开头的题号：1. / 1、/ 1) / 第1题 / （1）
PROMPT_NUM = [
    re.compile(r"^\s*第\s*\d+\s*题\s*[.、．:：]?\s*"),
    re.compile(r"^\s*[（(]\s*\d+\s*[)）]\s*"),
    re.compile(r"^\s*\d+\s*[.、．:：)）]\s*"),
]

# 选项开头自带的 A. / A、/ (A) / A)；问卷星的 dit 文本里经常带
OPT_PREFIX = re.compile(
    r"^\s*(?:[（(]\s*[A-Za-z]\s*[)）]|[A-Za-z])\s*[.、．:：)）]\s*")


def _clean(text):
    return re.sub(r"\s+", " ", str(text or "")).strip()


def clean_prompt(text):
    s = _clean(text)
    s = TRAILING_TAG.sub("", s).strip()
    # 多选题题干末尾常留一个空的「（）」占位，复制出去像漏字，直接去掉
    s = re.sub(r"[（(]\s*[)）]\s*$", "", s).strip()
    for pat in PROMPT_NUM:
        new = pat.sub("", s, count=1).strip()
        if new != s:
            s = new
            break
    return s


def _clean_option(text):
    s = _clean(text)
    stripped = OPT_PREFIX.sub("", s, count=1).strip()
    # 「A股」这类正常文本不该被剥成「股」：只有剥完非空才采用
    return stripped if stripped else s


def _letters(i):
    """0 -> A, 25 -> Z, 26 -> AA。选项超过 26 个也不至于串号。"""
    out = ""
    n = i + 1
    while n:
        n, r = divmod(n - 1, 26)
        out = chr(65 + r) + out
    return out


def _option_texts(q):
    out = []
    for o in q.get("options") or []:
        if isinstance(o, dict):
            t = o.get("text") or o.get("label") or o.get("value")
        else:
            t = o
        t = _clean_option(t)
        if t:
            out.append(t)
    return out


def _format_question(idx, q, warnings):
    prompt = clean_prompt(q.get("prompt"))
    kind = str(q.get("type_name") or "")
    lines = []

    head = f"{idx}.{prompt}" if prompt else f"{idx}."
    tag = TYPE_TAGS.get(kind)
    if tag:
        head += f" [{tag}]"
    lines.append(head)

    if kind in ("单选", "多选", "下拉"):
        opts = _option_texts(q)
        if not opts:
            warnings.append(f"第 {idx} 题（{kind}）没有解析到选项")
        for i, text in enumerate(opts):
            lines.append(f"{_letters(i)}.{text}")
        return lines

    if kind in ("量表/滑块", "排序"):
        opts = _option_texts(q)
        if not opts:
            warnings.append(f"第 {idx} 题（{kind}）没有解析到选项")
        # 量表和排序没有字母前缀，一行一个值
        lines.extend(opts)
        return lines

    if kind in ("矩阵量表", "矩阵填空"):
        mx = q.get("matrix") or {}
        scale = [_clean_option(x) for x in (mx.get("scale") or []) if _clean_option(x)]
        rows = [_clean_option(x) for x in (mx.get("rows") or []) if _clean_option(x)]
        if not scale:
            warnings.append(f"第 {idx} 题（{kind}）没有解析到量表刻度")
        if not rows:
            warnings.append(f"第 {idx} 题（{kind}）没有解析到矩阵行")
        if scale:
            lines.append(" ".join(scale))
        lines.extend(rows)
        return lines

    if kind.startswith("填空"):
        return lines

    warnings.append(f"第 {idx} 题题型「{kind or '未知'}」没有对应的输出规则，只保留题干")
    return lines


def format_survey(survey):
    """返回 {text, count, warnings}。

    text 末尾带一个换行；题与题之间不插空行 —— 样例里就是紧挨着的。
    题干本身没有题号的，也会被补成 N.，所以不再需要「无题号前加空行」。
    """
    questions = (survey or {}).get("questions") or []
    warnings = []
    lines = []
    for i, q in enumerate(questions, 1):
        lines.extend(_format_question(i, q, warnings))
    text = "\n".join(lines).strip()
    if text:
        text += "\n"
    return {"text": text, "count": len(questions), "warnings": warnings}
