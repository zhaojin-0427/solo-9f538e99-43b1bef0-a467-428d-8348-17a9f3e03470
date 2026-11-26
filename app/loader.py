"""从 JSON 载入影展数据：作品（≤24）、评委接单上限、回避关系、原派单。

支持的 JSON 形态见 data/sample_data.json。键名同时兼容英文与常见中文写法，
评委表也支持 ``{"J01": 5}`` 或 ``{"J01": {"name": "张", "capacity": 5}}``
这样的字典写法。
"""
from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from typing import Any

MAX_WORKS = 24

_WORK_KEYS = ("code", "id", "work_code", "work_id", "编号", "作品编号", "作品号")
_WORK_TITLE_KEYS = ("title", "name", "名称", "标题", "作品名")
_JUDGE_KEYS = ("code", "id", "judge_code", "judge_id", "编号", "评委编号", "评委号")
_JUDGE_NAME_KEYS = ("name", "judge_name", "姓名", "名字", "评委名")
_CAP_KEYS = ("capacity", "limit", "max", "max_orders", "接单上限", "上限", "容量")
_JUDGE_REF_KEYS = ("judge", "judge_code", "judge_id", "评委", "评委编号", "评委号")
_WORK_REF_KEYS = ("work", "work_code", "work_id", "作品", "作品编号", "作品号")
_REASON_KEYS = ("reason", "note", "原因", "理由", "说明")
_SCORE_KEYS = ("score", "points", "评分", "分数", "打分")


class LoaderError(Exception):
    def __init__(self, errors: list[str] | str):
        self.errors = [errors] if isinstance(errors, str) else list(errors)
        super().__init__("; ".join(self.errors))


def _pick(item: dict, keys: tuple[str, ...], default: Any = None) -> Any:
    for k in keys:
        if k in item and item[k] is not None:
            return item[k]
    return default


def _as_code(value: Any) -> str:
    if value is None:
        return ""
    return str(value).strip()


def _as_capacity(value: Any, judge_code: str, errors: list[str]) -> int:
    if isinstance(value, bool):
        errors.append(f"评委 {judge_code} 的接单上限不能是布尔值: {value!r}")
        return 0
    try:
        cap = int(value)
    except (TypeError, ValueError):
        errors.append(f"评委 {judge_code} 的接单上限不是整数: {value!r}")
        return 0
    if cap < 0:
        errors.append(f"评委 {judge_code} 的接单上限不能为负: {cap}")
    return cap


