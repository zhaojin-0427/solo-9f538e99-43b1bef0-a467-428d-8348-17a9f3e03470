"""端到端业务流测试（Flask test client + 真实 SQLite 文件库）。

重点覆盖题目要求：
* 每单只收一次分；已交分有效派单占容量；
* 补报回避立即作废派单与评分、总分立即隐藏；
* 预览期间可照常交分；拟撤单收到新分 -> 409 stale -> 重算 -> 再确认；
* 其他派单交分不妨碍确认；
* 改派后旧单分数被拒收（JSON 409 + HTML 409）；
* 补不齐列出缺评委作品且不能确认；
* 写入故障整批回滚；
* 并发交分只有一次成功。
"""
from __future__ import annotations

import json
import threading

import pytest

from app.db import connect
from app.service import (
    ServiceError, confirm_preview, create_preview, report_recusal,
    submit_score,
)


def jpost(client, url, body=None):
    return client.post(url, data=json.dumps(body or {}),
                       content_type="application/json")


def assignment_id(db, judge, work, status="active"):
    row = db.execute(
        "SELECT id FROM assignments WHERE judge_code=? AND work_code=? "
        "AND status=? ORDER BY id DESC",
        (judge, work, status)).fetchone()
    assert row is not None, f"assignment {judge}->{work} ({status}) not found"
    return row["id"]


# ---------------------------------------------------------------------------
# 页面与总分
# ---------------------------------------------------------------------------

def test_index_and_pages_render(client):
    resp = client.get("/")
    assert resp.status_code == 200
    text = resp.get_data(as_text=True)
    assert "W01" in text and "山城夏梦" in text
    # W01 两份评分 88 + 91 = 179，应显示总分
    assert "179" in text
    # W02 只有一份有效评分，应提示暂不显示总分
    assert "暂不显示总分" in text

    assert client.get("/judges").status_code == 200
    assert client.get("/judges/J01").status_code == 200
    assert client.get("/judge/J01").status_code == 200  # 别名
    assert client.get("/recusals").status_code == 200
    assert client.get("/events").status_code == 200
    assert client.get("/admin/load").status_code == 200
    assert client.get("/healthz").get_json()["ok"] is True


def test_total_hidden_after_recusal_voids_scored_assignment(client, db):
    # W01 原本有两份分，总分可见（API 校验）
    state = client.get("/api/state").get_json()
    w01 = next(w for w in state["works"] if w["code"] == "W01")
    assert w01["total_score"] == 179
    assert w01["total_ready"] is True

    aid = assignment_id(db, "J01", "W01")
    r = jpost(client, "/api/recusals",
              {"judge_code": "J01", "work_code": "W01", "reason": "test"})
    assert r.status_code == 200
    assert r.get_json()["voided"]["had_score"] is True

    row = db.execute("SELECT status, score, void_reason FROM assignments "
                     "WHERE id=?", (aid,)).fetchone()
    assert row["status"] == "voided"
    assert row["void_reason"] == "recusal"
    # 分数值保留用于审计，但不再是有效评分
    assert row["score"] == 88

    state = client.get("/api/state").get_json()
    w01 = next(w for w in state["works"] if w["code"] == "W01")
    assert w01["total_ready"] is False
    assert w01["total_score"] is None
    assert w01["scored_count"] == 1


# ---------------------------------------------------------------------------
# 交分：一次 / 校验 / 作废拒收
# ---------------------------------------------------------------------------

def test_score_accepted_once_then_rejected(client, db):
    aid = assignment_id(db, "J03", "W02")  # 待评
    r1 = jpost(client, f"/api/assignments/{aid}/score", {"score": 90})
    assert r1.status_code == 200 and r1.get_json()["ok"]

    r2 = jpost(client, f"/api/assignments/{aid}/score", {"score": 95})
    assert r2.status_code == 409
    assert r2.get_json()["code"] == "already_scored"

    row = db.execute("SELECT score, submitted_at FROM assignments WHERE id=?",
                     (aid,)).fetchone()
    assert row["score"] == 90 and row["submitted_at"]

    # 非法分数
    r3 = jpost(client, f"/api/assignments/{aid}/score", {"score": 999})
    assert r3.status_code == 400
    r4 = jpost(client, f"/api/assignments/{aid}/score", {"score": "abc"})
    assert r4.status_code == 400


def test_html_form_duplicate_score_returns_409(client, db):
    aid = assignment_id(db, "J04", "W03")  # 已有分 78
    resp = client.post(f"/assignments/{aid}/score", data={"score": "80"})
    assert resp.status_code == 409
    assert "每单只收一次" in resp.get_data(as_text=True)


