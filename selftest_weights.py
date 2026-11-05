#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""选择题概率配置自检。

覆盖三层：
  quota.py            配额分配是否精确（概率 × 轮数 = 次数）
  choice_weights.py   校验、归一化、存储迁移、配置编译
  server.py           配置如何落到每一轮

运行：
    python selftest_weights.py
"""

import json
import os
import random
import tempfile
from collections import Counter

from choice_weights import (
    build_plans,
    config_stats,
    empty_store,
    load_store,
    preview_question,
    question_sig,
    save_store,
    validate_config,
)
from quota import QuotaPlan, allocate, plan_multi, plan_single
from server import build_round_answers

TOTAL = 0
FAILED = 0


def check(name, ok, detail=""):
    global TOTAL, FAILED
    TOTAL += 1
    if ok:
        print(f"  ok   {name}")
    else:
        FAILED += 1
        print(f"  FAIL {name}   {detail}")


def section(title):
    print(f"\n== {title}")


# ---------------------------------------------------------------- 夹具
def single_q(values=("1", "2", "3"), topic=1):
    return {
        "topic": topic, "type": 3, "type_name": "单选", "required": True,
        "prompt": f"{topic}.单选", "options": [
            {"value": v, "text": f"选项{v}"} for v in values],
    }


def multi_q(values=("1", "2", "3", "4"), topic=2, max_select=3):
    q = {
        "topic": topic, "type": 4, "type_name": "多选", "required": True,
        "prompt": f"{topic}.多选", "options": [
            {"value": v, "text": f"选项{v}"} for v in values],
    }
    if max_select:
        q["max_select"] = max_select
    return q


def matrix_q(topic=3, rows=3, scale=5):
    """矩阵题夹具，字段与 parse_wjx.parse_matrix 的输出保持一致。

    关键：矩阵题**没有 options**。表头刻度在 matrix.scale 里，提交值是
    每行的分值 1..N。早先这个夹具手动塞了一份 options，正好掩盖了
    QuotaPlan 用 q["options"] 取值的缺陷 —— 真实问卷解析出来 options 为空，
    配额算在空集合上，每轮给 0 行，填写器直接报「页面 16 行，答案 0 行」。
    夹具必须跟真实解析结果对齐，否则自检只能证明代码自洽。
    """
    return {
        "topic": topic, "type": 6, "type_name": "矩阵量表", "required": True,
        "prompt": f"{topic}.矩阵",
        "matrix": {"rows": [f"行{i}" for i in range(rows)],
                   "scale": [str(i) for i in range(1, scale + 1)]},
    }


# ---------------------------------------------------------------- 1. 配额
section("配额：单选")
seq = plan_single(["1", "2", "3"], {"1": 70, "2": 20, "3": 10}, 400, random.Random(1))
c = Counter(seq)
check("400 轮 70/20/10 → 280/80/40",
      (c["1"], c["2"], c["3"]) == (280, 80, 40), dict(c))
check("序列长度等于轮数", len(seq) == 400, len(seq))
check("洗牌后仍然只有原值", set(seq) <= {"1", "2", "3"}, set(seq))

seq = plan_single(["1", "2"], {"1": 50, "2": 50}, 3, random.Random(2))
c = Counter(seq)
check("3 轮 50/50 → 2:1 或 1:2", sorted(c.values()) == [1, 2], dict(c))

seq = plan_single(["1", "2", "3"], {"1": 1, "2": 1, "3": 1}, 7, random.Random(3))
c = Counter(seq)
check("7 轮三等分 → 3/2/2", sorted(c.values()) == [2, 2, 3], dict(c))

counts = allocate([0.7, 0.2, 0.1], 400)
check("最大余数法总和等于轮数", sum(counts) == 400, counts)

section("配额：多选")
rows = plan_multi(["1", "2", "3", "4"], {"1": 0.7, "2": 0.6, "3": 0.3, "4": 0.2},
                  400, 1, 4, random.Random(4))
c = Counter()
lens = Counter()
for r in rows:
    lens[len(r)] += 1
    for v in r:
        c[v] += 1
check("400 轮 0.7/0.6/0.3/0.2 → 280/240/120/80",
      (c["1"], c["2"], c["3"], c["4"]) == (280, 240, 120, 80), dict(c))
check("每轮至少 1 项", min(lens) >= 1, dict(lens))
check("每轮不超过 4 项", max(lens) <= 4, dict(lens))
check("平均项数 1.8", abs(sum(k * v for k, v in lens.items()) / 400 - 1.8) < 0.01)

rows = plan_multi(["1", "2", "3", "4"], {str(i): 0.5 for i in range(1, 5)},
                  400, 1, 2, random.Random(5))
lens = Counter(len(r) for r in rows)
check("上限 2 项时每轮恰好 2 项", lens == {2: 400}, dict(lens))

rows = plan_multi(list("123456"), {str(i): 0.5 for i in range(1, 7)},
                  200, 3, 3, random.Random(6))
lens = Counter(len(r) for r in rows)
check("固定 3 项时每轮恰好 3 项", lens == {3: 200}, dict(lens))

rows = plan_multi(["1", "2"], {"1": 1.0, "2": 0.0}, 10, 1, 2, random.Random(7))
check("单选项概率 1.0 时每轮都选", all("1" in r for r in rows))

section("配额：矩阵")
cell = plan_single(["1", "2", "3", "4", "5"],
                   {"1": 10, "2": 20, "3": 40, "4": 20, "5": 10}, 300, random.Random(8))
c = Counter(cell)
check("300 格 10/20/40/20/10 → 30/60/120/60/30",
      (c["1"], c["2"], c["3"], c["4"], c["5"]) == (30, 60, 120, 60, 30), dict(c))

# 真实问卷的矩阵题只有 matrix.rows / matrix.scale，没有 options。
# 这一段就是「YDmF2Wn Q21 报 0 行」的回归用例：取值必须来自 scale，
# 每轮必须给够 16 行，否则填写器会以行数不符中止整批。
mx16 = matrix_q(topic=21, rows=16, scale=5)
mplan = QuotaPlan(mx16, {"mode": "matrix",
                         "weights": {"1": 0.1, "2": 0.2, "3": 0.4,
                                     "4": 0.2, "5": 0.1}}, 400, random.Random(9))
check("矩阵无 options 时取值来自 scale",
      mplan.values == ["1", "2", "3", "4", "5"], mplan.values)
check("矩阵 400 轮每轮都是 16 行",
      {len(mplan.at(i)) for i in range(1, 401)} == {16},
      {len(mplan.at(i)) for i in range(1, 401)})
mc = mplan.counts()
check("矩阵 6400 格分布精确 10/20/40/20/10",
      (mc["1"], mc["2"], mc["3"], mc["4"], mc["5"]) == (640, 1280, 2560, 1280, 640),
      mc)

# 没有 scale 就没有可分配的取值，此时宁可不出配额（返回 None 保留原答案），
# 也不能编一个「每轮 1 行」的方案出来
no_scale = matrix_q(rows=16)
no_scale["matrix"]["scale"] = []
nplan = QuotaPlan(no_scale, {"mode": "matrix", "weights": {"1": 1.0}}, 10)
check("矩阵缺 scale 时不生成配额", nplan.at(1) is None, nplan.at(1))
check("矩阵缺 scale 时每轮答案保留原样",
      all(len(r["3"]) == 16 for r in
          build_round_answers({"questions": [no_scale]}, {"3": [3] * 16},
                              {}, False, 10, False, {"3": nplan})[0]))

# ---------------------------------------------------------------- 2. 校验
section("校验")
cfg, errs, _ = validate_config(single_q(), {"mode": "single",
                                            "weights": {"1": 70, "2": 20, "3": 10}})
check("单选归一化到 1", abs(sum(cfg["weights"].values()) - 1) < 1e-9, cfg)

_, errs, _ = validate_config(single_q(), {"mode": "single", "weights": {"1": -1}})
check("负权重被拒", bool(errs), errs)

_, errs, _ = validate_config(single_q(), {"mode": "single", "weights": {"1": 0}})
check("全 0 被拒", bool(errs), errs)

_, errs, _ = validate_config(single_q(), {"mode": "multi_target",
                                          "weights": {"1": 0.5}})
check("题型与模式不匹配被拒", bool(errs), errs)

_, errs, _ = validate_config(multi_q(), {"mode": "multi_target",
                                         "weights": {"1": 1.5, "2": 0.2}})
check("目标概率超 100% 被拒", bool(errs), errs)

cfg, errs, warns = validate_config(multi_q(max_select=2),
                                   {"mode": "multi_target",
                                    "weights": {str(i): 0.9 for i in range(1, 5)}})
check("目标和超出限项时自动缩放", not errs and bool(warns), (errs, warns))

# ---------------------------------------------------------------- 3. 编译
section("配置编译")
survey = {
    "title": "t", "question_count": 3, "required_count": 3,
    "questions": [single_q(), multi_q(), matrix_q()],
}
sig = "sig-test"
store = empty_store()
store["sig"] = sig
store["items"] = {
    "1": {"mode": "single", "weights": {"1": 0.7, "2": 0.2, "3": 0.1},
          "sig": question_sig(survey["questions"][0])},
    "2": {"mode": "multi_target", "weights": {"1": 0.7, "2": 0.6, "3": 0.3, "4": 0.2}},
    "3": {"mode": "matrix", "weights": {"1": 0.1, "2": 0.2, "3": 0.4, "4": 0.2, "5": 0.1}},
}
plans, issues = build_plans(survey, store, sig, 400)
check("三题全部编译", set(plans) == {"1", "2", "3"}, set(plans))
check("无异常提示", not issues, issues)
check("Q1 配额 280/80/40",
      plans["1"].counts() == {"1": 280, "2": 80, "3": 40}, plans["1"].counts())
check("Q2 配额 280/240/120/80",
      plans["2"].counts() == {"1": 280, "2": 240, "3": 120, "4": 80},
      plans["2"].counts())

bad = empty_store()
bad["sig"] = "other"
bad["items"] = {"1": {"mode": "single", "weights": {"1": 1.0}}}
plans2, issues2 = build_plans(survey, bad, sig, 100)
check("问卷指纹不符整份作废", not plans2 and bool(issues2), issues2)

stale = empty_store()
stale["sig"] = sig
stale["items"] = {"1": {"mode": "single", "weights": {"1": 1}, "sig": "old"}}
plans3, issues3 = build_plans(survey, stale, sig, 100)
check("题目选项变过时跳过该题", not plans3 and bool(issues3), issues3)

# ---------------------------------------------------------------- 4. 落轮
section("展开到每一轮")
rounds, trace, ftopics, texts = build_round_answers(
    survey, {}, {}, False, 400, False, plans)
check("展开 400 轮", len(rounds) == 400, len(rounds))
c1 = Counter(r["1"] for r in rounds)
c2 = Counter()
for r in rounds:
    for v in r["2"]:
        c2[v] += 1
check("Q1 落轮后仍是 280/80/40",
      (c1["1"], c1["2"], c1["3"]) == (280, 80, 40), dict(c1))
check("Q2 落轮后仍是 280/240/120/80",
      (c2["1"], c2["2"], c2["3"], c2["4"]) == (280, 240, 120, 80), dict(c2))
check("矩阵每题 3 行", all(len(r["3"]) == 3 for r in rounds))

short = build_round_answers(survey, {}, {}, False, 50, False, plans)[0]
check("轮数变小时配额跟着重算", Counter(r["1"] for r in short)["1"] == 35,
      Counter(r["1"] for r in short))

# ---------------------------------------------------------------- 5. 预览
section("预览")
pv = preview_question(survey["questions"][0],
                      {"mode": "single", "weights": {"1": 70, "2": 20, "3": 10}},
                      rounds=400)
check("预览给出的次数与配额一致",
      pv["counts"] == {"1": 280, "2": 80, "3": 40}, pv["counts"])
check("预览目标已归一化", abs(pv["target"]["1"] - 0.7) < 1e-9, pv["target"])
check("预览偏差为 0", pv["fit_error"] == 0.0, pv["fit_error"])

pv = preview_question(survey["questions"][1],
                      {"mode": "multi_target",
                       "weights": {"1": 0.7, "2": 0.6, "3": 0.3, "4": 0.2}},
                      rounds=400)
check("多选预览平均项数 1.8", abs(pv["mean_count"] - 1.8) < 0.01, pv["mean_count"])

# ---------------------------------------------------------------- 6. 存储
section("存储与迁移")
fd, path = tempfile.mkstemp(suffix=".json")
os.close(fd)
save_store(path, store)
back = load_store(path)
check("保存后能读回", back["sig"] == sig and len(back["items"]) == 3, back.get("items"))
check("版本号为 2", back["version"] == 2, back["version"])
os.remove(path)

legacy = {
    "version": 1, "sig": "sig-x", "updated": "",
    "rounds": {
        "1": {"8": {"mode": "single", "weights": {"1": 1.0, "2": 0.0}}},
        "2": {"8": {"mode": "single", "weights": {"1": 1.0, "2": 0.0}}},
        "3": {"8": {"mode": "single", "weights": {"1": 0.0, "2": 1.0}}},
        "4": {"5": {"mode": "single", "weights": {"1": 0.5, "2": 0.5}}},
    },
}
fd, path = tempfile.mkstemp(suffix=".json")
os.close(fd)
json.dump(legacy, open(path, "w", encoding="utf-8"), ensure_ascii=False)
mig = load_store(path)
os.remove(path)
check("v1 迁移出两道题", set(mig["items"]) == {"8", "5"}, set(mig["items"]))
check("多轮不同配置取多数（2 次那份）",
      mig["items"]["8"]["weights"]["1"] == 1.0, mig["items"]["8"]["weights"])
check("迁移后不再有 rounds 键", "rounds" not in mig, list(mig))
check("记录迁移来源", "v1" in (mig.get("migrated_from") or ""), mig.get("migrated_from"))

fd, path = tempfile.mkstemp(suffix=".json")
os.close(fd)
open(path, "w", encoding="utf-8").write("{ not json")
check("坏文件回落空配置", load_store(path)["items"] == {})
os.remove(path)

check("统计只数题目", config_stats(store)["pairs"] == 3, config_stats(store))
check("v2 不再有轮次维度", config_stats(store)["configured_rounds"] == [])

# ---------------------------------------------------------------- 汇总
print(f"\n{'=' * 46}")
print(f"共 {TOTAL} 项，通过 {TOTAL - FAILED}，失败 {FAILED}")
print("=" * 46)
raise SystemExit(1 if FAILED else 0)
