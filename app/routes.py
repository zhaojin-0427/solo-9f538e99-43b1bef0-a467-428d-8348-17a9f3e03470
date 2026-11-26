"""全部网页路由与 JSON API。

网页（服务端渲染，表单与普通浏览器兼容）：
    GET  /                         总览：作品、有效评分、总分
    GET  /works/<code>             作品详情（含作废历史）
    GET  /judges                   评委列表与容量使用
    GET  /judges/<code>            评委派单页（交分）
    POST /assignments/<id>/score   表单交分（每单一次，旧单拒收）
    GET  /recusals                 回避名单 + 补报表单
    POST /recusals                 补报回避（立即作废对应派单与评分）
    GET  /preview                  生成新预览并跳转
    POST /previews                 生成新预览
    GET  /previews/<token>         查看预览
    POST /previews/<token>/confirm 整批确认改派
    GET  /events                   操作审计
    GET/POST /admin/load            载入 JSON

JSON API 在 /api 下，语义与网页完全一致，便于脚本/自测：
    GET  /api/state
    POST /api/assignments/<id>/score
    POST /api/recusals
    POST /api/previews
    GET  /api/previews/<token>
    POST /api/previews/<token>/confirm
    POST /api/admin/load
"""
from __future__ import annotations

import json

from flask import (
    Blueprint, jsonify, redirect, render_template, request, url_for,
)

from .db import get_db
from .loader import LoaderError, load_payload
from .service import (
    ServiceError, assess_preview, confirm_preview, create_preview,
    overview, report_recusal, submit_score,
)

bp = Blueprint("main", __name__)


def wants_json() -> bool:
    if request.path.startswith("/api/"):
        return True
    return "application/json" in request.headers.get("Accept", "") and \
        "text/html" not in request.headers.get("Accept", "")


# ---------------------------------------------------------------------------
# 错误处理（在应用工厂中注册为 app 级处理器）
# ---------------------------------------------------------------------------

def handle_service_error(err: ServiceError):
    if wants_json():
        return jsonify({"ok": False, "error": err.message,
                        "code": err.code, **err.extra}), err.status
    return render_template("error.html", message=err.message,
                           code=err.code, status=err.status), err.status


def handle_404(err):  # noqa: ANN001
    if wants_json():
        return jsonify({"ok": False, "error": "not found"}), 404
    return render_template("error.html", message="页面或资源不存在",
                           code="not_found", status=404), 404


# ---------------------------------------------------------------------------
# 总览 / 作品
# ---------------------------------------------------------------------------

@bp.get("/healthz")
def healthz():
    return jsonify({"ok": True})


@bp.get("/")
def index():
    db = get_db()
    data = overview(db)
    return render_template("index.html", **data)


@bp.get("/works/<code>")
def work_detail(code: str):
    db = get_db()
    work = db.execute("SELECT * FROM works WHERE code=?", (code,)).fetchone()
    if work is None:
        raise ServiceError(f"作品 {code} 不存在", 404, "work_not_found")
    assignments = db.execute(
        "SELECT a.*, j.name AS judge_name FROM assignments a "
        "JOIN judges j ON j.code=a.judge_code "
        "WHERE a.work_code=? ORDER BY a.status DESC, a.id",
        (code,)).fetchall()
    recusals = db.execute(
        "SELECT r.*, j.name AS judge_name FROM recusals r "
        "JOIN judges j ON j.code=r.judge_code WHERE r.work_code=?",
        (code,)).fetchall()
    active_scored = [a for a in assignments
                     if a["status"] == "active" and a["score"] is not None]
    total = round(sum(a["score"] for a in active_scored), 2) \
        if len(active_scored) == 2 else None
    return render_template("work_detail.html", work=work,
                           assignments=assignments, recusals=recusals,
                           total=total, scored_count=len(active_scored))


# 常见别名
@bp.get("/work/<code>")
def work_detail_alias(code: str):
    return work_detail(code)


# ---------------------------------------------------------------------------
# 评委与派单页
# ---------------------------------------------------------------------------

def judge_context(code: str):
    db = get_db()
    judge = db.execute("SELECT * FROM judges WHERE code=?", (code,)).fetchone()
    if judge is None:
        raise ServiceError(f"评委 {code} 不存在", 404, "judge_not_found")
    assignments = db.execute(
        "SELECT a.*, w.title AS work_title FROM assignments a "
        "JOIN works w ON w.code=a.work_code "
        "WHERE a.judge_code=? ORDER BY "
        "CASE a.status WHEN 'active' THEN 0 ELSE 1 END, "
        "a.score IS NOT NULL, a.id",
        (code,)).fetchall()
    active_count = sum(1 for a in assignments if a["status"] == "active")
    return {"judge": judge, "assignments": assignments,
            "active_count": active_count}


