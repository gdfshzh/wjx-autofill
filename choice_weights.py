#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""choice_weights.py —— 选择题概率配置（全局一份，按总轮数精确配额）

v2 起配置不再按轮次分开：一道题只有一份配置，对所有轮次生效。
保存时只记目标概率，开跑时按「次数」换算出每轮取什么值。

数据结构（choice_weights.json）
{
  "version": 2,
  "sig": "问卷指纹 survey_sig()",
  "updated": "2026-11-03 13:10:00",
  "items": {
    "8": {
      "v": 2,
      "sig": "题目选项指纹",
      "mode": "multi_target",
      "weights": {"1": 0.7, "2": 0.6, "3": 0.3},
      "count": null
    }
  }
}

精确配额（quota.py）
  目标概率 × 总轮数 = 每个选项该出现多少次，四舍五入后按最大余数法补齐，
  再均匀散布到 N 轮、整体洗牌。400 轮设 70% 就是 280 次，不是「大约 280」。

四种语义
  single       单选/下拉：weights 归一化后即各选项出现概率
  multi_target 多选：weights 是每个选项的目标出现概率（0-1），
               总和等于平均选中项数
  multi_weight 多选：weights 是相对权重，按平均项数换算成目标概率
  matrix       矩阵量表：weights 是分值 1..N 的分布，每行独立配额

复杂度
  配额在开跑前一次算完，O(N · 选项数)，运行时取值为 O(1)。
  多选分配是贪心 + 尾部修正，O(N · m log m)。
