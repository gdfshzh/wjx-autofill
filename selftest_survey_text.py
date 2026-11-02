# -*- coding: utf-8 -*-
"""selftest_survey_text.py —— 题目文本格式自检

跑法：
    python selftest_survey_text.py
"""

from survey_text import format_survey


def q(topic, type_name, prompt, options=None, matrix=None):
    d = {"topic": topic, "type_name": type_name, "prompt": prompt}
    if options is not None:
        d["options"] = [{"value": str(i + 1), "text": t} for i, t in enumerate(options)]
    if matrix:
        d["matrix"] = matrix
    return d


def main():
    survey = {"questions": [
        q(1, "单选", "1.您的年级是", ["2007级", "2006级", "2005级", "2004级"]),
        q(2, "量表/滑块", "2.请对学校的安全管理制度的满意度作个评价",
          ["很不满意", "不满意", "一般", "满意", "很满意"]),
        q(3, "矩阵量表", "3.请对学校的安全管理制度的满意度作个评价",
          matrix={"scale": ["很不满意", "不满意", "一般", "满意", "很满意"],
                  "rows": ["寝室里面用电安全管理", "食堂就餐安全管理"]}),
        q(4, "排序", "4.请按您认为的安全要素的重要程度排序",
          ["用电安全", "食堂就餐安全", "公共安全"]),
        q(5, "填空(单行)", "5.您觉得我们学校有哪些安全措施令您觉得很有安全感"),
    ]}
    expected = """1.您的年级是 [单选题]
A.2007级
B.2006级
C.2005级
D.2004级
2.请对学校的安全管理制度的满意度作个评价 [量表题]
很不满意
不满意
一般
满意
很满意
3.请对学校的安全管理制度的满意度作个评价 [矩阵题]
很不满意 不满意 一般 满意 很满意
寝室里面用电安全管理
食堂就餐安全管理
4.请按您认为的安全要素的重要程度排序 [排序题]
用电安全
食堂就餐安全
公共安全
5.您觉得我们学校有哪些安全措施令您觉得很有安全感
"""
    r = format_survey(survey)
    ok = 0
    total = 0

    def check(name, cond, detail=""):
        nonlocal ok, total
        total += 1
        if cond:
            ok += 1
            print(f"  [PASS] {name}")
        else:
            print(f"  [FAIL] {name}\n--- got ---\n{r['text']}\n--- want ---\n{expected}")

    check("样例逐字匹配", r["text"] == expected)
    check("题数正确", r["count"] == 5)
    check("没有误报警告", not r["warnings"], r["warnings"])

    # 题号清洗：1、/（2）/第3题 统一重排
    s2 = {"questions": [
        q(7, "单选", "1、第一题", ["甲"]),
        q(8, "单选", "（2）第二题", ["乙"]),
        q(9, "单选", "第3题第三题", ["丙"]),
        q(10, "多选", "第四题（）", ["A.选项甲", "B、选项乙"]),
    ]}
    r2 = format_survey(s2)
    lines = r2["text"].splitlines()
    check("重排题号", lines[0].startswith("1.第一题") and lines[2].startswith("2.第二题")
          and lines[4].startswith("3.第三题") and lines[6].startswith("4.第四题"), lines)
    check("多选标注", "4.第四题 [多选题]" in r2["text"], r2["text"])
    check("选项不重复加字母", "A.选项甲" in r2["text"] and "A.A.选项甲" not in r2["text"])
    check("空括号被清掉", "（）" not in r2["text"])

    # 缺选项要给出提示，但不能把整份文本搞崩
    s3 = {"questions": [q(1, "量表/滑块", "1.没有选项的量表")]}
    r3 = format_survey(s3)
    check("缺选项有提示", any("没有解析到选项" in x for x in r3["warnings"]), r3)
    check("缺选项仍保留题干", r3["text"].startswith("1.没有选项的量表 [量表题]"), r3["text"])

    print(f"\n{ok}/{total} 通过")
    return 0 if ok == total else 1


if __name__ == "__main__":
    raise SystemExit(main())