def test_score_to_voided_assignment_rejected(client, db):
    jpost(client, "/api/recusals", {"judge_code": "J06", "work_code": "W04"})
    aid = assignment_id(db, "J06", "W04", status="voided")

    r = jpost(client, f"/api/assignments/{aid}/score", {"score": 66})
    assert r.status_code == 409
    assert r.get_json()["code"] == "assignment_voided"

    # HTML 表单同样返回 409 且页面明确“拒收”
    h = client.post(f"/assignments/{aid}/score", data={"score": "67"})
    assert h.status_code == 409
    assert "拒收" in h.get_data(as_text=True)

    row = db.execute("SELECT score FROM assignments WHERE id=?",
                     (aid,)).fetchone()
    assert row["score"] is None  # 拒收，未写入


# ---------------------------------------------------------------------------
# 核心：预览中交分 -> 拟撤单新分 -> stale -> 重算 -> 再确认 -> 旧单拒收
# ---------------------------------------------------------------------------

def test_preview_target_scored_requires_recompute_and_old_orders_rejected(
        client, db):
    # 触发精心设计的 cascade 场景：J01->W07（已评分 70）补报回避
    rep = jpost(client, "/api/recusals",
                {"judge_code": "J01", "work_code": "W07"})
    assert rep.status_code == 200

    p1 = jpost(client, "/api/previews").get_json()
    assert p1["plan"]["complete"] is True
    assert len(p1["plan"]["remove"]) == 1  # 最少撤 1 张待评旧单
    old_target = p1["plan"]["remove"][0]["assignment_id"]

    # 预览打开期间，评委给“预览要撤的单”交了分 —— 交分本身成功
    s = client.post(f"/assignments/{old_target}/score",
                    data={"score": "76"})
    assert s.status_code == 302  # 表单交分成功后重定向

    # 用旧预览确认 -> 409 stale，且 blocker 明确是 scored
    c1 = jpost(client, f"/api/previews/{p1['token']}/confirm")
    assert c1.status_code == 409
    body = c1.get_json()
    assert body["status"] == "stale"
    assert body["blockers"][0]["type"] == "scored"

    # 旧预览页面也应展示失效状态
    page = client.get(f"/previews/{p1['token']}")
    assert page.status_code == 200
    assert "重新计算" in page.get_data(as_text=True)

    # 重算：刚交分的单变成必须保留的固定单，不会再出现在 remove 中
    p2 = jpost(client, "/api/previews").get_json()
    assert p2["plan"]["complete"] is True
    removed_ids_p2 = {r["assignment_id"] for r in p2["plan"]["remove"]}
    assert old_target not in removed_ids_p2
    assert len(p2["plan"]["remove"]) == 1
    rid2 = p2["plan"]["remove"][0]["assignment_id"]
    assert rid2 != old_target

    # “其他交分不妨碍确认”：给新预览保留的一张待评单交分
    keep_id = p2["plan"]["kept_pending"][0]
    ok_score = jpost(client, f"/api/assignments/{keep_id}/score",
                     {"score": 81})
    assert ok_score.status_code == 200

    # 确认新预览 -> 成功
    c2 = jpost(client, f"/api/previews/{p2['token']}/confirm")
    assert c2.status_code == 200
    result = c2.get_json()["result"]
    assert set(result["removed_assignment_ids"]) == {rid2}
    assert len(result["added"]) == 2

    # 刚交分的 old_target 被保留且分数还在
    row = db.execute("SELECT status, score FROM assignments WHERE id=?",
                     (old_target,)).fetchone()
    assert row["status"] == "active" and row["score"] == 76

    # 被撤的旧单已作废；无论 API 还是 HTML，交分一律 409 拒收
    row = db.execute("SELECT status, void_reason FROM assignments WHERE id=?",
                     (rid2,)).fetchone()
    assert row["status"] == "voided"
    assert row["void_reason"] == "replacement"

    api_old = jpost(client, f"/api/assignments/{rid2}/score", {"score": 55})
    assert api_old.status_code == 409
    assert api_old.get_json()["code"] == "assignment_voided"

    html_old = client.post(f"/assignments/{rid2}/score",
                           data={"score": "56"})
    assert html_old.status_code == 409
    assert "拒收" in html_old.get_data(as_text=True)
    assert db.execute("SELECT score FROM assignments WHERE id=?",
                      (rid2,)).fetchone()["score"] is None

    # 最终状态：每件作品恰好 2 张有效派单、评委不同、容量不超
    state = client.get("/api/state").get_json()
    caps = {j["code"]: j["capacity"] for j in state["judges"]}
    usage: dict[str, int] = {}
    for w in state["works"]:
        act = w["active_assignments"]
        assert len(act) == 2, w["code"]
        judges = [a["judge_code"] for a in act]
        assert len(set(judges)) == 2, w["code"]
        for j in judges:
            usage[j] = usage.get(j, 0) + 1
    for j, n in usage.items():
        assert n <= caps[j]

    # 已交分的 keep_id 重复提交仍被拒
    again = jpost(client, f"/api/assignments/{keep_id}/score", {"score": 1})
    assert again.status_code == 409


