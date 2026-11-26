"""补位规划器：在回避关系与接单上限内，求“撤掉的旧待评派单最少”的补齐方案。

建模（最小费用最大流）
--------------------
每件作品最终需要 2 名不同评委。快照中的有效派单分为：

* 已交分派单（scored）：必须保留，直接占住该作品的一个席位和该评委的容量，
  在网络中作为“预先固定”处理（扣减作品剩余需求与评委剩余容量）。
* 待评派单（pending）：可以保留（费用 0），也可以撤掉；撤掉 1 张即目标函数
  中的代价 1。

固定已交分派单后，设剩余席位数为 D（= Σ(2 - 该作品已交分数)），剩余候选边：

* 已存在的待评派单 -> 评委→作品 边，费用 0；
* 新评委组合       -> 评委→作品 边，费用 1；
* 源点→评委 容量 = capacity - 该评委已交分单数（待评保留/新派都要占容量）；
* 评委→作品 容量 1（保证同一作品两名评委不同）；
* 作品→汇点 容量 = 该作品剩余席位。

在完整补齐（流量 = D）的前提下，费用 = 新派单数 N。设当前待评派单总数为 P、
保留 K、撤掉 R = P - K，而 D = K + N（每个剩余席位要么由保留待评单占，要么
由新派单占），所以 R = P - D + N。P、D 在一次快照中是常数，因此最小化 N
等价于最小化 R —— 恰好满足“撤掉的旧待评派单数最少”。

若最大流 < D，则无法全部补齐；返回最大流方案下仍未满的作品作为
“缺评委的作品”，调用方不得执行任何改派。

规模：作品 ≤ 24 → D ≤ 48，节点/边都很小，使用 SPFA 增广足够快，且不依赖
任何第三方库。
"""
from __future__ import annotations

from collections import deque
from dataclasses import dataclass
from typing import Iterable

SEATS_PER_WORK = 2
# 新派单的费用。待评旧单费用为 0，因此最小费用优先保留旧待评单。
COST_NEW = 1


@dataclass(frozen=True)
class AssignmentView:
    id: int
    work_code: str
    judge_code: str
    scored: bool


@dataclass
class Plan:
    complete: bool
    target_seats: int
    filled_seats: int
    remove: list[dict]
    add: list[dict]
    kept_pending: list[int]
    fixed_scored: list[int]
    short_works: list[dict]
    eligible_counts: dict[str, int]
    errors: list[str]

    def to_dict(self) -> dict:
        return {
            "complete": self.complete,
            "target_seats": self.target_seats,
            "filled_seats": self.filled_seats,
            "remove": self.remove,
            "add": self.add,
            "kept_pending": self.kept_pending,
            "fixed_scored": self.fixed_scored,
            "short_works": self.short_works,
            "eligible_counts": self.eligible_counts,
            "errors": self.errors,
        }


def _add_edge(graph, u, v, cap, cost, meta=None):
    fwd = {"to": v, "rev": len(graph[v]), "cap": cap, "orig_cap": cap,
           "cost": cost, "meta": meta}
    rev = {"to": u, "rev": len(graph[u]), "cap": 0, "orig_cap": 0,
           "cost": -cost, "meta": None}
    graph[u].append(fwd)
    graph[v].append(rev)
    return fwd


