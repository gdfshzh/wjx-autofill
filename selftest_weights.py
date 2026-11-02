#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
selftest_weights.py —— 选项概率模块自检

跑法：
    python selftest_weights.py

覆盖：
  1. 单选权重归一化与经验分布
  2. 多选目标边缘概率（固定项数 / 区间限项）
  3. 相对权重模式不越限、0 权重选项不被选中
  4. 矩阵量表按分值分布
  5. 输入校验：负数 / 未知选项 / 全 0 / 不可行目标
  6. 轮次隔离：同一题不同轮次各用各的配置
  7. 配置落盘与重新读取
"""

import json
import os
import random
import shutil
import tempfile
from collections import Counter

from choice_weights import (
    QuestionSampler,
    build_round_samplers,
    config_stats,
    empty_store,
    load_store,
    preview_question,
    save_store,
    validate_config,
)
from server import build_round_answers


def q(topic, type_name, n=4, **kw):
    if type_name == "矩阵量表":
        d = {"topic": topic, "type_name": type_name,
             "matrix": {"rows": ["r1", "r2", "r3"],
                        "scale": ["很差", "较差", "一般", "较好", "很好"]}}
        d.update(kw)
        return d
    d = {"topic": topic, "type_name": type_name,
         "options": [{"value": str(i), "text": f"选项{i}"} for i in range(1, n + 1)]}
    d.update(kw)
    return d


def sample_many(sampler, n=20000, seed=1):
    rng = random.Random(seed)
    c, lens = Counter(), Counter()
    for _ in range(n):
        v = sampler.draw(rng)
        if isinstance(v, list):
            lens[len(v)] += 1
            for x in v:
                c[str(x)] += 1
        else:
            c[str(v)] += 1
    return c, lens, n


def main():
    ok = 0
    total = 0

    def check(name, cond, detail=""):
        nonlocal ok, total
        total += 1
        if cond:
            ok += 1
            print(f"  [PASS] {name}")
        else:
            print(f"  [FAIL] {name} {detail}")

    print("1) 单选分布")
    s1 = q(1, "单选", 4)
    cfg, errs, warns = validate_config(s1, {"mode": "single",
                                            "weights": {"1": 50, "2": 30, "3": 15, "4": 5}})
    check("归一化无错误", not errs, errs)
    check("权重和为 1", abs(sum(cfg["weights"].values()) - 1) < 1e-9)
    c, _, n = sample_many(QuestionSampler(s1, cfg), 20000)
    got = {k: v / n for k, v in c.items()}
    check("经验分布贴近 50/30/15/5",
          all(abs(got[k] - cfg["weights"][k]) < 0.015 for k in got), got)

    print("2) 多选目标边缘概率")
    m1 = q(5, "多选", 4, max_select=2)
    cfg, errs, warns = validate_config(m1, {"mode": "multi_target",
                                            "weights": {"1": 0.8, "2": 0.7,
                                                        "3": 0.3, "4": 0.2}})
    check("目标配置通过", not errs, errs)
    check("目标概率和=2", abs(sum(cfg["weights"].values()) - 2) < 1e-6)
    s = QuestionSampler(m1, cfg)
    c, lens, n = sample_many(s, 20000)
    got = {k: v / n for k, v in c.items()}
    check("边缘概率误差 < 2%",
          all(abs(got[k] - cfg["weights"][k]) < 0.02 for k in got), got)
    check("每轮都选 2 项", set(lens) == {2}, dict(lens))

    m2 = q(6, "多选", 4)   # 无 max_select：1-4 项
    cfg2, errs, warns = validate_config(m2, {"mode": "multi_target",
                                             "weights": {"1": 0.5, "2": 0.5,
                                                         "3": 0.5, "4": 0.5}})
    check("无上限也能拟合", not errs, errs)
    s2 = QuestionSampler(m2, cfg2)
    c, lens, n = sample_many(s2, 10000, seed=7)
    got = {k: v / n for k, v in c.items()}
    check("无上限边缘概率误差 < 2%",
          all(abs(got[k] - 0.5) < 0.02 for k in got), got)
    check("无上限项数落在 1-4", min(lens) >= 1 and max(lens) <= 4, dict(lens))

    print("3) 相对权重模式")
    cfg, errs, _ = validate_config(m1, {"mode": "multi_weight",
                                        "weights": {"1": 1, "2": 0, "3": 0, "4": 0}})
    s = QuestionSampler(m1, cfg)
    c, lens, n = sample_many(s, 3000)
    check("0 权重选项永不出现", set(c) == {"1"}, dict(c))
    check("仍遵守上限 2 项", max(lens) <= 2, dict(lens))

    print("4) 矩阵量表")
    mx = q(9, "矩阵量表")
    cfg, errs, _ = validate_config(mx, {"mode": "matrix",
                                        "weights": {"1": 10, "2": 20, "3": 40,
                                                    "4": 20, "5": 10}})
    check("矩阵配置通过", not errs, errs)
    pv = preview_question(mx, cfg, samples=2000, seed=3)
    check("矩阵每行 5 档、3 行", pv["mean_count"] == 3, pv["mean_count"])
    check("矩阵中间档最高",
          pv["actual"]["3"] > pv["actual"]["1"] and pv["actual"]["3"] > pv["actual"]["5"],
          pv["actual"])

    print("5) 输入校验")
    _, errs, _ = validate_config(s1, {"mode": "single", "weights": {"1": -1, "2": 2}})
    check("负权重被拒", bool(errs))
    _, errs, _ = validate_config(s1, {"mode": "single", "weights": {"9": 1}})
    check("未知选项被拒", bool(errs))
    _, errs, _ = validate_config(s1, {"mode": "single", "weights": {"1": 0, "2": 0}})
    check("全 0 被拒", bool(errs))
    cfg, errs, warns = validate_config(m1, {"mode": "multi_target",
                                            "weights": {"1": 1, "2": 1,
                                                        "3": 0.1, "4": 0.1},
                                            "count": 3})
    check("不可行目标被拒", bool(errs), errs)
    cfg, errs, warns = validate_config(m1, {"mode": "multi_target",
                                            "weights": {"1": 1, "2": 1, "3": 1, "4": 0}})
    check("目标和超上限会自动缩放", not errs and bool(warns), (errs, warns))

    print("6) 轮次隔离")
    survey = {"questions": [q(1, "单选", 4), q(2, "单选", 4),
                            {"topic": 3, "type_name": "填空(单行)", "options": []}]}
    store = empty_store()
    store["sig"] = "sig-x"
    store["rounds"] = {
        "1": {"1": {"mode": "single", "weights": {"1": 1, "2": 0, "3": 0, "4": 0},
                    "sig": ""}},
        "2": {"1": {"mode": "single", "weights": {"1": 0, "2": 1, "3": 0, "4": 0},
                    "sig": ""}},
    }
    # 补上题目指纹，模拟真实保存路径
    for r in ("1", "2"):
        store["rounds"][r]["1"]["sig"] = __import__("choice_weights").question_sig(survey["questions"][0])
    samplers, issues = build_round_samplers(survey, store, "sig-x", 3)
    check("两轮都装配成功", set(samplers) == {1, 2} and not issues, issues)
    base = {"1": "3", "2": "4"}
    rounds, _, _, _ = build_round_answers(survey, base, {}, False, 3, False, samplers)
    check("第 1 轮用第 1 轮配置", rounds[0]["1"] == "1", rounds[0])
    check("第 2 轮用第 2 轮配置", rounds[1]["1"] == "2", rounds[1])
    check("第 3 轮无配置回落到基准", rounds[2]["1"] == "3", rounds[2])
    check("轮次间互不影响", rounds[0]["1"] != rounds[1]["1"])

    store["rounds"]["2"]["1"]["weights"] = {"1": 0.5, "2": 0.5, "3": 0, "4": 0}
    samplers2, _ = build_round_samplers(survey, store, "sig-x", 2)
    check("改第 2 轮不污染第 1 轮",
          samplers2[1]["1"]._weights == [1.0, 0.0, 0.0, 0.0]
          and abs(sum(samplers2[2]["1"]._weights) - 1) < 1e-9)

    print("7) 落盘与读取")
    tmp = tempfile.mkdtemp()
    try:
        p = os.path.join(tmp, "choice_weights.json")
        save_store(p, store)
        back = load_store(p)
        check("轮次数一致", set(back["rounds"]) == {"1", "2"})
        check("配置内容一致",
              back["rounds"]["1"]["1"]["weights"]["1"] == 1)
        st = config_stats(back)
        check("统计正确", st["pairs"] == 2 and st["configured_rounds"] == [1, 2], st)
        # 写坏文件时回落空配置而不是抛异常
        open(p, "w", encoding="utf-8").write("{bad json")
        check("坏文件回落空配置", load_store(p)["rounds"] == {})
    finally:
        shutil.rmtree(tmp, ignore_errors=True)

    print(f"\n{ok}/{total} 通过")
    return 0 if ok == total else 1


if __name__ == "__main__":
    raise SystemExit(main())
