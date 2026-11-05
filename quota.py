# -*- coding: utf-8 -*-
"""按目标概率生成精确配额的序列。

逐轮独立抽样的问题是：400 轮设 70%，实际可能落在 68% 或 73%，
轮数越少偏差越大，而且没法预先知道会跑成什么样。这里换成另一条路：

    先算每个选项在整个批次里该出现多少次
      → 把这些次数均匀散布到 N 轮
      → 整体洗牌，顺序随机但总数精确

这样「400 轮、A 占 70%」就是 280 次 A，一次不差。

对外三个函数：

    plan_single(values, weights, rounds)              单选
    plan_multi(values, weights, rounds, lo, hi)       多选（带限项）
    plan_matrix(values, weights, rounds, rows)        矩阵（每行独立）

返回的都是长度 rounds 的列表，按轮索引取即可。
"""

import math
import random


def _normalize(weights, values):
    """取权重并归一化。全 0 时退化为均匀。"""
    raw = [max(0.0, float(weights.get(v, 0.0))) for v in values]
    total = sum(raw)
    if total <= 0 or not math.isfinite(total):
        return [1.0 / len(values)] * len(values)
    return [x / total for x in raw]


def allocate(probs, total):
    """最大余数法：把概率换算成整数次数，总和恰好等于 total。

    直接 round(p*N) 再补差会让小数部分的差异被忽略，
    最大余数法保证「总和不超发、余数给小数最大的那几个」。
    """
    if total <= 0:
        return [0] * len(probs)
    raw = [p * total for p in probs]
    base = [int(x) for x in raw]
    rest = total - sum(base)
    if rest > 0:
        order = sorted(range(len(raw)), key=lambda i: raw[i] - base[i], reverse=True)
        for i in order[:rest]:
            base[i] += 1
    elif rest < 0:
        # 浮点误差导致的超发，从最小的开始扣
        order = sorted(range(len(raw)), key=lambda i: raw[i] - base[i])
        for i in order[:abs(rest)]:
            if base[i] > 0:
                base[i] -= 1
    return base


def spread(counts, total):
    """把各选项的 counts 均匀散布成长度 total 的序列。

    每个选项的第 j 次出现放在 (j+0.5)/k 的分数位置，排序后即交错排列。
    这样 10 轮里 7 个 A 会落在 1/4/6/8/9 这类分散位置，不会前 7 个全是 A。
    """
    entries = []
    for idx, k in enumerate(counts):
        if k <= 0:
            continue
        for j in range(k):
            entries.append(((j + 0.5) / k, idx))
    entries.sort()
    seq = [idx for _, idx in entries]
    # sum(counts) 可能与 total 差一个（浮点），补齐或截断
    while len(seq) < total:
        seq.append(min(range(len(counts)), key=lambda i: counts[i]))
    return seq[:total]


def _split_counts(total, rounds, lo, hi):
    """把 total 个槽位分到 rounds 轮，每轮在 [lo, hi] 之间。

    先给每轮 lo，剩下的按轮均分，超出 hi 的余量再分给还没满的轮。
    """
    if rounds <= 0:
        return []
    lo = max(0, min(lo, hi))
    base = [lo] * rounds
    remain = total - lo * rounds
    if remain < 0:
        return base
    capacity = (hi - lo) * rounds
    remain = min(remain, capacity)
    i = 0
    while remain > 0:
        if base[i % rounds] < hi:
            base[i % rounds] += 1
            remain -= 1
        i += 1
        if i > rounds * (hi - lo + 2):
            break
    return base


def plan_single(values, weights, rounds, rng=None):
    """单选 / 下拉 / 量表：返回长度 rounds 的值列表。

    每个值的出现次数完全等于「概率 × 轮数」四舍五入后的结果。
    """
    if not values:
        return []
    rounds = max(1, int(rounds))
    probs = _normalize(weights, values)
    counts = allocate(probs, rounds)
    seq = spread(counts, rounds)
    out = [values[i] for i in seq]
    # 均匀散布保证边际比例，但位置有规律；洗一遍让顺序也随机
    if rng is not None:
        rng.shuffle(out)
    return out


