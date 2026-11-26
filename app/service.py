"""业务服务层：交分、补报回避、生成预览、原子确认补位。

只依赖传入的 ``sqlite3.Connection``（便于 Flask 路由与测试共用）。
"""
from __future__ import annotations

import json
import math
import secrets
import sqlite3
from typing import Any

from .solver import AssignmentView, plan_replacement

SCORE_MIN = 0.0
SCORE_MAX = 100.0


class ServiceError(Exception):
    def __init__(self, message: str, status: int = 400,
                 code: str = "bad_request", extra: dict | None = None):
        super().__init__(message)
        self.message = message
        self.status = status
        self.code = code
        self.extra = extra or {}


def _rollback_quiet(db: sqlite3.Connection) -> None:
    try:
        db.execute("ROLLBACK")
    except sqlite3.Error:
        pass


def add_event(db: sqlite3.Connection, kind: str, detail: dict) -> None:
    db.execute(
        "INSERT INTO events(kind, detail_json) VALUES(?,?)",
        (kind, json.dumps(detail, ensure_ascii=False)),
    )


def parse_score(raw: Any) -> float:
    if isinstance(raw, bool) or raw is None:
        raise ServiceError("请输入分数（0~100 的数字）", 400, "invalid_score")
    if isinstance(raw, str):
        text = raw.strip()
        if not text:
            raise ServiceError("分数不能为空", 400, "invalid_score")
        try:
            value = float(text)
        except ValueError:
            raise ServiceError(f"分数不是数字: {raw!r}", 400, "invalid_score")
    elif isinstance(raw, (int, float)):
        value = float(raw)
    else:
        raise ServiceError("分数格式无法识别", 400, "invalid_score")

    if not math.isfinite(value):
        raise ServiceError("分数必须是有限数字", 400, "invalid_score")
    if not (SCORE_MIN <= value <= SCORE_MAX):
        raise ServiceError(
            f"分数 {value:g} 超出允许范围 {SCORE_MIN:g}~{SCORE_MAX:g}",
            400, "score_out_of_range",
        )
    return round(value, 2)


# ---------------------------------------------------------------------------
# 交分
# ---------------------------------------------------------------------------

def submit_score(db: sqlite3.Connection, assignment_id: int,
                 raw_score: Any) -> dict:
    """对一张派单交分。每单仅一次；作废单（含改派后的旧单）一律拒收。"""
    score = parse_score(raw_score)

    db.execute("BEGIN IMMEDIATE")
    try:
        row = db.execute(
            "SELECT * FROM assignments WHERE id = ?", (assignment_id,)
        ).fetchone()
        if row is None:
            raise ServiceError(f"派单 #{assignment_id} 不存在", 404,
                               "assignment_not_found")
        if row["status"] != "active":
            raise ServiceError(
                f"派单 #{assignment_id} 已作废"
                + (f"（原因：{row['void_reason']}）" if row["void_reason"] else "")
                + "，旧单分数被拒收",
                409, "assignment_voided",
                extra={"assignment_id": assignment_id,
                       "void_reason": row["void_reason"]},
            )
        if row["score"] is not None:
            raise ServiceError(
                f"派单 #{assignment_id} 已交过分（{row['score']:g}），每单只收一次",
                409, "already_scored",
                extra={"assignment_id": assignment_id,
                       "score": row["score"]},
            )

        cur = db.execute(
            "UPDATE assignments SET score = ?, "
            "submitted_at = datetime('now','localtime') "
            "WHERE id = ? AND status = 'active' AND score IS NULL",
            (score, assignment_id),
        )
        if cur.rowcount != 1:
            # 并发下被别人抢先交分/作废
            raise ServiceError("交分状态已变化，请刷新后重试", 409,
                               "score_state_changed")

        add_event(db, "score_submitted", {
            "assignment_id": assignment_id,
            "work_code": row["work_code"],
            "judge_code": row["judge_code"],
            "score": score,
        })
        db.execute("COMMIT")
    except ServiceError:
        _rollback_quiet(db)
        raise
    except Exception as exc:  # 数据库写入异常等
        _rollback_quiet(db)
        raise ServiceError(f"交分写入失败：{exc}", 500, "write_failed") from exc

    return {"assignment_id": assignment_id, "score": score, "accepted": True}