@bp.get("/judges")
def judges_page():
    db = get_db()
    judges = db.execute(
        "SELECT j.*, ("
        "  SELECT COUNT(*) FROM assignments a "
        "  WHERE a.judge_code=j.code AND a.status='active'"
        ") AS active_count FROM judges j ORDER BY j.position, j.code"
    ).fetchall()
    return render_template("judges.html", judges=judges)


@bp.get("/judges/<code>")
def judge_page(code: str):
    return render_template("judge.html", **judge_context(code))


@bp.get("/judge/<code>")
def judge_page_alias(code: str):
    return judge_page(code)


@bp.post("/assignments/<int:assignment_id>/score")
@bp.post("/assignment/<int:assignment_id>/score")  # 单数资源别名
def submit_score_form(assignment_id: int):
    raw = request.form.get("score", "")
    db = get_db()
    try:
        result = submit_score(db, assignment_id, raw)
    except ServiceError as err:
        # 网页端直接在派单页内显式报错（保留 4xx 状态码，拒收语义明确）
        row = db.execute(
            "SELECT judge_code FROM assignments WHERE id=?",
            (assignment_id,)).fetchone()
        if row is not None:
            ctx = judge_context(row["judge_code"])
            return render_template(
                "judge.html",
                form_error=err.message,
                form_error_code=err.code,
                error_assignment_id=assignment_id,
                **ctx,
            ), err.status
        return render_template("error.html", message=err.message,
                               code=err.code, status=err.status), err.status

    judge_row = db.execute(
        "SELECT judge_code FROM assignments WHERE id=?",
        (assignment_id,)).fetchone()
    return redirect(url_for("main.judge_page",
                            code=judge_row["judge_code"],
                            scored=assignment_id))


# ---------------------------------------------------------------------------
# 回避
# ---------------------------------------------------------------------------

@bp.get("/recusals")
def recusals_page():
    db = get_db()
    recusals = db.execute(
        "SELECT r.*, j.name AS judge_name FROM recusals r "
        "JOIN judges j ON j.code=r.judge_code "
        "ORDER BY r.created_at DESC, r.judge_code, r.work_code"
    ).fetchall()
    judges = db.execute("SELECT code, name FROM judges ORDER BY position, code")
    works = db.execute("SELECT code, title FROM works ORDER BY position, code")
    return render_template("recusals.html", recusals=recusals,
                           judges=judges, works=works,
                           form_error=None)


@bp.post("/recusals")
def recusals_submit():
    judge_code = request.form.get("judge_code", "")
    work_code = request.form.get("work_code", "")
    reason = request.form.get("reason", "")
    db = get_db()
    try:
        result = report_recusal(db, judge_code, work_code, reason)
    except ServiceError as err:
        judges = db.execute(
            "SELECT code, name FROM judges ORDER BY position, code")
        works = db.execute(
            "SELECT code, title FROM works ORDER BY position, code")
        recusals = db.execute(
            "SELECT r.*, j.name AS judge_name FROM recusals r "
            "JOIN judges j ON j.code=r.judge_code "
            "ORDER BY r.created_at DESC").fetchall()
        return render_template(
            "recusals.html", recusals=recusals, judges=judges, works=works,
            form_error=err.message,
            sel_judge=judge_code, sel_work=work_code, reason=reason,
        ), err.status

    token, _envelope = create_preview(db)
    if result["voided"]:
        v = result["voided"]
        if v["had_score"]:
            msg = (f"已登记回避 {judge_code} → {work_code}，对应派单 #{v['assignment_id']} "
                   f"及其评分 {v['voided_score']:g} 已立即作废。请核对补位预览。")
        else:
            msg = (f"已登记回避 {judge_code} → {work_code}，对应待评派单 "
                   f"#{v['assignment_id']} 已立即作废。请核对补位预览。")
    elif result["already_recused"]:
        msg = f"{judge_code} → {work_code} 本就在回避名单中，当前无有效派单。"
    else:
        msg = f"已登记回避 {judge_code} → {work_code}，当前没有对应有效派单。"
    return redirect(url_for("main.preview_show", token=token, msg=msg))


# ---------------------------------------------------------------------------
# 补位预览 / 确认
# ---------------------------------------------------------------------------

@bp.route("/preview", methods=["GET"])
def preview_latest():
    """一键生成新预览（GET 便于导航栏直接进入），PRG 跳到固定 token 地址。"""
    db = get_db()
    token, _ = create_preview(db)
    return redirect(url_for("main.preview_show", token=token))


@bp.post("/previews")
def preview_create():
    db = get_db()
    token, _ = create_preview(db)
    return redirect(url_for("main.preview_show", token=token))


@bp.get("/previews/<token>")
def preview_show(token: str):
    db = get_db()
    view = assess_preview(db, token)
    return render_template("preview.html", view=view,
                           msg=request.args.get("msg"))