def parse_payload(payload: Any) -> dict:
    """把任意 JSON 对象规范化为 {works, judges, recusals, assignments}。

    仅做结构解析，不做引用校验（引用校验在 load_payload 中结合数据库进行，
    但这里也做纯数据层面的校验并抛出 LoaderError）。
    """
    if not isinstance(payload, dict):
        raise LoaderError("JSON 顶层必须是对象 {}")

    def first_present(*keys, default=None):
        for k in keys:
            if k in payload:
                return payload[k]
        return default

    raw_works = first_present("works", "作品", "works_data", default=[])
    raw_judges = first_present("judges", "评委", default=[])
    raw_recusals = first_present("recusals", "conflicts", "回避", "回避关系", default=[])
    raw_assignments = first_present(
        "assignments", "orders", "dispatches", "派单", "原派单", default=[]
    )

    errors: list[str] = []

    # ---- 作品 ----------------------------------------------------------
    works: list[dict] = []
    if not isinstance(raw_works, list):
        raise LoaderError("works / 作品 必须是数组")
    if len(raw_works) == 0:
        errors.append("至少需要 1 件作品")
    if len(raw_works) > MAX_WORKS:
        errors.append(f"作品数量 {len(raw_works)} 超过上限 {MAX_WORKS} 件")

    seen_work: set[str] = set()
    for i, item in enumerate(raw_works):
        if not isinstance(item, dict):
            errors.append(f"第 {i + 1} 件作品不是对象: {item!r}")
            continue
        code = _as_code(_pick(item, _WORK_KEYS))
        if not code:
            errors.append(f"第 {i + 1} 件作品缺少编号")
            continue
        if code in seen_work:
            errors.append(f"作品编号重复: {code}")
            continue
        seen_work.add(code)
        title = str(_pick(item, _WORK_TITLE_KEYS, "") or "")
        works.append({"code": code, "title": title})

    # ---- 评委（数组或字典） -------------------------------------------
    judges: list[dict] = []
    seen_judge: set[str] = set()

    def add_judge(code: str, name: str, cap_raw: Any, idx_label: str):
        code = _as_code(code)
        if not code:
            errors.append(f"{idx_label} 评委缺少编号")
            return
        if code in seen_judge:
            errors.append(f"评委编号重复: {code}")
            return
        cap = _as_capacity(cap_raw, code, errors)
        seen_judge.add(code)
        judges.append({"code": code, "name": name or code, "capacity": cap})

    if isinstance(raw_judges, dict):
        for code, val in raw_judges.items():
            if isinstance(val, dict):
                add_judge(
                    _pick(val, _JUDGE_KEYS, code),
                    str(_pick(val, _JUDGE_NAME_KEYS, "") or ""),
                    _pick(val, _CAP_KEYS, 0),
                    f"评委 {code}",
                )
            elif isinstance(val, int):
                add_judge(code, code, val, f"评委 {code}")
            else:
                errors.append(f"评委 {code} 的配置格式无法识别: {val!r}")
    elif isinstance(raw_judges, list):
        for i, item in enumerate(raw_judges):
            if isinstance(item, str):
                add_judge(item, item, 10**6, f"第 {i + 1} 位")
            elif isinstance(item, dict):
                add_judge(
                    _pick(item, _JUDGE_KEYS),
                    str(_pick(item, _JUDGE_NAME_KEYS, "") or ""),
                    _pick(item, _CAP_KEYS, 0),
                    f"第 {i + 1} 位",
                )
            else:
                errors.append(f"第 {i + 1} 位评委格式无法识别: {item!r}")
    else:
        errors.append("judges / 评委 必须是数组或对象")

    # ---- 回避关系 ------------------------------------------------------
    recusals: list[dict] = []
    if not isinstance(raw_recusals, list):
        errors.append("recusals / 回避关系 必须是数组")
        raw_recusals = []
    seen_recusal: set[tuple[str, str]] = set()
    for i, item in enumerate(raw_recusals):
        if not isinstance(item, dict):
            errors.append(f"第 {i + 1} 条回避关系不是对象")
            continue
        j = _as_code(_pick(item, _JUDGE_REF_KEYS))
        w = _as_code(_pick(item, _WORK_REF_KEYS))
        if not j or not w:
            errors.append(f"第 {i + 1} 条回避关系缺少 judge/work: {item!r}")
            continue
        if (j, w) in seen_recusal:
            continue
        seen_recusal.add((j, w))
        recusals.append({"judge_code": j, "work_code": w,
                         "reason": str(_pick(item, _REASON_KEYS, "") or "")})

    # ---- 原派单 --------------------------------------------------------
    assignments: list[dict] = []
    if not isinstance(raw_assignments, list):
        errors.append("assignments / 原派单 必须是数组")
        raw_assignments = []
    seen_pair: set[tuple[str, str]] = set()
    for i, item in enumerate(raw_assignments):
        if not isinstance(item, dict):
            errors.append(f"第 {i + 1} 张派单不是对象")
            continue
        w = _as_code(_pick(item, _WORK_REF_KEYS))
        j = _as_code(_pick(item, _JUDGE_REF_KEYS))
        if not w or not j:
            errors.append(f"第 {i + 1} 张派单缺少 work/judge: {item!r}")
            continue
        if (w, j) in seen_pair:
            errors.append(f"派单重复: {j} -> {w}")
            continue
        seen_pair.add((w, j))
        score = _pick(item, _SCORE_KEYS, None)
        parsed_score: float | None = None
        if score is not None and score != "":
            try:
                if isinstance(score, bool):
                    raise ValueError
                parsed_score = float(score)
            except (TypeError, ValueError):
                errors.append(f"派单 {j} -> {w} 的评分不是数字: {score!r}")
        assignments.append({"work_code": w, "judge_code": j, "score": parsed_score})

    # ---- 引用与业务规则校验 -------------------------------------------
    work_codes = {w["code"] for w in works}
    judge_codes = {j["code"] for j in judges}

    for r in recusals:
        if r["judge_code"] not in judge_codes:
            errors.append(f"回避关系引用了不存在的评委: {r['judge_code']}")
        if r["work_code"] not in work_codes:
            errors.append(f"回避关系引用了不存在的作品: {r['work_code']}")

    per_work: dict[str, list[str]] = {}
    per_judge_count: dict[str, int] = {}
    for a in assignments:
        if a["work_code"] not in work_codes:
            errors.append(f"派单引用了不存在的作品: {a['work_code']}")
        if a["judge_code"] not in judge_codes:
            errors.append(f"派单引用了不存在的评委: {a['judge_code']}")
        if a["work_code"] in work_codes and a["judge_code"] in judge_codes:
            per_work.setdefault(a["work_code"], []).append(a["judge_code"])
            per_judge_count[a["judge_code"]] = per_judge_count.get(a["judge_code"], 0) + 1
            if (a["judge_code"], a["work_code"]) in seen_recusal:
                errors.append(
                    f"原派单 {a['judge_code']} -> {a['work_code']} 违反已知回避关系"
                )
            if a["score"] is not None and not (0 <= a["score"] <= 100):
                errors.append(
                    f"派单 {a['judge_code']} -> {a['work_code']} "
                    f"评分 {a['score']} 超出 0~100"
                )

    for w, js in per_work.items():
        if len(js) > 2:
            errors.append(f"作品 {w} 有 {len(js)} 张原派单，超过 2 名评委")
        if len(set(js)) != len(js):
            errors.append(f"作品 {w} 的原派单中同一评委出现多次")

    cap_map = {j["code"]: j["capacity"] for j in judges}
    for j, cnt in per_judge_count.items():
        if j in cap_map and cnt > cap_map[j]:
            errors.append(
                f"评委 {j} 的原派单 {cnt} 张超过接单上限 {cap_map[j]}"
            )

    if errors:
        raise LoaderError(errors)

    return {
        "works": works,
        "judges": judges,
        "recusals": recusals,
        "assignments": assignments,
    }