# ---------------------------------------------------------------------------
# 补报回避
# ---------------------------------------------------------------------------

def report_recusal(db: sqlite3.Connection, judge_code: str, work_code: str,
                   reason: str = "") -> dict:
    judge_code = (judge_code or "").strip()
    work_code = (work_code or "").strip()
    if not judge_code or not work_code:
        raise ServiceError("必须选择评委与作品", 400, "missing_refs")

    judge = db.execute("SELECT code FROM judges WHERE code=?",
                       (judge_code,)).fetchone()
    if judge is None:
        raise ServiceError(f"评委 {judge_code} 不存在", 404, "judge_not_found")
    work = db.execute("SELECT code FROM works WHERE code=?",
                     (work_code,)).fetchone()
    if work is None:
        raise ServiceError(f"作品 {work_code} 不存在", 404, "work_not_found")

    db.execute("BEGIN IMMEDIATE")
    try:
        existing = db.execute(
            "SELECT 1 FROM recusals WHERE judge_code=? AND work_code=?",
            (judge_code, work_code),
        ).fetchone()
        already = existing is not None
        if not already:
            db.execute(
                "INSERT INTO recusals(judge_code, work_code, reason, source) "
                "VALUES(?,?,?,'manual')",
                (judge_code, work_code, reason.strip()),
            )
        elif reason.strip():
            db.execute(
                "UPDATE recusals SET reason=? WHERE judge_code=? AND work_code=? "
                "AND (reason IS NULL OR reason='')",
                (reason.strip(), judge_code, work_code),
            )

        row = db.execute(
            "SELECT * FROM assignments WHERE judge_code=? AND work_code=? "
            "AND status='active'",
            (judge_code, work_code),
        ).fetchone()
        voided = None
        if row is not None:
            db.execute(
                "UPDATE assignments SET status='voided', "
                "voided_at=datetime('now','localtime'), void_reason='recusal' "
                "WHERE id=? AND status='active'",
                (row["id"],),
            )
            voided = {
                "assignment_id": row["id"],
                "work_code": row["work_code"],
                "judge_code": row["judge_code"],
                "voided_score": row["score"],
                "had_score": row["score"] is not None,
            }
            add_event(db, "assignment_voided_recusal", voided)

        add_event(db, "recusal_reported", {
            "judge_code": judge_code,
            "work_code": work_code,
            "reason": reason.strip(),
            "already_recused": already,
            "voided": voided,
        })
        db.execute("COMMIT")
    except ServiceError:
        _rollback_quiet(db)
        raise
    except Exception as exc:
        _rollback_quiet(db)
        raise ServiceError(f"补报回避失败：{exc}", 500, "write_failed") from exc

    return {
        "judge_code": judge_code,
        "work_code": work_code,
        "already_recused": already,
        "voided": voided,
    }


# ---------------------------------------------------------------------------
# 快照与预览
# ---------------------------------------------------------------------------

def build_plan(db: sqlite3.Connection):
    works = [r["code"] for r in db.execute(
        "SELECT code FROM works ORDER BY position, code")]
    capacities = {r["code"]: r["capacity"] for r in db.execute(
        "SELECT code, capacity FROM judges")}
    recusals = {(r["judge_code"], r["work_code"]) for r in db.execute(
        "SELECT judge_code, work_code FROM recusals")}
    assignments = [
        AssignmentView(
            id=r["id"], work_code=r["work_code"], judge_code=r["judge_code"],
            scored=r["score"] is not None,
        )
        for r in db.execute(
            "SELECT id, work_code, judge_code, score FROM assignments "
            "WHERE status='active'")
    ]
    return plan_replacement(works, capacities, recusals, assignments)