"""

import hashlib
import json
import math
import os
import random
import time
from collections import Counter

from quota import QuotaPlan

VERSION = 2
CHOICE_TYPES = ("单选", "多选", "矩阵量表", "下拉")
MULTI_ENUM_MAX = 12       # 组合枚举上限，超过改用 multi_weight，避免 2^20 卡死
FIT_ITERS = 120
FIT_TOL = 1e-4
FIT_CACHE_MAX = 256

_FIT_CACHE = {}


# ---------------------------------------------------------------- 基础工具
def question_sig(q):
    """题目指纹：题型 + 选项值/文案（矩阵用行名 + 分值文案）。

    只比题号不够：换问卷后 Q5 仍叫 Q5，选项却可能整组换掉。
    配置里存一份，运行时对不上就丢弃该题配置，避免把旧权重套到新选项上。
    """
    h = hashlib.md5()
    h.update((q.get("type_name") or "").encode("utf-8"))
    if q.get("type_name") == "矩阵量表":
        mx = q.get("matrix") or {}
        h.update(("|".join(mx.get("rows") or [])).encode("utf-8"))
        h.update(("|".join(mx.get("scale") or [])).encode("utf-8"))
    else:
        for o in q.get("options") or []:
            h.update(f"{o.get('value')}:{o.get('text')}".encode("utf-8"))
    return h.hexdigest()[:10]


def question_values(q):
    """返回 [(value, text), ...]，value 一律转成字符串（与答案 JSON 对齐）。

    矩阵的取值是分值 1..N（不是表头文案），必须和 quota.values_for 完全一致，
    否则配额会算在一组页面上根本不存在的 key 上。
    """
    if q.get("type_name") == "矩阵量表":
        scale = (q.get("matrix") or {}).get("scale") or []
        return [(str(i + 1), str(s)) for i, s in enumerate(scale)]
    return [(str(o.get("value")), str(o.get("text"))) for o in (q.get("options") or [])]


def default_mode(q):
    t = q.get("type_name")
    if t == "多选":
        return "multi_target"
    if t == "矩阵量表":
        return "matrix"
    return "single"


def multi_bounds(q, count=None):
    """多选的可选/必选范围 (lo, hi)。count 优先于题干。"""
    m = len(q.get("options") or [])
    if m <= 0:
        return 0, 0
    if count not in (None, "", 0):
        try:
            n = int(count)
        except (TypeError, ValueError):
            n = 0
        if n > 0:
            n = min(n, m)
            return n, n
    if q.get("exact_select"):
        n = max(1, min(int(q["exact_select"]), m))
        return n, n
    lo = max(1, int(q.get("min_select") or 1))
    hi = min(int(q.get("max_select") or m), m)
    if lo > hi:                     # 题干自相矛盾时以下限为准
        lo = hi
    return lo, hi


def _to_num(v):
    """把界面传上来的字符串/数字收敛成有限浮点，非法直接抛 ValueError。"""
    if isinstance(v, bool):
        raise ValueError("布尔值不是合法权重")
    if isinstance(v, str):
        s = v.strip().replace("%", "")
        if not s:
            raise ValueError("空值")
        v = float(s)
    if not isinstance(v, (int, float)):
        raise ValueError("不是数字")
    v = float(v)
    if not math.isfinite(v):
        raise ValueError("不是有限数")
    return v


def _norm(weights):
    total = sum(weights.values())
    if total <= 0:
        return {}
    return {k: v / total for k, v in weights.items()}


# ---------------------------------------------------------------- 校验
def validate_config(q, cfg, normalize=True):
    """校验并规范化单题配置。

    返回 (clean, errors, warnings)。
      errors   非空表示不可保存/不可使用
      warnings 是自动修正的说明（归一化、目标值缩放、退化等）

    normalize=False 用于读取已存配置时做二次校验，仍然会归一化（幂等）。
    """
    errs, warns = [], []
    if not isinstance(cfg, dict):
        return {}, ["配置必须是对象"], warns
    if q.get("type_name") not in CHOICE_TYPES:
        return {}, [f"题型「{q.get('type_name')}」不支持概率配置"], warns

    mode = str(cfg.get("mode") or default_mode(q))
    if mode not in ("single", "multi_target", "multi_weight", "matrix"):
        return {}, [f"未知模式 {mode}"], warns
    t = q.get("type_name")
    if t == "矩阵量表" and mode != "matrix":
        return {}, ["矩阵量表只支持 matrix 模式"], warns
    if t != "矩阵量表" and mode == "matrix":
        return {}, ["非矩阵题不能使用 matrix 模式"], warns
    if t in ("单选", "下拉") and mode != "single":
        return {}, [f"{t}只支持 single 模式"], warns
    if t == "多选" and mode not in ("multi_target", "multi_weight"):
        return {}, ["多选只支持 multi_target / multi_weight 模式"], warns

    vals = question_values(q)
    if not vals:
        return {}, ["该题没有可配置的选项"], warns
    valid = {v for v, _ in vals}

    raw = cfg.get("weights")
    if not isinstance(raw, dict) or not raw:
        return {}, ["weights 必须是非空对象"], warns

    weights = {}
    for k, v in raw.items():
        key = str(k)
        if key not in valid:
            errs.append(f"选项 {key} 不在本题选项表内")
            continue
        try:
            num = _to_num(v)
        except (TypeError, ValueError) as e:
            errs.append(f"选项 {key} 的权重非法：{e}")
            continue
        if num < 0:
            errs.append(f"选项 {key} 的权重不能为负（{num:g}）")
            continue
        weights[key] = num
    if errs:
        return {}, errs, warns

    # 缺省选项按 0 处理，保持完整键集合，前端回显时不用猜
    for v, _ in vals:
        weights.setdefault(v, 0.0)

    total = sum(weights.values())
    if not math.isfinite(total) or total <= 0:
        return {}, ["权重之和必须是大于 0 的有限数"], warns

    clean = {
        "v": VERSION,
        "sig": question_sig(q),
        "mode": mode,
    }

    if mode in ("single", "matrix"):
        clean["weights"] = _norm(weights)
        return clean, errs, warns

    # ---------------- 多选
    try:
        count = int(cfg.get("count")) if cfg.get("count") not in (None, "", 0) else None
    except (TypeError, ValueError):
        errs.append("count 必须是整数或留空")
        return {}, errs, warns
    if count is not None and count < 1:
        errs.append("count 至少为 1")
        return {}, errs, warns
    if count is not None and count > len(vals):
        warns.append(f"固定选 {count} 项超过选项数，已收敛到 {len(vals)} 项")
        count = len(vals)

    lo, hi = multi_bounds(q, count)
    if q.get("exact_select") and count and count != q["exact_select"]:
        # 题干「限选 N 项」是硬约束，改了提交也会被服务端打回；
        # 这里必须报错而不是像区间题那样给个警告就放行
        errs.append(f"题干限选 {q['exact_select']} 项，不能改成 {count} 项")
        return {}, errs, warns

    if mode == "multi_weight":
        clean["weights"] = _norm(weights)
        clean["count"] = count
        pos = sum(1 for v in weights.values() if v > 0)
        if lo == hi and pos < lo:
            errs.append(f"固定选 {lo} 项，但只有 {pos} 个选项的权重大于 0")
            return {}, errs, warns
        return clean, errs, warns

    # 选项太多时组合枚举代价指数上升，直接退化成相对权重抽样。
    # 这一步放在缩放目标值之前，权重仍然是用户填的原值。
    if len(vals) > MULTI_ENUM_MAX:
        warns.append(f"选项数 {len(vals)} 超过 {MULTI_ENUM_MAX}，"
                     f"无法精确拟合边缘分布，已改为按相对权重不放回抽样")
        clean["mode"] = "multi_weight"
        clean["weights"] = _norm(weights)
        clean["count"] = count
        return clean, errs, warns

    # multi_target：weights 是目标出现概率
    if max(weights.values()) > 1.0000001:
        # 全部大于 1 才认为是百分数输入（70/20/10 这种）。
        # 只要有小于 1 的混在里面，说明是 0-1 写法里有一项填错了，
        # 此时把 1.5 当成 1.5% 会静默吞掉错误，必须报出来。
        if min(weights.values()) > 1.0000001 and max(weights.values()) <= 100.0000001:
            warns.append("检测到百分比输入，已按 0-100% 换算")
            weights = {k: v / 100.0 for k, v in weights.items()}
        else:
            errs.append("目标出现概率必须在 0-100% 之间（用小数写法时不超过 1）")
            return {}, errs, warns

    p = dict(weights)
    s = sum(p.values())
    target_sum = None
    if lo == hi:
        target_sum = float(lo)
        if abs(s - target_sum) > 1e-6:
            scale = target_sum / s
            p = {k: v * scale for k, v in p.items()}
            warns.append(f"固定选 {lo} 项，目标和 {s:.2f} 已自动缩放到 {target_sum:.0f}")
    else:
        if s < lo:
            scale = lo / s
            p = {k: v * scale for k, v in p.items()}
            warns.append(f"目标和 {s:.2f} 低于下限 {lo}，已缩放到 {lo}")
        elif s > hi:
            scale = hi / s
            p = {k: v * scale for k, v in p.items()}
            warns.append(f"目标和 {s:.2f} 高于上限 {hi}，已缩放到 {hi}")

    over = [(k, v) for k, v in p.items() if v > 1.0000001]
    if over:
        k, v = max(over, key=lambda x: x[1])
        errs.append(
            f"目标分布不可行：Q{q.get('topic')} 选项 {k} 的概率被推到 {v:.0%}，"
            f"超过 100%。固定选 {lo} 项时，各项目标概率之和必须等于 {lo}，"
            f"且单项不超过 100%")
        return {}, errs, warns

    pos = sum(1 for v in p.values() if v > 0)
    if pos < lo:
        errs.append(f"至少选 {lo} 项，但只有 {pos} 个选项的目标概率大于 0")
        return {}, errs, warns

    # 预先拟合一次，把「目标不可精确达到」提前暴露；拟合结果会被缓存
    fit = _fit_for(q, lo, hi, p)
    if fit is None:
        errs.append("在限项约束下找不到可行的选项组合，请调低部分选项的概率")
        return {}, errs, warns
    _, _, fit_err = fit
    if fit_err > 0.02:
        warns.append(f"目标分布无法精确达到，已取最接近的可行分布（最大偏差 {fit_err:.1%}）")

    clean["weights"] = {k: round(v, 10) for k, v in p.items()}
    clean["count"] = count
    clean["fit_error"] = round(fit_err, 6)
    return clean, errs, warns


# ---------------------------------------------------------------- 多选组合拟合
def _feasible_masks(m, lo, hi):
    out = []
    for mask in range(1, 1 << m):
        c = bin(mask).count("1")
        if lo <= c <= hi:
            out.append(mask)
    return out


def _fit_marginals(m, lo, hi, targets):
    """IPF：在可行组合集合上求一个分布，使各选项边缘概率贴近 targets。

    组合数最多 4095（m≤12），每轮迭代只做「按选项缩放 + 归一化」，
    收敛判据是最大边缘误差 < FIT_TOL。不可行时返回最后一次结果与误差。
    """
    masks = _feasible_masks(m, lo, hi)
    if not masks:
        return None
    n = len(masks)
    p = [1.0 / n] * n
    idx = [[k for k, mask in enumerate(masks) if mask & (1 << i)] for i in range(m)]

    err = 1.0
    for _ in range(FIT_ITERS):
        for i, q in enumerate(targets):
            cells = idx[i]
            if q <= 1e-12:
                for k in cells:
                    p[k] = 0.0
                continue
            cur = sum(p[k] for k in cells)
            if cur <= 1e-15:
                return None
            f = q / cur
            for k in cells:
                p[k] *= f
        s = sum(p)
        if s <= 0:
            return None
        inv = 1.0 / s
        p = [x * inv for x in p]
        err = 0.0
        for i, q in enumerate(targets):
            cur = sum(p[k] for k in idx[i])
            err = max(err, abs(cur - q))
        if err < FIT_TOL:
            break
    return masks, p, err


def _fit_for(q, lo, hi, probs):
    """按 (题号, 选项数, 范围, 目标值) 缓存拟合结果。"""
    vals = question_values(q)
    m = len(vals)
    if m > MULTI_ENUM_MAX:
        return None
    key = (str(q.get("topic")), m, lo, hi,
           tuple(round(float(probs.get(v, 0.0)), 4) for v, _ in vals))
    hit = _FIT_CACHE.get(key)
    if hit is not None:
        return hit
    targets = [float(probs.get(v, 0.0)) for v, _ in vals]
    # 目标概率之和就是期望选中项数。和正好落在区间端点时，项数被唯一确定：
    # 例如上限 2 项、目标和 2.0，就不可能抽出 1 项 —— 不先收紧范围，
    # IPF 会在边界附近收敛很慢，还会把 2.5% 的轮次抽成 1 项。
    s = sum(targets)
    if s <= lo + 1e-9:
        lo = hi = lo
    elif s >= hi - 1e-9:
        lo = hi = hi
    fit = _fit_marginals(m, lo, hi, targets)
    if len(_FIT_CACHE) >= FIT_CACHE_MAX:
        _FIT_CACHE.clear()        # 配置数量有限，整表清空比 LRU 简单且够用
    _FIT_CACHE[key] = fit
    return fit


# ---------------------------------------------------------------- 抽样器
class QuestionSampler:
    """把一份已校验配置编译成可反复调用的抽样器。

    构造时做完所有预处理（组合枚举 / IPF / 权重表），draw() 只做一次随机。
    """

    def __init__(self, q, cfg):
        self.q = q
        self.cfg = cfg
        self.mode = cfg["mode"]
        self.topic = str(q.get("topic"))
        self.values = [v for v, _ in question_values(q)]
        self.texts = {v: t for v, t in question_values(q)}
        self._table = None
        self._weights = None
        self._n = None

        w = cfg.get("weights") or {}
        if self.mode in ("single", "matrix"):
            self._weights = [float(w.get(v, 0.0)) for v in self.values]
            if sum(self._weights) <= 0:
                self._weights = [1.0] * len(self.values)
        elif self.mode == "multi_weight":
            self._weights = [float(w.get(v, 0.0)) for v in self.values]
            if sum(self._weights) <= 0:
                self._weights = [1.0] * len(self.values)
            lo, hi = multi_bounds(q, cfg.get("count"))
            # 0 权重选项只应在「项数不够」时被迫补位。先把上限压到正权重个数，
            # 常规情况下 0 权重就永远不会出现。
            pos = sum(1 for x in self._weights if x > 0)
            hi = min(hi, max(1, pos))
            lo = min(lo, hi)
            self._n = (lo, hi)
        else:
            lo, hi = multi_bounds(q, cfg.get("count"))
            probs = {v: float(w.get(v, 0.0)) for v in self.values}
            fit = _fit_for(q, lo, hi, probs)
            if fit is None:
                # 超长选项表：退化成相对权重 + 不放回抽样，仍然遵守限项
                self.mode = "multi_weight"
                self._weights = [max(0.0, probs.get(v, 0.0)) for v in self.values]
                pos = sum(1 for x in self._weights if x > 0)
                hi = min(hi, max(1, pos))
                lo = min(lo, hi)
                self._n = (lo, hi)
            else:
                masks, ps, _ = fit
                table = []
                for mask, prob in zip(masks, ps):
                    if prob <= 0:
                        continue
                    picks = [self.values[i] for i in range(len(self.values))
                             if mask & (1 << i)]
                    table.append((picks, prob))
                total = sum(x[1] for x in table)
                self._table = [(picks, prob / total) for picks, prob in table]

    def draw(self, rng=None):
        rng = rng or random
        if self.mode == "single":
            return rng.choices(self.values, weights=self._weights, k=1)[0]

        if self.mode == "matrix":
            rows = len((self.q.get("matrix") or {}).get("rows") or [])
            return [int(rng.choices(self.values, weights=self._weights, k=1)[0])
                    for _ in range(rows)]

        # 多选
        if self.mode == "multi_target" and self._table:
            picks, _ = rng.choices(self._table, weights=[p for _, p in self._table], k=1)[0]
            return list(picks)

        lo, hi = self._n if isinstance(self._n, tuple) else (self._n, self._n)
        n = _pick_count(lo, hi, rng)
        return _weighted_sample(self.values, self._weights, n, rng)


def _pick_count(lo, hi, rng):
    """多选项数：固定值直接返回；区间内用三角分布，峰值在 1/3 处。

    与 wjx_autofill.random_answers 的项数策略保持一致，避免同一份问卷
    在「随机填充」和「按概率抽」两条路径上给出完全不同的项数分布。
    """
    lo = max(1, int(lo))
    hi = max(lo, int(hi))
    if hi - lo >= 2:
        n = int(round(rng.triangular(lo, hi, lo + (hi - lo) / 3)))
    else:
        n = rng.randint(lo, hi)
    return max(lo, min(hi, n))


def _weighted_sample(values, weights, n, rng):
    """按权重不放回抽样（Efraimidis-Spirakis）：key = u^(1/w)，取最大 n 个。

    w=0 的选项永远排最后；全 0 时退化成均匀，调用方已在校验阶段拦住全 0。
    """
    n = max(0, min(int(n), len(values)))
    keyed = []
    for v, w in zip(values, weights):
        if w <= 0:
            keyed.append((-1.0, v))
        else:
            keyed.append((rng.random() ** (1.0 / w), v))
    keyed.sort(key=lambda x: x[0], reverse=True)
    return [v for _, v in keyed[:n]]


def sample_question(q, cfg, rng=None):
    """一次性抽样，主要给预览与自检用；批量请复用 QuestionSampler。"""
    return QuestionSampler(q, cfg).draw(rng)


# ---------------------------------------------------------------- 轮次装配
def build_plans(survey, store, sig, rounds):
    """把配置编译成 {topic: QuotaPlan}，一份配置覆盖全部轮次。

    配额在开跑前一次算完，之后每轮直接取值，不再抽样 ——
    所以界面显示的「预计次数」和实际提交的次数必然一致。

    任何一层对不上（问卷指纹、题目指纹、选项值、题型）都跳过并记一条 issue，
    不抛异常：一份过期的配置不应该让整轮任务起不来。
    """
    out, issues = {}, []
    items = (store or {}).get("items") or {}
    if not items:
        return out, []
    if store.get("sig") and store.get("sig") != sig:
        issues.append("概率配置属于另一份问卷（指纹不一致），本次全部不套用")
        return out, issues

    rounds = max(1, int(rounds))
    qmap = {str(q.get("topic")): q for q in survey.get("questions") or []}
    for topic, cfg in items.items():
        topic = str(topic)
        q = qmap.get(topic)
        if not q:
            issues.append(f"Q{topic} 已不在问卷里，跳过")
            continue
        if not isinstance(cfg, dict):
            issues.append(f"Q{topic} 配置不是对象，跳过")
            continue
        if cfg.get("sig") and cfg["sig"] != question_sig(q):
            issues.append(f"Q{topic} 的选项已变，跳过该题配置")
            continue
        try:
            clean, errs, warns = validate_config(q, cfg, normalize=True)
            if errs:
                issues.append(f"Q{topic} 配置无效：{errs[0]}")
                continue
            for w in warns:
                issues.append(f"Q{topic}：{w}")
            out[topic] = QuotaPlan(q, clean, rounds)
        except Exception as e:
            issues.append(f"Q{topic} 配置读取失败：{type(e).__name__}: {e}")
    return out, issues


def config_stats(store):
    """给接口/前端用的统计：配置了几道题。"""
    items = (store or {}).get("items") or {}
    topics = [str(t) for t in items if isinstance(items.get(t), dict)]
    return {
        "pairs": len(topics),
        "questions": sorted(topics, key=lambda x: int(x) if x.isdigit() else 0),
        # 字段保留，v2 不再有轮次维度，前端读到空数组即可
        "configured_rounds": [],
    }


# ---------------------------------------------------------------- 预览
def preview_question(q, cfg, rounds=100, seed=None):
    """按 rounds 轮生成精确配额，返回每选项的次数与占比。

    「实际」不是抽样估计，是按配额算出来的确定值 ——
    400 轮 70% 直接显示 280 次，不会因为随机波动显示成 276。
    """
    rounds = max(1, min(int(rounds or 100), 20000))
    # 先归一化：前端传的是草稿，单选/矩阵的 weights 可能是 70/20/10 这种
    # 相对权重，不归一化的话 target 会和 actual（0-1）差两个数量级
    clean, _errs, _warns = validate_config(q, cfg, normalize=True)
    if clean:
        cfg = clean
    rng = random.Random(seed) if seed is not None else None
    plan = QuotaPlan(q, cfg, rounds, rng)
    vals = [v for v, _ in question_values(q)]
    counts = plan.counts()

    hist = {}
    for item in plan.items():
        n = len(item) if isinstance(item, list) else 1
        hist[n] = hist.get(n, 0) + 1

    is_multi = (q.get("type_name") == "多选"
                or str(cfg.get("mode") or "").startswith("multi"))
    rows = len((q.get("matrix") or {}).get("rows") or [])
    if is_multi:
        denom = rounds
    elif rows:
        denom = rounds * rows
    else:
        denom = rounds

    actual = {v: counts.get(v, 0) / max(1, denom) for v in vals}

    target = None
    if cfg.get("mode") in ("single", "matrix", "multi_target"):
        target = {v: float((cfg.get("weights") or {}).get(v, 0.0)) for v in vals}

    err = None
    if target:
        err = max(abs(actual.get(v, 0.0) - target.get(v, 0.0)) for v in vals)

    cells = sum(counts.values())
    return {
        "mode": cfg.get("mode"),
        "rounds": rounds,
        "samples": rounds,               # 兼容旧字段名
        "values": vals,
        "texts": {v: t for v, t in question_values(q)},
        "counts": {v: counts.get(v, 0) for v in vals},
        "actual": actual,
        "target": target,
        "mean_count": round(cells / rounds, 3),
        "count_hist": {str(k): v for k, v in sorted(hist.items())},
        "fit_error": round(err, 6) if err is not None else None,
        "exact": True,
    }


# ---------------------------------------------------------------- 文件读写
def empty_store():
    return {"version": VERSION, "sig": "", "updated": "", "items": {}}


def load_store(path):
    """读配置。文件不存在/损坏都返回空结构，不阻断服务启动。

    v1 的 rounds 结构会自动迁移：同一题在多个轮次配了不同值时，
    取出现次数最多的那一份（平票取靠前的轮次）。
    """
    if not os.path.exists(path):
        return empty_store()
    try:
        data = json.load(open(path, encoding="utf-8"))
        if not isinstance(data, dict):
            raise ValueError("顶层不是对象")
        data.setdefault("sig", "")
        data.setdefault("updated", "")

        items = data.get("items")
        if not isinstance(items, dict):
            items = {}
        items = {str(k): v for k, v in items.items() if isinstance(v, dict)}

        legacy = data.get("rounds")
        if isinstance(legacy, dict) and legacy:
            merged = {}
            for r in sorted(legacy, key=lambda x: int(x) if str(x).isdigit() else 0):
                conf = legacy[r]
                if not isinstance(conf, dict):
                    continue
                for t, cfg in conf.items():
                    if isinstance(cfg, dict):
                        merged.setdefault(str(t), []).append(cfg)
            for t, lst in merged.items():
                if t in items:
                    continue
                keys = [json.dumps([c.get("mode"), c.get("weights"), c.get("count")],
                                   sort_keys=True, ensure_ascii=False) for c in lst]
                most = Counter(keys).most_common(1)[0][0]
                items[t] = lst[keys.index(most)]
            data["migrated_from"] = f"v1（{len(legacy)} 轮）"

        data["items"] = items
        data["version"] = VERSION
        data.pop("rounds", None)
        return data
    except Exception:
        return empty_store()


def save_store(path, store):
    """原子写：先写临时文件再 os.replace，写一半断电不会留下坏 JSON。"""
    store["version"] = VERSION
    store["updated"] = time.strftime("%Y-%m-%d %H:%M:%S")
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(store, f, ensure_ascii=False, indent=2)
    os.replace(tmp, path)