def plan_multi(values, weights, rounds, lo=1, hi=None, rng=None):
    """多选：返回长度 rounds 的列表，每项是选中的值数组。

    weights 是每个值的目标出现概率（0-1）。各项概率之和等于平均选中项数。
    构造步骤：
      1. 各选项目标次数 k_j = p_j × rounds
      2. 总槽位 S = Σk_j，分成 rounds 轮，每轮 n_t 在 [lo, hi]
      3. 逐轮分配选项，按「剩余次数 / 剩余轮数」的紧迫度优先
    """
    if not values:
        return []
    rounds = max(1, int(rounds))
    hi = hi or len(values)
    hi = max(1, min(hi, len(values)))
    lo = max(1, min(lo, hi))

    probs = _normalize_multi(weights, values)
    # 用 +0.5 取整而不是 round()：Python 的 round 是银行家舍入，
    # 0.5 会向偶数靠，0.7×5=3.5 会被舍成 4 或 3 取决于奇偶，结果反直觉
    counts = [int(p * rounds + 0.5) for p in probs]

    # 单选项次数不能超过总轮数
    counts = [min(c, rounds) for c in counts]
    total_slots = sum(counts)

    # 槽位太少或太多时，按限项把每轮容量收进来
    total_slots = max(total_slots, lo * rounds)
    total_slots = min(total_slots, hi * rounds)

    # 如果因为裁剪导致 counts 总和与 total_slots 不一致，按比例补齐
    if sum(counts) != total_slots:
        counts = _rescale(counts, total_slots, rounds)

    per_round = _split_counts(total_slots, rounds, lo, hi)
    plan_idx = _assign(counts, per_round, rounds)

    out = []
    for row in plan_idx:
        vals = [values[j] for j in row]
        if rng is not None:
            rng.shuffle(vals)
        out.append(vals)
    return out


def _normalize_multi(weights, values):
    """多选的目标概率不归一化到 1，只裁剪到 [0,1] 并按限项缩放。"""
    raw = [max(0.0, float(weights.get(v, 0.0))) for v in values]
    return [min(x, 1.0) for x in raw]


def _rescale(counts, target, rounds):
    """把 counts 的总和调整到 target，保持相对比例。"""
    cur = sum(counts)
    if cur == 0:
        base = [0] * len(counts)
        for i in range(target):
            base[i % len(base)] += 1
        return base
    out = [int(round(c * target / cur)) for c in counts]
    out = [min(c, rounds) for c in out]
    diff = target - sum(out)
    i = 0
    while diff != 0 and i < 10000:
        k = i % len(out)
        if diff > 0 and out[k] < rounds:
            out[k] += 1
            diff -= 1
        elif diff < 0 and out[k] > 0:
            out[k] -= 1
            diff += 1
        i += 1
    return out


def _assign(counts, per_round, rounds):
    """把每选项的次数分配到每轮，保证每轮恰好 per_round[t] 项。"""
    n = len(counts)
    rem = list(counts)
    plan = []
    for t in range(rounds):
        need = per_round[t]
        left_rounds = rounds - t
        # 剩余轮数里每轮至少要选几次才能做完，用来判断谁更紧迫
        urgent = [math.ceil(rem[j] / left_rounds) if rem[j] > 0 else 0
                  for j in range(n)]
        order = sorted(range(n), key=lambda j: (-urgent[j], -rem[j] / left_rounds))

        row = []
        for j in order:
            if len(row) >= need:
                break
            if rem[j] > 0 and urgent[j] > 0:
                row.append(j)
                rem[j] -= 1
        # 还有空位就按剩余次数补
        if len(row) < need:
            for j in sorted(range(n), key=lambda x: -rem[x]):
                if len(row) >= need:
                    break
                if rem[j] > 0 and j not in row:
                    row.append(j)
                    rem[j] -= 1
        # 仍然不够（说明配额本身不可行），按顺序塞满，保证每轮项数不变
        if len(row) < need:
            for j in range(n):
                if len(row) >= need:
                    break
                if j not in row:
                    row.append(j)
        plan.append(row)

    _fix_tail(plan, rem, counts)
    return plan


def _fix_tail(plan, rem, counts):
    """尾部修正：把还欠的配额和超发的配额互换。

    贪心到最后几轮可能出现「某选项还欠 3 次，但每轮都满了」。
    找一轮里已经选了、且那项本身超发的，换掉即可。
    """
    surplus = [-r for r in rem]          # 负数表示欠，正数表示超发
    for j, r in enumerate(rem):
        surplus[j] = -r
    if all(s == 0 for s in surplus):
        return
    for j, s in enumerate(surplus):
        if s <= 0:
            continue
        # 该选项超发了 s 次，找含它的轮次，把位置让给欠的选项
        for t in range(len(plan) - 1, -1, -1):
            if s <= 0:
                break
            if j not in plan[t]:
                continue
            for k, d in enumerate(surplus):
                if d >= 0:
                    continue
                if k in plan[t]:
                    continue
                plan[t][plan[t].index(j)] = k
                surplus[j] -= 1
                surplus[k] += 1
                s -= 1
                break