def create_preview(db: sqlite3.Connection) -> tuple[str, dict]:
    plan = build_plan(db)
    token = secrets.token_urlsafe(12)
    envelope = {"version": 1, "plan": plan.to_dict()}
    db.execute(
        "INSERT INTO previews(token, status, plan_json) VALUES(?,'open',?)",
        (token, json.dumps(envelope, ensure_ascii=False)),
    )
    return token, envelope


def get_preview_row(db: sqlite3.Connection, token: str) -> sqlite3.Row | None:
    return db.execute(
        "SELECT * FROM previews WHERE token=?", (token,)
    ).fetchone()


def preview_envelope(row: sqlite3.Row) -> dict:
    return json.loads(row["plan_json"])


def find_blockers(db: sqlite3.Connection, plan: dict) -> list[dict]:
    """检查预览列出的“待撤旧单”此刻是否仍可撤。

    关键规则：若其中某单在预览后收到了新分 -> scored 阻断，必须重算重确认。
    其他派单交分不在此列表中，不构成阻断。
    """
    blockers: list[dict] = []
    for item in plan.get("remove", []):
        aid = item["assignment_id"]
        row = db.execute(
            "SELECT * FROM assignments WHERE id=?", (aid,)
        ).fetchone()
        base = {"assignment_id": aid,
                "work_code": item.get("work_code"),
                "judge_code": item.get("judge_code")}
        if row is None:
            blockers.append({**base, "type": "missing",
                             "message": f"预览拟撤的派单 #{aid} 已不存在"})
            continue
        if row["work_code"] != item.get("work_code") or \
                row["judge_code"] != item.get("judge_code"):
            blockers.append({**base, "type": "identity_changed",
                             "message": f"派单 #{aid} 身份信息已变化"})
            continue
        if row["status"] != "active":
            blockers.append({**base, "type": "not_active",
                             "status": row["status"],
                             "void_reason": row["void_reason"],
                             "message": f"预览拟撤的派单 #{aid} 已不是有效派单"})
            continue
        if row["score"] is not None:
            blockers.append({**base, "type": "scored",
                             "score": row["score"],
                             "message": (
                                 f"预览拟撤的派单 #{aid}（{row['judge_code']} → "
                                 f"{row['work_code']}）已收到新分 "
                                 f"{row['score']:g}，已交分派单必须保留，"
                                 "请重新计算并重新确认")})
    return blockers


def simulate_final_state(db: sqlite3.Connection, plan: dict,
                         remove_ids: set[int]) -> list[str]:
    """在事务内模拟执行计划后的状态，完整校验两评委/回避/上限。"""
    errors: list[str] = []

    judge_exists = {r["code"] for r in db.execute("SELECT code FROM judges")}
    work_exists = {r["code"] for r in db.execute("SELECT code FROM works")}
    capacities = {r["code"]: r["capacity"] for r in db.execute(
        "SELECT code, capacity FROM judges")}
    recusals = {(r["judge_code"], r["work_code"]) for r in db.execute(
        "SELECT judge_code, work_code FROM recusals")}

    final_pairs: dict[tuple[str, str], dict] = {}
    for row in db.execute("SELECT * FROM assignments WHERE status='active'"):
        if row["id"] in remove_ids:
            continue
        final_pairs[(row["work_code"], row["judge_code"])] = {
            "id": row["id"], "score": row["score"], "new": False}

    for item in plan.get("add", []):
        w, j = item["work_code"], item["judge_code"]
        if w not in work_exists:
            errors.append(f"新增派单引用了不存在的作品 {w}")
            continue
        if j not in judge_exists:
            errors.append(f"新增派单引用了不存在的评委 {j}")
            continue
        if (w, j) in final_pairs:
            errors.append(f"新增派单 {j} → {w} 与保留派单重复")
            continue
        final_pairs[(w, j)] = {"id": None, "score": None, "new": True}

    if errors:
        return errors

    by_work: dict[str, list[str]] = {}
    judge_count: dict[str, int] = {}
    for (w, j) in final_pairs:
        by_work.setdefault(w, []).append(j)
        judge_count[j] = judge_count.get(j, 0) + 1
        if (j, w) in recusals:
            errors.append(f"改派结果 {j} → {w} 违反当前回避关系")

    for w in work_exists:
        js = by_work.get(w, [])
        if len(js) != 2:
            errors.append(f"作品 {w} 改派后有效派单为 {len(js)} 张，应为 2 张")
        elif len(set(js)) != 2:
            errors.append(f"作品 {w} 改派后两名评委相同")

    for j, cnt in judge_count.items():
        if cnt > capacities.get(j, 0):
            errors.append(
                f"评委 {j} 改派后有效派单 {cnt} 张，超过上限 "
                f"{capacities.get(j)} 张")

    return errors


