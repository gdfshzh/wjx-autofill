# -*- coding: utf-8 -*-
"""selftest_linked.py —— 联动选项（选完选项再补充填写）自检

跑法：
    python selftest_linked.py

覆盖：
  1. 解析器从 .ui-text 提取 has_text / text_id / text_required
  2. submitdata 的 `选项值^补充文本` 编码，单选与多选
  3. 模板候选、手填文本、选项名兜底的优先级
  4. 与概率抽样衔接：概率只决定选项，文本跟随选中的值
"""

import json
import sys

from parse_wjx import parse_html
from wjx_autofill import build_submitdata
from server import build_round_answers
from choice_weights import empty_store, build_round_samplers, question_sig

HTML = """<!doctype html><html><head><title>联动题测试</title></head><body>
<div id="divQuestion">
  <div class="field" topic="8" type="3" req="1">
    <div class="field-label"><div class="topichtml">8.您所在学校的实施机构是?</div></div>
    <div class="ui-radio">
      <span><input type="radio" value="1" id="q8_1" name="q8"/></span>
      <div class="label" dit="A.%E5%AD%A6%E6%A0%A1%E8%87%AA%E8%A1%8C%E7%BB%84%E7%BB%87" for="q8_1">A.学校自行组织</div>
      <div class="ui-text" id="utq8_1"><input type="text" id="tqq8_1" class="OtherRadioText" rel="q8_1" required="true"/></div>
    </div>
    <div class="ui-radio">
      <span><input type="radio" value="2" id="q8_2" name="q8"/></span>
      <div class="label" dit="B.%E5%85%B6%E4%BB%96" for="q8_2">B.其他</div>
      <div class="ui-text" id="utq8_2"><input type="text" id="tqq8_2" class="OtherRadioText" rel="q8_2" required="true"/></div>
    </div>
  </div>
  <div class="field" topic="9" type="4" req="1">
    <div class="field-label"><div class="topichtml">9.多选联动</div></div>
    <div class="ui-checkbox">
      <span><input type="checkbox" value="1" id="q9_1" name="q9"/></span>
      <div class="label" dit="A.%E7%94%B2" for="q9_1">A.甲</div>
      <div class="ui-text" id="utq9_1"><input type="text" id="tqq9_1" class="OtherRadioText" rel="q9_1" required="true"/></div>
    </div>
    <div class="ui-checkbox">
      <span><input type="checkbox" value="2" id="q9_2" name="q9"/></span>
      <div class="label" dit="B.%E4%B9%99" for="q9_2">B.乙</div>
      <div class="ui-text" id="utq9_2"><input type="text" id="tqq9_2" class="OtherRadioText" rel="q9_2" required="true"/></div>
    </div>
  </div>
</div></body></html>"""


def main():
    ok = total = 0

    def check(name, cond, detail=""):
        nonlocal ok, total
        total += 1
        if cond:
            ok += 1
            print(f"  [PASS] {name}")
        else:
            print(f"  [FAIL] {name} {detail}")

    s = parse_html(HTML)
    q8 = next(q for q in s["questions"] if q["topic"] == 8)
    q9 = next(q for q in s["questions"] if q["topic"] == 9)

    print("1) 解析")
    check("Q8 识别为联动题", q8["has_option_text"] is True)
    check("Q8 四个补充框", q8["option_text_count"] == 2, q8["option_text_count"])
    check("Q8 选项1 必填", q8["options"][0]["text_required"] is True)
    check("Q8 选项1 输入框 id", q8["options"][0]["text_id"] == "tqq8_1")
    check("Q9 多选也识别", q9["has_option_text"] is True and q9["option_text_count"] == 2)

    print("2) submitdata 编码")
    payload = build_submitdata({"8": "2"}, s, {"8": {"2": "某某机构"}})
    check("单选 ^ 编码", payload == "8$2^某某机构", payload)
    payload = build_submitdata({"9": ["2", "1"]}, s,
                               {"9": {"1": "甲说明", "2": "乙说明"}})
    check("多选按选项顺序 + 各自 ^", payload == "9$1^甲说明|2^乙说明", payload)
    payload = build_submitdata({"8": "2"}, s)
    check("没有文本时不拼 ^", payload == "8$2", payload)
    payload = build_submitdata({"8": "1"}, s, {"8": {"1": "A|B^C$D}E!F<G"}})
    check("分隔符被转义", "¦" in payload and "ˆ" in payload and "|" not in payload.split("$")[1],
          payload)

    print("3) 模板 / 手填 / 兜底优先级")
    base = {"8": "2"}
    rounds, _, _, texts = build_round_answers(
        s, base, {}, False, 1, False, None,
        option_pools={"8": {"2": ["模板机构"]}}, manual_texts={"8": {"2": "手填机构"}})
    check("模板优先于手填", texts[0]["8"]["2"] == "模板机构", texts)
    rounds, _, _, texts = build_round_answers(
        s, base, {}, False, 1, False, None, manual_texts={"8": {"2": "手填机构"}})
    check("无模板用手填", texts[0]["8"]["2"] == "手填机构", texts)
    rounds, _, _, texts = build_round_answers(s, base, {}, False, 1, False, None)
    check("都没有用选项名兜底", texts[0]["8"]["2"] == "其他", texts)
    check("兜底后 payload 不为空",
          build_submitdata(rounds[0], s, texts[0]) == "8$2^其他")

    print("4) 与概率配置衔接")
    store = empty_store()
    store["sig"] = "sig-test"
    store["rounds"] = {
        "1": {"8": {"mode": "single", "weights": {"1": 1, "2": 0},
                    "sig": question_sig(q8)}},
        "2": {"8": {"mode": "single", "weights": {"1": 0, "2": 1},
                    "sig": question_sig(q8)}},
    }
    samplers, issues = build_round_samplers(s, store, "sig-test", 2)
    check("两轮抽样器装配成功", not issues and set(samplers) == {1, 2}, issues)
    rounds, _, _, texts = build_round_answers(
        s, {"8": "1"}, {}, False, 2, False, samplers,
        option_pools={"8": {"1": ["学校自组"], "2": ["其他机构"]}})
    check("第1轮选项1配文本1",
          rounds[0]["8"] == "1" and texts[0]["8"]["1"] == "学校自组", texts[0])
    check("第2轮选项2配文本2",
          rounds[1]["8"] == "2" and texts[1]["8"]["2"] == "其他机构", texts[1])
    check("概率不影响文本映射",
          build_submitdata(rounds[1], s, texts[1]) == "8$2^其他机构")

    print("5) 多选每轮取不同候选")
    rounds, _, _, texts = build_round_answers(
        s, {"9": ["1", "2"]}, {}, True, 3, False, None,
        option_pools={"9": {"1": ["甲A", "甲B", "甲C"], "2": ["乙A", "乙B", "乙C"]}})
    pairs = [(x["9"], texts[i]["9"]) for i, x in enumerate(rounds)]
    check("三轮都带两个补充文本",
          all(len(t) == 2 for _, t in pairs), pairs)
    check("候选确实在轮换", len({tuple(sorted(t.items())) for _, t in pairs}) >= 2, pairs)

    print(f"\n{ok}/{total} 通过")
    return 0 if ok == total else 1


if __name__ == "__main__":
    raise SystemExit(main())