@bp.post("/previews/<token>/confirm")
def preview_confirm(token: str):
    db = get_db()
    result = confirm_preview(db, token)
    if result["status"] == "stale":
        view = assess_preview(db, token)
        return render_template(
            "preview.html", view=view,
            confirm_error=result["message"],
            blockers=result.get("blockers"),
            validation_errors=result.get("validation_errors"),
        ), 409
    if result["status"] == "not_confirmable":
        view = assess_preview(db, token)
        return render_template("preview.html", view=view,
                               confirm_error=result["reason"]), 409
    return redirect(url_for("main.preview_show", token=token,
                            confirmed="1"))


# ---------------------------------------------------------------------------
# 审计事件
# ---------------------------------------------------------------------------

@bp.get("/events")
def events_page():
    db = get_db()
    rows = db.execute(
        "SELECT * FROM events ORDER BY id DESC LIMIT 200").fetchall()
    events = []
    for r in rows:
        try:
            detail = json.loads(r["detail_json"])
        except json.JSONDecodeError:
            detail = {}
        events.append({"row": r, "detail": detail})
    return render_template("events.html", events=events)


# ---------------------------------------------------------------------------
# JSON 载入（网页上传 / 粘贴）
# ---------------------------------------------------------------------------

@bp.get("/admin/load")
def admin_load_page():
    return render_template("admin_load.html", form_error=None, summary=None)


@bp.post("/admin/load")
def admin_load_submit():
    db = get_db()
    replace = request.form.get("mode", "replace") != "merge"
    payload = None
    try:
        if request.form.get("use_sample"):
            from pathlib import Path
            sample = Path(__file__).resolve().parent.parent / "data" / "sample_data.json"
            payload = json.loads(sample.read_text(encoding="utf-8"))
        elif request.files.get("json_file") and request.files["json_file"].filename:
            raw = request.files["json_file"].read().decode("utf-8-sig")
            payload = json.loads(raw)
        else:
            text = request.form.get("json_text", "").strip()
            if not text:
                raise LoaderError("请上传 JSON 文件或粘贴 JSON 内容")
            payload = json.loads(text)
        summary = load_payload(db, payload, replace=replace, source="web-upload")
    except LoaderError as err:
        return render_template("admin_load.html", form_error=err.errors,
                               summary=None), 400
    except (json.JSONDecodeError, UnicodeDecodeError) as err:
        return render_template(
            "admin_load.html",
            form_error=[f"JSON 解析失败：{err}"], summary=None), 400

    return render_template("admin_load.html", form_error=None,
                           summary=summary)


# ===========================================================================
# JSON API
# ===========================================================================

api = Blueprint("api", __name__, url_prefix="/api")


@api.get("/state")
def api_state():
    db = get_db()
    data = overview(db)
    return jsonify({"ok": True, **{
        "works": [
            {k: v for k, v in w.items() if k not in ("active", "voided")}
            | {
                "active_assignments": [dict(a) for a in w["active"]],
                "voided_assignments": [dict(a) for a in w["voided"]],
            }
            for w in data["works"]
        ],
        "judges": data["judges"],
    }})


@api.post("/assignments/<int:assignment_id>/score")
def api_score(assignment_id: int):
    body = request.get_json(silent=True) or {}
    raw = body.get("score", request.form.get("score"))
    result = submit_score(get_db(), assignment_id, raw)
    return jsonify({"ok": True, **result})


@api.post("/recusals")
def api_recusal():
    body = request.get_json(silent=True) or request.form
    result = report_recusal(
        get_db(),
        body.get("judge_code") or body.get("judge"),
        body.get("work_code") or body.get("work"),
        body.get("reason", ""),
    )
    return jsonify({"ok": True, **result})


@api.post("/previews")
def api_preview_create():
    token, envelope = create_preview(get_db())
    return jsonify({"ok": True, "token": token,
                    "preview_url": url_for("main.preview_show", token=token),
                    **envelope})


@api.get("/previews/<token>")
def api_preview_get(token: str):
    view = assess_preview(get_db(), token)
    return jsonify({"ok": True, **view})


@api.post("/previews/<token>/confirm")
def api_preview_confirm(token: str):
    body = request.get_json(silent=True) or {}
    inject = body.get("_inject_failure_after")  # 自测故障注入钩子
    result = confirm_preview(get_db(), token, inject_failure_after=inject)
    status = 200 if result["status"] in ("confirmed", "already_confirmed") else 409
    return jsonify({"ok": status == 200, **result}), status


@api.post("/admin/load")
def api_admin_load():
    payload = request.get_json(silent=True)
    if payload is None:
        raise ServiceError("请求体必须是 JSON 数据对象", 400, "invalid_json")
    replace = request.args.get("replace", "1") != "0"
    try:
        summary = load_payload(get_db(), payload, replace=replace,
                               source="api")
    except LoaderError as err:
        return jsonify({"ok": False, "errors": err.errors}), 400
    return jsonify({"ok": True, "summary": summary})