def assess_preview(db: sqlite3.Connection, token: str) -> dict:
    """只读评估预览当前是否仍可确认（供预览页展示警告）。"""
    row = get_preview_row(db, token)
    if row is None:
        raise ServiceError("预览不存在", 404, "preview_not_found")
    envelope = preview_envelope(row)
    plan = envelope["plan"]
    blockers = find_blockers(db, plan) if row["status"] == "open" else []
    validation_errors: list[str] = []
    if row["status"] == "open" and not blockers and plan.get("complete"):
        validation_errors = simulate_final_state(
            db, plan, {r["assignment_id"] for r in plan.get("remove", [])})
    applicable = (
        row["status"] == "open"
        and plan.get("complete", False)
        and not blockers
        and not validation_errors
    )
    return {
        "token": token,
        "status": row["status"],
        "created_at": row["created_at"],
        "confirmed_at": row["confirmed_at"],
        "conflict_reason": row["conflict_reason"],
        "result": json.loads(row["result_json"]) if row["result_json"] else None,
        "plan": plan,
        "blockers": blockers,
        "blocker_map": {b["assignment_id"]: b for b in blockers},
        "validation_errors": validation_errors,
        "applicable": applicable,
    }


def confirm_preview(db: sqlite3.Connection, token: str,
                    inject_failure_after: int | None = None) -> dict:
    """整批执行预览列出的撤单与改派。

    * 拟撤旧单若已收到新分 -> 返回 status='stale'，预览被标记失效，
      调用方应引导用户重算并重确认（不是异常 500）。
    * 其他状态漂移导致整批校验不通过 -> 同样 stale。
    * 真正的写入失败 -> 回滚整个事务，原有效派单全部保留，并抛 500。
    * ``inject_failure_after`` 仅用于自测：执行第 N 条写语句后注入故障。
    """
    row = get_preview_row(db, token)
    if row is None:
        raise ServiceError("预览不存在或已过期", 404, "preview_not_found")
    if row["status"] == "confirmed":
        return {"status": "already_confirmed",
                "result": json.loads(row["result_json"] or "{}")}

    db.execute("BEGIN IMMEDIATE")
    try:
        row = db.execute(
            "SELECT * FROM previews WHERE token=?", (token,)
        ).fetchone()
        if row["status"] == "confirmed":
            db.execute("COMMIT")
            return {"status": "already_confirmed",
                    "result": json.loads(row["result_json"] or "{}")}

        envelope = preview_envelope(row)
        plan = envelope["plan"]

        if not plan.get("complete"):
            db.execute("ROLLBACK")
            return {"status": "not_confirmable",
                    "reason": "该预览未能补齐所有席位，不能执行改派；"
                              "请先处理缺评委的作品"}

        blockers = find_blockers(db, plan)
        if blockers:
            msg = "预览后拟撤派单状态发生变化（其中有新交分），" \
                  "该预览已失效，请重新计算并重新确认"
            db.execute(
                "UPDATE previews SET status='stale', conflict_reason=? "
                "WHERE token=?",
                (msg, token),
            )
            db.execute("COMMIT")
            return {"status": "stale", "blockers": blockers, "message": msg}

        remove_ids = {item["assignment_id"] for item in plan.get("remove", [])}
        validation_errors = simulate_final_state(db, plan, remove_ids)
        if validation_errors:
            msg = "当前数据与预览生成时相比发生变化，整批校验未通过，" \
                  "请重新计算并重新确认"
            db.execute(
                "UPDATE previews SET status='stale', conflict_reason=? "
                "WHERE token=?",
                (msg + " | " + "；".join(validation_errors), token),
            )
            db.execute("COMMIT")
            return {"status": "stale",
                    "validation_errors": validation_errors, "message": msg}

        writes = 0
        removed: list[int] = []
        for item in plan.get("remove", []):
            aid = item["assignment_id"]
            cur = db.execute(
                "UPDATE assignments SET status='voided', "
                "voided_at=datetime('now','localtime'), "
                "void_reason='replacement' "
                "WHERE id=? AND status='active' AND score IS NULL",
                (aid,),
            )
            if cur.rowcount != 1:
                raise ServiceError(
                    f"拟撤派单 #{aid} 已不满足撤单条件（可能刚收到分数）",
                    409, "remove_condition_failed")
            removed.append(aid)
            writes += 1
            if inject_failure_after is not None and writes >= inject_failure_after:
                raise RuntimeError("injected failure during remove batch")

        added: list[dict] = []
        for item in plan.get("add", []):
            cur = db.execute(
                "INSERT INTO assignments(work_code, judge_code, status) "
                "VALUES(?,?,'active')",
                (item["work_code"], item["judge_code"]),
            )
            added.append({"assignment_id": cur.lastrowid,
                          "work_code": item["work_code"],
                          "judge_code": item["judge_code"]})
            writes += 1
            if inject_failure_after is not None and writes >= inject_failure_after:
                raise RuntimeError("injected failure during add batch")

        result = {"removed_assignment_ids": removed, "added": added}
        add_event(db, "preview_confirmed", {
            "token": token, **result,
        })
        db.execute(
            "UPDATE previews SET status='confirmed', result_json=?, "
            "confirmed_at=datetime('now','localtime') WHERE token=?",
            (json.dumps(result, ensure_ascii=False), token),
        )
        db.execute("COMMIT")
    except ServiceError:
        _rollback_quiet(db)
        raise
    except Exception as exc:
        _rollback_quiet(db)
        raise ServiceError(
            f"确认过程中写入失败，整批改派已回滚，原有效派单保留：{exc}",
            500, "write_failed") from exc

    return {"status": "confirmed", "result": result}


