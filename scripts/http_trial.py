#!/usr/bin/env python3
"""真实 HTTP 试跑脚本（仅用标准库）。

验证题目要求的两个关键场景：
  A. 预览打开期间，给“预览拟撤的旧待评单”交分 -> 确认被 409 拒绝 ->
     重算 -> 重新确认成功；期间给其他保留单交分不妨碍确认。
  B. 改派确认后，向被撤的旧单提交分数 -> 被 409 拒收且分数不入库。

用法：
  python scripts/http_trial.py [BASE_URL]
默认 BASE_URL=http://127.0.0.1:8000
脚本会通过 /api/admin/load 重新载入内置样例，因此可重复执行。
"""
from __future__ import annotations

import json
import sys
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

BASE = sys.argv[1] if len(sys.argv) > 1 else "http://127.0.0.1:8000"
ROOT = Path(__file__).resolve().parent.parent
SAMPLE = ROOT / "data" / "sample_data.json"

failures: list[str] = []
checks = 0


class NoRedirect(urllib.request.HTTPRedirectHandler):
    """把 302/303 当作普通返回，便于断言“表单交分成功 -> 302”。"""

    def redirect_request(self, *args, **kwargs):  # noqa: ANN002, ANN003
        return None


opener = urllib.request.build_opener(NoRedirect)


def call(method: str, path: str, body=None, form=None):
    headers: dict[str, str] = {}
    data = None
    if body is not None:
        data = json.dumps(body).encode("utf-8")
        headers["Content-Type"] = "application/json"
    elif form is not None:
        data = urllib.parse.urlencode(form).encode("utf-8")
        headers["Content-Type"] = "application/x-www-form-urlencoded"
    req = urllib.request.Request(BASE + path, data=data,
                                 headers=headers, method=method)
    try:
        resp = opener.open(req, timeout=10)
        raw = resp.read().decode("utf-8", "replace")
        return resp.status, dict(resp.headers), raw
    except urllib.error.HTTPError as exc:
        raw = exc.read().decode("utf-8", "replace")
        return exc.code, dict(exc.headers), raw


def call_json(method: str, path: str, body=None):
    status, headers, raw = call(method, path, body=body)
    try:
        return status, headers, json.loads(raw)
    except json.JSONDecodeError:
        raise AssertionError(f"{method} {path} -> {status} 非 JSON: {raw[:300]}")


def check(name: str, cond: bool, detail: str = ""):
    global checks
    checks += 1
    mark = "PASS" if cond else "FAIL"
    print(f"  [{mark}] {name}" + (f"  -- {detail}" if detail and not cond else ""))
    if not cond:
        failures.append(name + (f" ({detail})" if detail else ""))


def wait_ready(timeout_s: float = 20.0):
    import time
    deadline = time.time() + timeout_s
    last = None
    while time.time() < deadline:
        try:
            status, _, data = call_json("GET", "/healthz")
            if status == 200 and data.get("ok"):
                return
        except Exception as exc:  # noqa: BLE001
            last = exc
        time.sleep(0.4)
    raise SystemExit(f"服务未就绪: {BASE} ({last})")