def _min_cost_max_flow(graph, source, sink, need):
    """返回 (flow, cost)。图很小，用 SPFA 找最短增广路（兼容负残量边）。"""
    n = len(graph)
    inf = 10**15
    flow = 0
    cost = 0

    while flow < need:
        dist = [inf] * n
        prev_v = [-1] * n
        prev_e = [-1] * n
        in_queue = [False] * n
        dist[source] = 0
        q = deque([source])

        while q:
            u = q.popleft()
            in_queue[u] = False
            du = dist[u]
            for ei, e in enumerate(graph[u]):
                if e["cap"] <= 0:
                    continue
                nd = du + e["cost"]
                if nd < dist[e["to"]]:
                    dist[e["to"]] = nd
                    prev_v[e["to"]] = u
                    prev_e[e["to"]] = ei
                    if not in_queue[e["to"]]:
                        in_queue[e["to"]] = True
                        q.append(e["to"])

        if dist[sink] == inf:
            break  # 没有增广路了 -> 已达到最大流

        add = need - flow
        v = sink
        while v != source:
            u = prev_v[v]
            e = graph[u][prev_e[v]]
            add = min(add, e["cap"])
            v = u

        v = sink
        while v != source:
            u = prev_v[v]
            ei = prev_e[v]
            e = graph[u][ei]
            e["cap"] -= add
            graph[v][e["rev"]]["cap"] += add
            v = u

        flow += add
        cost += add * dist[sink]

    return flow, cost