# ---------------------------------------------------------------------------
# 总览查询
# ---------------------------------------------------------------------------

def overview(db: sqlite3.Connection) -> dict:
    works = [dict(r) for r in db.execute(
        "SELECT code, title, position FROM works ORDER BY position, code")]
    judges = [dict(r) for r in db.execute(
        "SELECT code, name, capacity FROM judges ORDER BY position, code")]

    assignments = [dict(r) for r in db.execute(
        "SELECT a.*, j.name AS judge_name, w.title AS work_title "
        "FROM assignments a "
        "JOIN judges j ON j.code = a.judge_code "
        "JOIN works w ON w.code = a.work_code "
        "ORDER BY a.work_code, a.id")]

    by_work: dict[str, list[dict]] = {w["code"]: [] for w in works}
    for a in assignments:
        by_work.setdefault(a["work_code"], []).append(a)

    work_views = []
    for w in works:
        rows = by_work.get(w["code"], [])
        active = [a for a in rows if a["status"] == "active"]
        voided = [a for a in rows if a["status"] == "voided"]
        scored = [a for a in active if a["score"] is not None]
        total = round(sum(a["score"] for a in scored), 2) \
            if len(scored) == 2 else None
        average = round(total / 2, 2) if total is not None else None
        work_views.append({
            **w,
            "active": active,
            "voided": voided,
            "scored_count": len(scored),
            "active_count": len(active),
            "total_score": total,
            "average_score": average,
            "total_ready": len(scored) == 2,
        })

    usage = {r["judge_code"]: r["n"] for r in db.execute(
        "SELECT judge_code, COUNT(*) AS n FROM assignments "
        "WHERE status='active' GROUP BY judge_code")}
    for j in judges:
        j["active_count"] = usage.get(j["code"], 0)

    return {"works": work_views, "judges": judges,
            "all_assignments": assignments}