def load_payload(
    conn: sqlite3.Connection,
    payload: Any,
    *,
    replace: bool = False,
    source: str = "json",
) -> dict:
    """校验并把 payload 写入数据库。

    ``replace=True`` 时在一个事务内清空全部业务数据后重灌；任何写入失败
    都会回滚，保留载入前的数据。
    """
    data = parse_payload(payload)

    conn.execute("BEGIN IMMEDIATE")
    try:
        if replace:
            for table in ("events", "previews", "assignments",
                          "recusals", "judges", "works"):
                conn.execute(f"DELETE FROM {table}")

        for i, w in enumerate(data["works"]):
            conn.execute(
                "INSERT INTO works(code, title, position) VALUES(?,?,?)",
                (w["code"], w["title"], i),
            )
        for i, j in enumerate(data["judges"]):
            conn.execute(
                "INSERT INTO judges(code, name, capacity, position) VALUES(?,?,?,?)",
                (j["code"], j["name"], j["capacity"], i),
            )
        for r in data["recusals"]:
            conn.execute(
                "INSERT INTO recusals(judge_code, work_code, reason, source) "
                "VALUES(?,?,?,?)",
                (r["judge_code"], r["work_code"], r["reason"], source),
            )
        for a in data["assignments"]:
            conn.execute(
                "INSERT INTO assignments(work_code, judge_code, status, score, "
                "submitted_at) VALUES(?,?,'active',?, CASE WHEN ? IS NOT NULL "
                "THEN datetime('now','localtime') ELSE NULL END)",
                (a["work_code"], a["judge_code"], a["score"], a["score"]),
            )
        conn.execute(
            "INSERT INTO events(kind, detail_json) VALUES('data_loaded', ?)",
            (json.dumps({
                "source": source,
                "replace": replace,
                "works": len(data["works"]),
                "judges": len(data["judges"]),
                "assignments": len(data["assignments"]),
            }, ensure_ascii=False),),
        )
        conn.execute("COMMIT")
    except LoaderError:
        conn.execute("ROLLBACK")
        raise
    except Exception as exc:
        conn.execute("ROLLBACK")
        # 例如追加模式下主键/唯一约束冲突；转成结构化载入错误，事务已回滚
        raise LoaderError([f"数据库写入失败，已回滚，原数据保留：{exc}"]) from exc

    return {
        "works": len(data["works"]),
        "judges": len(data["judges"]),
        "recusals": len(data["recusals"]),
        "assignments": len(data["assignments"]),
    }


def load_file(
    conn: sqlite3.Connection,
    path: str | Path,
    *,
    replace: bool = False,
) -> dict:
    p = Path(path)
    with p.open("r", encoding="utf-8") as fh:
        payload = json.load(fh)
    return load_payload(conn, payload, replace=replace, source=str(p.name))