def plan_matrix(values, weights, rounds, rows, rng=None):
    """矩阵量表：每轮 rows 个分值，每行独立配额。"""
    if not values:
        return []
    rounds = max(1, int(rounds))
    rows = max(1, int(rows))
    per_cell = plan_single(values, weights, rounds * rows, rng)
    out = []
    for t in range(rounds):
        out.append(per_cell[t * rows:(t + 1) * rows])
    return out


def values_for(q):
    """这道题可被抽到的取值列表。

    矩阵量表在页面里没有 options：题目的「选项」是表头刻度（同意/一般…），
    真正提交的是每行的分值 1..N。这里统一按分值编码，和前端
    （web/app.js 里 matrix scale.map 出 value=i+1）以及 choice_weights
    的 question_values 保持同一套编号，否则配额会算在空集合上。
    """
    if q.get("type_name") == "矩阵量表":
        scale = (q.get("matrix") or {}).get("scale") or []
        return [str(i + 1) for i in range(len(scale))]
    return [str(o["value"]) for o in (q.get("options") or [])]


class QuotaPlan:
    """把一份配额序列包装成按轮取值的对象。

    和原来的 QuestionSampler 接口对齐（有 draw），但内部不做随机抽样，
    直接返回预先算好的那一轮。这样每一轮的结果在开跑前就确定了，
    日志、界面、实际提交三者一致。
    """

    def __init__(self, q, cfg, rounds, rng=None):
        self.q = q
        self.cfg = cfg
        self.rounds = max(1, int(rounds))
        self.values = values_for(q)
        self.mode = str(cfg.get("mode") or "single")
        self._seq = []
        self._build(rng)
        self._idx = 0

    def _build(self, rng):
        q = self.q
        vals = self.values
        w = self.cfg.get("weights") or {}

        if not vals:
            return

        if self.mode == "matrix" or q.get("type_name") == "矩阵量表":
            rows = len((q.get("matrix") or {}).get("rows") or [])
            if rows <= 0:
                # 解析不到子项行数时不能瞎编：max(1, rows) 会给出每轮 1 个分值，
                # 和页面 16 行对不上，填写器直接中止。这里留空，
                # at() 返回 None，调用方保留原答案（随机填充的那 16 行）。
                return
            self._seq = plan_matrix(vals, w, self.rounds, rows, rng)
            return

        if self.mode in ("multi_target", "multi_weight") or q.get("type_name") == "多选":
            lo, hi = self._bounds(q, self.cfg.get("count"))
            if self.mode == "multi_weight":
                # 相对权重：先按权重的占比换算成目标概率
                probs = _normalize(w, vals)
                mean = self._mean_count(lo, hi)
                w = {v: probs[i] * mean for i, v in enumerate(vals)}
            self._seq = plan_multi(vals, w, self.rounds, lo, hi, rng)
            return

        self._seq = plan_single(vals, w, self.rounds, rng)

    @staticmethod
    def _bounds(q, count=None):
        n = len(q.get("options") or [])
        lo = 1
        hi = n
        if q.get("exact_select"):
            lo = hi = min(int(q["exact_select"]), n)
        else:
            if q.get("min_select"):
                lo = max(1, min(int(q["min_select"]), n))
            if q.get("max_select"):
                hi = max(lo, min(int(q["max_select"]), n))
        if count:
            lo = hi = max(1, min(int(count), n))
        return lo, hi

    @staticmethod
    def _mean_count(lo, hi):
        """相对权重模式下，先估一个平均选中项数。"""
        return (lo + hi) / 2.0

    def at(self, round_no):
        """取第 round_no 轮（1 起）。超出范围时按余数循环。

        没算出配额时返回 None 而不是 []：空列表会被当成「这题的答案就是空」，
        矩阵题拿到 0 行会直接中止整批；None 表示「这题没有配额」，
        调用方保留基础答案。
        """
        if not self._seq:
            return None
        idx = (max(1, int(round_no)) - 1) % len(self._seq)
        return self._seq[idx]

    def draw(self, round_no=None):
        """兼容旧接口：不传轮次就顺序取。"""
        if round_no is not None:
            return self.at(round_no)
        if not self._seq:
            return None
        val = self._seq[self._idx % len(self._seq)]
        self._idx += 1
        return val

    def counts(self):
        """每个值在整个批次里实际出现多少次，给界面展示。"""
        out = {v: 0 for v in self.values}
        for item in self._seq:
            if isinstance(item, list):
                for v in item:
                    out[v] = out.get(v, 0) + 1
            else:
                out[item] = out.get(item, 0) + 1
        return out

    def items(self):
        """整个批次的序列，按轮排列。"""
        return list(self._seq)