def plan_replacement(
    works: Iterable[str],
    capacities: dict[str, int],
    recusals: Iterable[tuple[str, str]],
    assignments: Iterable[AssignmentView],
) -> Plan:
    """根据当前有效派单快照计算补位计划。

    ``works`` 顺序会影响同费用下的确定性选择；``capacities`` 为
    judge_code -> 接单上限；``recusals`` 为 (judge_code, work_code) 集合。
    传入的 assignments 应只包含 **有效（active）** 派单。
    """
    work_list = list(dict.fromkeys(works))
    work_index = {w: i for i, w in enumerate(work_list)}
    judge_list = sorted(capacities.keys())
    judge_index = {j: i for i, j in enumerate(judge_list)}
    recusal_set = set(recusals)
    active = list(assignments)

    errors: list[str] = []

    # ---- 1. 汇总已交分 / 待评 ----------------------------------------
    scored_by_work: dict[str, list[str]] = {w: [] for w in work_list}
    pending_by_work: dict[str, list[AssignmentView]] = {w: [] for w in work_list}
    scored_count_judge: dict[str, int] = {j: 0 for j in judge_list}
    pending_edges: list[AssignmentView] = []

    for a in active:
        if a.work_code not in work_index:
            errors.append(f"派单 {a.id} 引用了未知作品 {a.work_code}")
            continue
        if a.judge_code not in judge_index:
            errors.append(f"派单 {a.id} 引用了未知评委 {a.judge_code}")
            continue
        if a.scored:
            scored_by_work[a.work_code].append(a.judge_code)
            scored_count_judge[a.judge_code] += 1
        else:
            pending_by_work[a.work_code].append(a)
            pending_edges.append(a)

    # 数据不变量校验（正常应用流程不会触发，JSON 载入层也会拦）
    for w in work_list:
        judges = scored_by_work[w] + [a.judge_code for a in pending_by_work[w]]
        if len(judges) > SEATS_PER_WORK:
            errors.append(f"作品 {w} 的有效派单超过 {SEATS_PER_WORK} 张")
        if len(set(judges)) != len(judges):
            errors.append(f"作品 {w} 存在同一评委的多张有效派单")
        for j in scored_by_work[w]:
            if (j, w) in recusal_set:
                errors.append(f"已交分派单 {j} -> {w} 违反回避，无法在保留评分的同时补齐")

    residual_cap: dict[str, int] = {}
    for j in judge_list:
        cap = int(capacities[j])
        fixed = scored_count_judge[j]
        if fixed > cap:
            errors.append(
                f"评委 {j} 已交分 {fixed} 单，超过接单上限 {cap}（已交分必须保留）"
            )
        residual_cap[j] = max(0, cap - fixed)

    if errors:
        # 硬性不一致：不产出可执行计划
        return Plan(
            complete=False, target_seats=0, filled_seats=0,
            remove=[], add=[], kept_pending=[], fixed_scored=[],
            short_works=[], eligible_counts={}, errors=errors,
        )

    # ---- 2. 建流网络 --------------------------------------------------
    n_w = len(work_list)
    n_j = len(judge_list)
    source = 0
    judge_base = 1
    work_base = 1 + n_j
    sink = work_base + n_w
    node_count = sink + 1
    graph: list[list[dict]] = [[] for _ in range(node_count)]

    judge_edges: list[tuple[dict, str, str, int | None, bool]] = []

    for j in judge_list:
        c = residual_cap[j]
        if c > 0:
            _add_edge(graph, source, judge_base + judge_index[j], c, 0)

    target = 0
    work_sink_edge: dict[str, dict] = {}
    for wi, w in enumerate(work_list):
        scored_judges = set(scored_by_work[w])
        demand = SEATS_PER_WORK - len(scored_judges)
        if demand <= 0:
            continue
        target += demand
        we = _add_edge(graph, work_base + wi, sink, demand, 0,
                       meta={"work_code": w})
        work_sink_edge[w] = we

        existing_pending = {a.judge_code: a for a in pending_by_work[w]}

        for j in judge_list:
            if j in scored_judges:
                continue  # 已交分评委已占该席位
            if (j, w) in recusal_set:
                continue  # 回避
            a = existing_pending.get(j)
            if a is not None:
                cost = 0
                aid = a.id
                is_pending = True
            else:
                cost = COST_NEW
                aid = None
                is_pending = False
            e = _add_edge(
                graph,
                judge_base + judge_index[j],
                work_base + wi,
                1,
                cost,
                meta={"judge_code": j, "work_code": w,
                      "assignment_id": aid, "pending": is_pending},
            )
            judge_edges.append((e, j, w, aid, is_pending))

    # ---- 3. 求最小费用最大流 ------------------------------------------
    flow, _cost = _min_cost_max_flow(graph, source, sink, target)

    selected_pending_ids: set[int] = set()
    remove: list[dict] = []
    add: list[dict] = []

    for e, j, w, aid, is_pending in judge_edges:
        selected = e["cap"] < e["orig_cap"]  # 容量 1：最终 cap=0 即被选中
        if not selected:
            continue
        if is_pending:
            selected_pending_ids.add(aid)  # type: ignore[arg-type]
        else:
            add.append({"work_code": w, "judge_code": j})

    kept_pending = sorted(selected_pending_ids)
    for a in pending_edges:
        if a.id not in selected_pending_ids:
            remove.append({
                "assignment_id": a.id,
                "work_code": a.work_code,
                "judge_code": a.judge_code,
            })

    fixed_scored = sorted(
        a.id for a in active if a.scored and a.work_code in work_index
    )

    # 候选评委数（未回避、且未在该作品已交分评委中），仅用于展示/诊断
    eligible_counts: dict[str, int] = {}
    for w in work_list:
        scored_judges = set(scored_by_work[w])
        eligible_counts[w] = sum(
            1 for j in judge_list
            if (j, w) not in recusal_set and j not in scored_judges
        )

    short_works: list[dict] = []
    for w in work_list:
        we = work_sink_edge.get(w)
        demand = SEATS_PER_WORK - len(scored_by_work[w])
        if demand <= 0 or we is None:
            continue
        got = we["orig_cap"] - we["cap"]
        if got < demand:
            short_works.append({
                "work_code": w,
                "valid_scores": len(scored_by_work[w]),
                "pending_on_plan": got,
                "need_more_seats": demand - got,
                "eligible_judges": eligible_counts.get(w, 0),
            })

    complete = flow >= target and not short_works and not errors

    # 稳定排序，方便阅读与测试
    remove.sort(key=lambda r: r["assignment_id"])
    add.sort(key=lambda r: (r["work_code"], r["judge_code"]))
    short_works.sort(key=lambda r: r["work_code"])

    return Plan(
        complete=complete,
        target_seats=target,
        filled_seats=flow,
        remove=remove,
        add=add,
        kept_pending=kept_pending,
        fixed_scored=fixed_scored,
        short_works=short_works,
        eligible_counts=eligible_counts,
        errors=errors,
    )