def test_other_scoring_without_removal_does_not_block_confirm(client, db):
    # J06->W04 待评；回避后存在零撤单直接补位方案（J03 有名额）
    jpost(client, "/api/recusals",
          {"judge_code": "J06", "work_code": "W04"})
    p = jpost(client, "/api/previews").get_json()
    assert p["plan"]["complete"] is True
    assert p["plan"]["remove"] == []
    assert {"work_code": "W04", "judge_code": "J03"} in p["plan"]["add"]

    # 预览期间给一张与拟撤集合无关的待评单交分
    keep_id = p["plan"]["kept_pending"][0]
    r = jpost(client, f"/api/assignments/{keep_id}/score", {"score": 73})
    assert r.status_code == 200

    c = jpost(client, f"/api/previews/{p['token']}/confirm")
    assert c.status_code == 200
    assert c.get_json()["status"] == "confirmed"


def test_preview_page_get_shows_live_warning_when_target_scored(client):
    jpost(client, "/api/recusals",
          {"judge_code": "J01", "work_code": "W07"})
    p = jpost(client, "/api/previews").get_json()
    rid = p["plan"]["remove"][0]["assignment_id"]
    jpost(client, f"/api/assignments/{rid}/score", {"score": 60})

    page = client.get(f"/previews/{p['token']}")
    text = page.get_data(as_text=True)
    assert page.status_code == 200
    assert "已收到新分" in text
    # 不可直接确认
    assert "存在变化，请先重新计算" in text


# ---------------------------------------------------------------------------
# 补不齐
# ---------------------------------------------------------------------------

def test_infeasible_lists_short_works_and_keeps_others(infeasible_app):
    client = infeasible_app.test_client()
    db = connect(infeasible_app.config["DATABASE"])

    jpost(client, "/api/recusals",
          {"judge_code": "J03", "work_code": "Y02"})

    p = jpost(client, "/api/previews").get_json()
    plan = p["plan"]
    assert plan["complete"] is False
    assert "Y02" in [s["work_code"] for s in plan["short_works"]]

    # 网页明确列出缺评委作品且没有确认按钮
    html = client.get(f"/previews/{p['token']}").get_data(as_text=True)
    assert "无法补齐全部席位" in html and "Y02" in html

    # 确认被拒（409），不产生任何新派单
    before = db.execute(
        "SELECT COUNT(*) c FROM assignments WHERE status='active'").fetchone()["c"]
    c = jpost(client, f"/api/previews/{p['token']}/confirm")
    assert c.status_code == 409
    assert c.get_json()["status"] == "not_confirmable"
    after = db.execute(
        "SELECT COUNT(*) c FROM assignments WHERE status='active'").fetchone()["c"]
    assert before == after

    # 除补报回避立即作废的那张外，其余原有效派单照旧
    rows = db.execute(
        "SELECT work_code, judge_code, status FROM assignments "
        "ORDER BY work_code, judge_code").fetchall()
    active_pairs = {(r["work_code"], r["judge_code"]) for r in rows
                    if r["status"] == "active"}
    assert ("Y02", "J01") in active_pairs
    assert ("Y01", "J01") in active_pairs and ("Y01", "J02") in active_pairs
    assert len(active_pairs) == 5  # 原 6 张，仅 J03->Y02 被回避作废
    db.close()


# ---------------------------------------------------------------------------
# 原子性：写入失败整批回滚
# ---------------------------------------------------------------------------

def test_confirm_write_failure_rolls_back_batch(app, db):
    report_recusal(db, "J01", "W07")
    token, env = create_preview(db)
    plan = env["plan"]
    rid = plan["remove"][0]["assignment_id"]

    with pytest.raises(ServiceError) as ei:
        confirm_preview(db, token, inject_failure_after=1)
    assert ei.value.status == 500
    assert ei.value.code == "write_failed"

    # 第一张撤单 UPDATE 已随事务回滚 -> 原派单仍有效
    row = db.execute("SELECT status FROM assignments WHERE id=?",
                     (rid,)).fetchone()
    assert row["status"] == "active"
    # 新增派单也全部不存在
    n_new = db.execute(
        "SELECT COUNT(*) c FROM assignments WHERE status='active' "
        "AND id > 16").fetchone()["c"]
    assert n_new == 0
    # 预览仍是 open，可在故障排除后正常确认
    prow = db.execute("SELECT status FROM previews WHERE token=?",
                      (token,)).fetchone()
    assert prow["status"] == "open"

    result = confirm_preview(db, token)
    assert result["status"] == "confirmed"
    assert db.execute("SELECT status FROM assignments WHERE id=?",
                      (rid,)).fetchone()["status"] == "voided"