def main() -> int:
    print(f"== HTTP 试跑目标 {BASE} ==")
    wait_ready()

    # 0. 重新载入样例，保证可重复
    sample = json.loads(SAMPLE.read_text(encoding="utf-8"))
    st, _, js = call_json("POST", "/api/admin/load?replace=1", sample)
    check("重新载入样例 200", st == 200 and js.get("ok"), f"{st} {js}")

    # 1. 主要页面可打开
    st, _, html = call("GET", "/")
    check("总览页 200 且含作品", st == 200 and "山城夏梦" in html, str(st))
    st, _, html = call("GET", "/judges/J01")
    check("J01 派单页 200", st == 200 and "派单页" in html, str(st))
    st, _, _ = call("GET", "/recusals")
    check("回避页 200", st == 200, str(st))

    # 2. 补报回避 J01 -> W07（该单已有 70 分），派单与评分立即作废
    st, _, js = call_json("POST", "/api/recusals",
                          {"judge_code": "J01", "work_code": "W07",
                           "reason": "HTTP 试跑：新发现的师生关系"})
    check("补报回避 200", st == 200 and js.get("ok"), f"{st} {js}")
    void_id = js["voided"]["assignment_id"]
    check("对应已评分派单被立即作废",
          js["voided"]["had_score"] is True
          and js["voided"]["voided_score"] == 70,
          json.dumps(js.get("voided"), ensure_ascii=False))

    # 3. 生成预览：最优为撤 1 张待评旧单 + 2 张新派单
    st, _, js = call_json("POST", "/api/previews")
    check("生成预览 200 且可补齐",
          st == 200 and js["plan"]["complete"] is True, str(st))
    token1 = js["token"]
    remove1 = js["plan"]["remove"]
    check("预览给出最少撤单（样例中为 1 张）",
          len(remove1) == 1, json.dumps(remove1, ensure_ascii=False))
    rid1 = remove1[0]["assignment_id"]
    print(f"      预览1 token={token1[:8]}… 拟撤派单 #{rid1} "
          f"({remove1[0]['judge_code']}->{remove1[0]['work_code']})")

    st, _, html = call("GET", f"/previews/{token1}")
    check("预览页 HTML 200", st == 200 and "补位预览" in html, str(st))

    # 4. 关键场景 A-1：预览打开期间，评委给“拟撤旧单”交分（浏览器表单）
    st, hdrs, _ = call("POST", f"/assignments/{rid1}/score",
                       form={"score": "76"})
    check("预览期间拟撤单仍可交分，表单返回 302",
          st == 302 and "Location" in hdrs, f"status={st}")

    # 5. 用旧预览确认 -> 必须 409 stale / scored
    st, _, js = call_json("POST", f"/api/previews/{token1}/confirm", {})
    check("旧预览确认被拒 409", st == 409, f"status={st}")
    check("拒绝原因为拟撤单已收到新分",
          js.get("status") == "stale"
          and js["blockers"][0]["type"] == "scored",
          json.dumps(js, ensure_ascii=False)[:400])

    # 6. 重算：刚交分的单必须保留，不再出现在撤单列表
    st, _, js = call_json("POST", "/api/previews")
    check("重新计算成功", st == 200 and js["plan"]["complete"], str(st))
    token2 = js["token"]
    remove2 = js["plan"]["remove"]
    rid2 = remove2[0]["assignment_id"]
    remove_ids2 = {r["assignment_id"] for r in remove2}
    check("重算后刚交分的单不再被撤", rid1 not in remove_ids2,
          f"remove={remove_ids2}")
    print(f"      预览2 token={token2[:8]}… 拟撤派单 #{rid2}")

    # 7. 其他派单交分不阻碍确认：给预览2保留的一张待评单交分
    keep_id = js["plan"]["kept_pending"][0]
    st, _, js_score = call_json(
        "POST", f"/api/assignments/{keep_id}/score", {"score": 81})
    check("给其他保留单交分成功", st == 200 and js_score.get("ok"),
          f"{st} {js_score}")

    # 8. 确认预览2 -> 整批执行
    st, _, js = call_json("POST", f"/api/previews/{token2}/confirm", {})
    check("重算后的预览确认成功 200",
          st == 200 and js.get("status") == "confirmed", f"{st} {js}")
    applied_removed = js["result"]["removed_assignment_ids"]
    check("确认执行的正是预览列出的撤单",
          applied_removed == [rid2], str(applied_removed))

    # 9. 关键场景 B：改派后向旧单提交分数 -> 拒收
    st, _, html = call("POST", f"/assignments/{rid2}/score",
                       form={"score": "55"})
    check("旧单 HTML 表单交分返回 409", st == 409, f"status={st}")
    check("HTML 明确提示旧单分数被拒收", "拒收" in html, "")

    st, _, js = call_json("POST", f"/api/assignments/{rid2}/score",
                          {"score": 54})
    check("旧单 API 交分返回 409 assignment_voided",
          st == 409 and js.get("code") == "assignment_voided",
          f"{st} {js}")

    # 补报回避时作废的那张旧评分单同样拒收
    st, _, js = call_json("POST", f"/api/assignments/{void_id}/score",
                          {"score": 1})
    check("回避作废单交分也被拒收",
          st == 409 and js.get("code") == "assignment_voided", f"{st}")

    # 10. 每单只收一次：keep_id 再交被拒
    st, _, js = call_json("POST", f"/api/assignments/{keep_id}/score",
                          {"score": 12})
    check("同一单第二次交分被 409 拒绝",
          st == 409 and js.get("code") == "already_scored", f"{st}")

    # 11. 最终状态核对：每件作品 2 张有效派单、评委不同
    st, _, state = call_json("GET", "/api/state")
    ok_shape = True
    detail_bad = []
    for w in state["works"]:
        act = w["active_assignments"]
        judges = [a["judge_code"] for a in act]
        if len(act) != 2 or len(set(judges)) != 2:
            ok_shape = False
            detail_bad.append(f"{w['code']}:{judges}")
    check("最终每件作品恰有 2 名不同评委", ok_shape, ",".join(detail_bad))

    w07 = next(w for w in state["works"] if w["code"] == "W07")
    print("      W07 有效派单:",
          [(a["judge_code"], a["score"]) for a in w07["active_assignments"]])
    w08 = next(w for w in state["works"] if w["code"] == "W08")
    print("      W08 有效派单:",
          [(a["judge_code"], a["score"]) for a in w08["active_assignments"]])

    print()
    if failures:
        print(f"== {checks} 项检查，{len(failures)} 项失败 ==")
        for f in failures:
            print(" -", f)
        return 1
    print(f"== 全部 {checks} 项 HTTP 试跑检查通过 ==")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
