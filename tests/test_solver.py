"""求解器测试：基本性质 + 随机小例上与暴力枚举对比，验证“撤单最少”。"""
from __future__ import annotations

import random
from itertools import combinations, product

from app.solver import AssignmentView, plan_replacement


def brute_force_min_removals(works, caps, rec, active):
    """枚举每件作品最终的两名评委，返回可行方案中最少撤掉的待评单数。

    不可行（无法满足固定已交分 / 回避 / 上限 / 每作两评委）时返回 None。
    """
    judges = sorted(caps)
    pending = [a for a in active if not a.scored]
    fixed_by_work: dict[str, set[str]] = {}
    for a in active:
        if a.scored:
            fixed_by_work.setdefault(a.work_code, set()).add(a.judge_code)

    choices = []
    for w in works:
        fixed = fixed_by_work.get(w, set())
        if len(fixed) > 2 or any((j, w) in rec for j in fixed):
            return None
        need = 2 - len(fixed)
        pool = [j for j in judges
                if j not in fixed and (j, w) not in rec]
        if need < 0 or len(pool) < need:
            return None
        opts = []
        for extra in combinations(pool, need):
            opts.append(set(fixed) | set(extra))
        choices.append([(w, js) for js in opts])

    best = None
    for combo in product(*choices):
        counts = {j: 0 for j in judges}
        final_pairs = set()
        for w, js in combo:
            for j in js:
                counts[j] += 1
                final_pairs.add((w, j))
        if any(counts[j] > caps[j] for j in judges):
            continue
        removed = sum(
            1 for a in pending
            if (a.work_code, a.judge_code) not in final_pairs
        )
        if best is None or removed < best:
            best = removed
    return best


def assert_plan_valid(plan, works, caps, rec, active):
    assert plan.complete, plan.errors
    by_id = {a.id: a for a in active}
    remove_ids = {r["assignment_id"] for r in plan.remove}

    final_pairs = set()
    for a in active:
        if a.id in remove_ids:
            assert not a.scored, "不能撤掉已交分派单"
            continue
        final_pairs.add((a.work_code, a.judge_code))
    for ad in plan.add:
        pair = (ad["work_code"], ad["judge_code"])
        assert pair not in final_pairs, f"重复派单 {pair}"
        final_pairs.add(pair)

    counts: dict[str, int] = {}
    for w in works:
        js = [j for (ww, j) in final_pairs if ww == w]
        assert len(js) == 2, f"{w} -> {js}"
        assert len(set(js)) == 2
        for j in js:
            assert (j, w) not in rec
            counts[j] = counts.get(j, 0) + 1
    for j, n in counts.items():
        assert n <= caps[j], (j, n, caps[j])

    for a in active:
        if a.scored:
            assert (a.work_code, a.judge_code) in final_pairs


def _random_instance(rng, nw=None, nj=None):
    nw = nw or rng.randint(2, 3)
    nj = nj or rng.randint(3, 6)
    works = [f"W{i}" for i in range(nw)]
    judges = [f"J{i}" for i in range(nj)]
    caps = {j: rng.randint(1, nw) for j in judges}
    rec = {(j, w) for w in works for j in judges if rng.random() < 0.18}

    active = []
    aid = 1
    for w in works:
        pool = [j for j in judges if (j, w) not in rec]
        k = rng.randint(0, min(2, len(pool)))
        for j in rng.sample(pool, k):
            active.append(AssignmentView(aid, w, j, rng.random() < 0.5))
            aid += 1
    return works, caps, rec, active


def test_solver_matches_bruteforce_random():
    rng = random.Random(20261126)
    trials = 0
    for _ in range(400):
        works, caps, rec, active = _random_instance(rng)
        plan = plan_replacement(works, caps, rec, active)
        optimum = brute_force_min_removals(works, caps, rec, active)
        trials += 1

        if optimum is None:
            assert plan.complete is False
        else:
            assert plan.complete is True, plan.errors
            assert len(plan.remove) == optimum, (
                f"remove={len(plan.remove)} optimum={optimum}")
            assert_plan_valid(plan, works, caps, rec, active)

    # 稍大的作品数（4）、较少评委，再跑一批
    for _ in range(60):
        works, caps, rec, active = _random_instance(
            rng, nw=4, nj=rng.randint(3, 4))
        plan = plan_replacement(works, caps, rec, active)
        optimum = brute_force_min_removals(works, caps, rec, active)
        trials += 1
        if optimum is None:
            assert plan.complete is False
        else:
            assert plan.complete is True
            assert len(plan.remove) == optimum
            assert_plan_valid(plan, works, caps, rec, active)

    assert trials == 460


def test_full_state_needs_no_change():
    works = ["W1", "W2"]
    caps = {"J1": 2, "J2": 2, "J3": 2}
    rec = set()
    active = [
        AssignmentView(1, "W1", "J1", True),
        AssignmentView(2, "W1", "J2", False),
        AssignmentView(3, "W2", "J2", True),
        AssignmentView(4, "W2", "J3", False),
    ]
    plan = plan_replacement(works, caps, rec, active)
    assert plan.complete
    assert plan.remove == [] and plan.add == []


def test_sample_cascade_min_one_removal():
    """与 sample_data.json 相同结构的手工实例：最优解必须只撤 1 单。"""
    works = [f"W{i}" for i in range(1, 9)]
    judges = [f"J{i}" for i in range(1, 7)]
    caps = {j: 3 for j in judges}
    rec = {("J3", "W3"), ("J3", "W7"), ("J6", "W7"), ("J1", "W7")}
    active = [
        AssignmentView(1, "W1", "J1", True),
        AssignmentView(2, "W1", "J2", True),
        AssignmentView(3, "W2", "J2", True),
        AssignmentView(4, "W2", "J3", False),
        AssignmentView(5, "W3", "J4", True),
        AssignmentView(6, "W3", "J5", False),
        AssignmentView(7, "W4", "J1", False),
        AssignmentView(8, "W4", "J6", False),
        AssignmentView(9, "W5", "J3", True),
        AssignmentView(10, "W5", "J4", True),
        AssignmentView(11, "W6", "J5", False),
        AssignmentView(12, "W6", "J6", True),
        # #13 J1->W7 已因补报回避被作废，不在 active 快照中
        AssignmentView(14, "W7", "J5", False),
        AssignmentView(15, "W8", "J2", False),
        AssignmentView(16, "W8", "J4", False),
    ]
    plan = plan_replacement(works, caps, rec, active)
    assert plan.complete
    assert len(plan.remove) == 1
    assert plan.remove[0]["assignment_id"] in (15, 16)
    assert len(plan.add) == 2
    assert_plan_valid(plan, works, caps, rec, active)