def test_failure_mid_adds_still_rolls_back_removes(app, db):
    report_recusal(db, "J01", "W07")
    token, _ = create_preview(db)
    # 计划：1 个 remove（第 1 次写）+ 2 个 add；在第 2 次写（首个 add）后炸
    with pytest.raises(ServiceError):
        confirm_preview(db, token, inject_failure_after=2)
    plan_row = db.execute("SELECT plan_json FROM previews WHERE token=?",
                          (token,)).fetchone()
    plan = json.loads(plan_row["plan_json"])["plan"]
    rid = plan["remove"][0]["assignment_id"]
    assert db.execute("SELECT status FROM assignments WHERE id=?",
                      (rid,)).fetchone()["status"] == "active"
    # 没有任何残留新派单
    assert db.execute(
        "SELECT COUNT(*) c FROM assignments WHERE void_reason IS NULL "
        "AND id > 16").fetchone()["c"] == 0


# ---------------------------------------------------------------------------
# 并发交分
# ---------------------------------------------------------------------------

def test_concurrent_scoring_has_single_winner(app):
    path = app.config["DATABASE"]
    setup = connect(path)
    aid = setup.execute(
        "SELECT id FROM assignments WHERE judge_code='J05' "
        "AND work_code='W03' AND status='active'").fetchone()["id"]
    setup.close()

    n_threads = 8
    barrier = threading.Barrier(n_threads)
    outcomes: list[tuple[str, str]] = []
    lock = threading.Lock()

    def worker():
        conn = connect(path)
        barrier.wait()
        try:
            submit_score(conn, aid, 88)
            with lock:
                outcomes.append(("ok", ""))
        except ServiceError as exc:
            with lock:
                outcomes.append(("err", exc.code))
        finally:
            conn.close()

    threads = [threading.Thread(target=worker) for _ in range(n_threads)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=20)

    assert sum(1 for kind, _ in outcomes if kind == "ok") == 1, outcomes
    assert all(code == "already_scored" for kind, code in outcomes
               if kind == "err"), outcomes

    check = connect(path)
    assert check.execute("SELECT score FROM assignments WHERE id=?",
                         (aid,)).fetchone()["score"] == 88
    check.close()


def test_html_confirm_stale_then_recompute_and_confirmation_page(client):
    # GET /preview 会即时生成预览并 302 到固定 token 页
    jpost(client, "/api/recusals",
          {"judge_code": "J01", "work_code": "W07"})
    resp = client.get("/preview")
    assert resp.status_code == 302
    location = resp.headers["Location"]
    token1 = location.rstrip("/").split("/")[-1]

    view = client.get(f"/api/previews/{token1}").get_json()
    rid = view["plan"]["remove"][0]["assignment_id"]
    jpost(client, f"/api/assignments/{rid}/score", {"score": 59})

    # 浏览器表单确认旧预览 -> 直接渲染 409 页面
    stale = client.post(f"/previews/{token1}/confirm")
    assert stale.status_code == 409
    text = stale.get_data(as_text=True)
    assert "确认被拒绝" in text and "重新计算" in text

    # 重算并用 HTML 表单确认 -> 302 到结果页
    p2 = jpost(client, "/api/previews").get_json()
    ok = client.post(f"/previews/{p2['token']}/confirm")
    assert ok.status_code == 302
    done = client.get(ok.headers["Location"])
    assert done.status_code == 200
    assert "已整批执行" in done.get_data(as_text=True)

    # 重复提交同一已确认 token：幂等返回（重定向到已执行页）
    again = client.post(f"/previews/{p2['token']}/confirm")
    assert again.status_code == 302


# ---------------------------------------------------------------------------
# JSON 载入校验
# ---------------------------------------------------------------------------

def test_loader_rejects_more_than_24_works(app):
    from app.loader import LoaderError, load_payload
    db = connect(app.config["DATABASE"])
    payload = {"works": [{"code": f"W{i}"} for i in range(25)],
               "judges": [{"code": "J1", "capacity": 100}],
               "assignments": []}
    with pytest.raises(LoaderError) as ei:
        load_payload(db, payload, replace=True)
    assert any("24" in e for e in ei.value.errors)
    # 载入失败不应破坏原有样例数据（事务回滚 / 校验先于写入）
    assert db.execute("SELECT COUNT(*) c FROM works").fetchone()["c"] == 8
    db.close()
