# 影展评委回避 · 补位调度系统

Flask + SQLite 的服务端渲染网页（另附一套同义 JSON API）。支持从 JSON 载入
至多 24 件作品的编号、评委接单上限、回避关系与原派单（可带初始分数），并附样例。

围绕题目规则实现：

- 每件作品必须由 **两名不同评委** 打分；
- 评委在各自的派单页交分，**每单只收一次**；
- 待评、已交分的有效派单都占用接单上限（已交分不会释放名额）；
- **补报回避立即作废**对应有效派单及其评分，两份有效评分未收齐时总分立即隐藏；
- 补位预览在回避关系与接单上限内全局补齐席位，**保留所有已交分派单**，
  并使 **被撤掉的旧待评派单数最少**（最小费用流求最优解，非贪心）；
- 预览/重算期间不加锁，评委照常交分；
- 确认时**整批事务**执行预览列出的撤单与新派，任何写入失败整体回滚，原有效派单保留；
- 若预览拟撤的旧单在预览后**收到新分**，确认返回 `409 stale`，必须重算并重新确认；
  其他派单交分不阻断确认；
- 补不齐时列出缺评委的作品，不提供确认动作，原有效派单照旧；
- 改派后旧单状态为 `voided`，对其交分（网页表单或 API）一律 **409 拒收**；
- 提供 `docker compose up --build` 启动方式。

---

## 1. Docker Compose 启动（推荐）

```bash
docker compose up --build
# 打开 http://localhost:8000
```

- 容器内 gunicorn 监听 `8000`，宿主机默认 `8000`；
  若要用宿主机 5000 端口：`HOST_PORT=5000 docker compose up --build`。
- SQLite 库位于命名卷 `festival-data` 的 `/data/festival.db`；
  首次启动（无作品数据）自动建表并载入 `data/sample_data.json`。
- `./data` 以只读方式挂载进容器，方便修改样例。

载入自己的 JSON（三选一）：

1. 网页 `http://localhost:8000/admin/load` 上传/粘贴（可替换或追加，可一键重载样例）；
2. 把文件放进 `./data`，然后
   ```bash
   docker compose exec web python -m app.cli load /app/data/你的文件.json --replace
   ```
3. 直接 `POST /api/admin/load?replace=1`，请求体为 JSON 数据对象。

---

## 2. 本地直接运行（不用 Docker）

需要 Python 3.11+：

```bash
python -m pip install -r requirements.txt

# 方式 A：开发服务器（空库自动载入 data/sample_data.json，默认 0.0.0.0:8000）
python -m app
# 或指定端口/库
DB_PATH=/tmp/festival.db python -m app.cli serve --port 8000

# 方式 B：gunicorn（与容器内同一条命令，单 worker 多线程）
DB_PATH=./festival.db gunicorn -w 1 --threads 8 -b 127.0.0.1:8000 wsgi:application

# 仅初始化 / 载入 JSON
python -m app.cli init --db /tmp/f.db
python -m app.cli load data/sample_data.json --db /tmp/f.db --replace
```

运行自动化自测：

```bash
python -m pip install pytest
python -m pytest
```

起一个真实 HTTP 服务后跑端到端试跑脚本（仅用标准库，会通过 API 重载样例，可重复跑）：

```bash
python scripts/http_trial.py http://127.0.0.1:8000
```

---

## 3. JSON 输入格式

最简结构（键名同时兼容 `judge/work/capacity` 与中文别名
`评委/作品/接单上限/回避/原派单`，评委表也支持字典写法）：

```json
{
  "works": [
    {"code": "W01", "title": "山城夏梦"}
  ],
  "judges": [
    {"code": "J01", "name": "林岚", "capacity": 3}
  ],
  "recusals": [
    {"judge": "J03", "work": "W03", "reason": "同事关系"}
  ],
  "assignments": [
    {"work": "W01", "judge": "J01", "score": 88},
    {"work": "W01", "judge": "J02"}
  ]
}
```

校验规则（不合法会返回全部错误且不写库）：

- 作品 `1..24` 件，编号唯一；
- 评委编号唯一，`capacity` 为非负整数；
- 所有引用必须存在；原派单须满足：每作 ≤2 人且评委不同、
  不违反已知回避、评委派单数不超过上限；
- `score` 可省略（待评），给出时须为 `0~100` 的数字（系统把分数域定为 0–100）。

附带两份数据：

- `data/sample_data.json`：8 作品 / 6 评委（各上限 3）/ 3 条已知回避 / 16 张原派单，
  其中 8 个分数已交。**推荐演示剧本**：对 `J01 -> W07` 补报回避
  （该单已有 70 分），最优预览为“撤 1 张 W08 的待评单 + 新增 2 张派单”的连锁补位。
- `examples/infeasible_demo.json`：补不齐示例。对 `J03 -> Y02` 补报回避后，
  `Y02` 会出现在缺评委列表中，预览不可确认。

---

## 4. 页面与接口

| 页面 | 说明 |
| --- | --- |
| `GET /` | 作品卡片：有效派单/分数、仅两份有效分时显示总分（及均分）、评委容量条 |
| `GET /works/<code>`（别名 `/work/<code>`） | 作品派单全记录（含作废单/原因/时间）、该作回避 |
| `GET /judges` | 评委列表与有效派单/上限 |
| `GET /judges/<code>`（别名 `/judge/<code>`） | **评委派单页**，待评单内联交分；已交分只读；作废单置灰 |
| `POST /assignments/<id>/score`（另有单数别名 `/assignment/<id>/score`） | 表单交分；成功 302，重复/作废/非法返回 4xx 页面 |
| `GET/POST /recusals` | 补报回避；POST 后立即作废对应派单与评分，并跳转新预览 |
| `GET /preview` | 即时生成一份新预览并 302 到 `/previews/<token>` |
| `POST /previews` | 同上（按钮用） |
| `GET /previews/<token>` | 预览：拟撤单、新派单、保留单、缺评委作品、实时可确认性 |
| `POST /previews/<token>/confirm` | 整批确认；陈旧返回 409 并重渲染提示重算 |
| `GET /events` | 交分/回避/作废/确认/载入审计 |
| `GET/POST /admin/load` | 上传/粘贴 JSON、替换或追加、重载样例 |

JSON API（语义与网页一致）：

```text
GET  /api/state
POST /api/assignments/<id>/score      {"score": 90}
POST /api/recusals                    {"judge_code":"J01","work_code":"W07"}
POST /api/previews
GET  /api/previews/<token>
POST /api/previews/<token>/confirm
POST /api/admin/load?replace=1        # 请求体即领域 JSON
```

错误统一形如 `{"ok": false, "code": "already_scored|assignment_voided|...", "error": "..."}`，
HTTP 状态码：`400` 输入问题、`404` 不存在、`409` 重复交分/旧单拒收/预览陈旧、`500` 写入失败回滚。

---

## 5. 关键设计

### 5.1 “撤单最少”为什么是全局最小费用流

补报回避作废一张派单后，问题不是简单地找个替补：若所有可替补评委都满额，
可能需要撤掉该评委在其他作品上的某张**待评**单来腾名额，产生连锁改派。
因此预览在全部作品上求全局最优。

把所有**已交分**派单当作必须保留的固定项（扣减作品剩余席位需求与评委剩余容量），
剩余网络为：

- 源点 → 评委，容量 = `capacity - 该评委已交分数`；
- 评委 → 作品，容量 1（保证同作评委不同），
  旧待评单对应边费用 0，新组合边费用 1；
- 作品 → 汇点，容量 = `2 - 该作已交分数`。

设剩余席位总数为 `D`、旧待评单总数 `P`、最终保留 `K`、撤掉 `R=P-K`、
新派 `N`。完整补齐时 `D=K+N`，故 `R=P-D+N`；`P、D` 在一次快照中为常数，
最小化费用 `N` 即等价于最小化撤单数 `R`。用 SPFA 求最小费用最大流
（规模 ≤24 作、48 席，非常快）。最大流不足 `D` 时判定补不齐，
并给出一个最大填充方案中仍未满的作品作为“缺评委作品”。

`tests/test_solver.py` 用 460 个随机小例与**暴力枚举所有最终评委组合**对比，
撤单数全部等于全局最优值。

### 5.2 并发与原子性

- 每个 SQLite 连接开启 WAL、`busy_timeout=15s`、外键约束；
- 连接为 autocommit 模式，所有写事务显式 `BEGIN IMMEDIATE`，
  交分用条件更新
  `UPDATE ... WHERE id=? AND status='active' AND score IS NULL`
  保证“每单一次”，8 线程并发交同一单的测试中恰好 1 次成功；
- 确认时在一个 `BEGIN IMMEDIATE` 事务内：重新读取预览 →
  检查每张拟撤单（若已带新分则把预览标记 `stale` 并提交该状态、返回 409）→
  模拟最终状态并完整校验“每作 2 名不同评委 / 不回避 / 不超上限” →
  逐条条件撤单 + 插入新派单 + 写审计 + 预览置 `confirmed` → 提交；
  任一步异常整体 `ROLLBACK`，原有效派单不变（含故障注入回滚测试）；
- 预览生成只是一次普通插入，不持有事务/锁，所以“打开预览或重算期间，
  评委照常交分”；评委交分也不会被预览阻塞。

---

## 6. 交付前自测结果（本环境实际执行）

运行环境：Python 3.11.2，Flask 3.1.3，gunicorn 27.0.1，pytest 9.2.0
（依赖装在临时 venv 中；仓库代码未使用任何第三方算法/数据库扩展）。

### 6.1 自动化测试：`python -m pytest` → **17 passed**

```text
tests/test_flows.py
  test_index_and_pages_render PASSED
  test_total_hidden_after_recusal_voids_scored_assignment PASSED
  test_score_accepted_once_then_rejected PASSED
  test_html_form_duplicate_score_returns_409 PASSED
  test_score_to_voided_assignment_rejected PASSED
  test_preview_target_scored_requires_recompute_and_old_orders_rejected PASSED
  test_other_scoring_without_removal_does_not_block_confirm PASSED
  test_preview_page_get_shows_live_warning_when_target_scored PASSED
  test_html_confirm_stale_then_recompute_and_confirmation_page PASSED
  test_infeasible_lists_short_works_and_keeps_others PASSED
  test_confirm_write_failure_rolls_back_batch PASSED
  test_failure_mid_adds_still_rolls_back_removes PASSED
  test_concurrent_scoring_has_single_winner PASSED
  test_loader_rejects_more_than_24_works PASSED
tests/test_solver.py
  test_solver_matches_bruteforce_random PASSED   # 460 随机例对拍暴力最优
  test_full_state_needs_no_change PASSED
  test_sample_cascade_min_one_removal PASSED
```

### 6.2 真实 HTTP 试跑：`scripts/http_trial.py` → **23 项检查全部 PASS**

分别对真实 gunicorn（与 Docker 同一条启动命令，`-w 1 --threads 8`，
试跑于 `:8000`）和 Flask dev server（`:8766`，验证空库自动载入样例）执行，
结果一致。关键实录（样例 ID 在重复载入后会变，脚本动态读取）：

```text
[PASS] 补报回避 200
[PASS] 对应已评分派单被立即作废        # J01->W07, voided_score=70
[PASS] 预览给出最少撤单（样例中为 1 张）
       预览1 拟撤派单 #15 (J02->W08)
[PASS] 预览期间拟撤单仍可交分，表单返回 302   # 给 #15 交 76 分
[PASS] 旧预览确认被拒 409
[PASS] 拒绝原因为拟撤单已收到新分      # blockers[0].type == "scored"
[PASS] 重新计算成功
[PASS] 重算后刚交分的单不再被撤        # #15(76) 变为保留的固定单
       预览2 拟撤派单 #16
[PASS] 给其他保留单交分成功            # 其他交分不阻断
[PASS] 重算后的预览确认成功 200
[PASS] 确认执行的正是预览列出的撤单
[PASS] 旧单 HTML 表单交分返回 409
[PASS] HTML 明确提示旧单分数被拒收
[PASS] 旧单 API 交分返回 409 assignment_voided
[PASS] 回避作废单交分也被拒收
[PASS] 同一单第二次交分被 409 拒绝
[PASS] 最终每件作品恰有 2 名不同评委
       W07 有效派单: [('J05', None), ('J04', None)]
       W08 有效派单: [('J02', 76.0), ('J01', None)]
== 全部 23 项 HTTP 试跑检查通过 ==
```

另外实际核对过：

- 全部 11 个页面/别名路由 + `/api/state` + `/healthz` 实测 200；
  不存在的预览/评委返回 404 页面；补不齐示例的 HTML 预览显示
  “无法补齐全部席位 / Y02”，其确认接口返回 `409 not_confirmable`；
- `entrypoint.sh` 通过 `sh -n` 语法检查，并用临时 `DB_PATH` 实跑：
  首次正确建库+载入 8 作品样例，第二次正确跳过 seed；
- `compose.yml` 通过 YAML 解析，服务/端口/卷/构建上下文齐全；
  所有 JSON 样例均可解析；`python -m compileall app tests scripts` 通过。

### 6.3 手工复现这两个规定动作（UI）

1. `docker compose up --build` → <http://localhost:8000>；
2. 打开“补报回避”，选 `J01` / `W07` 并确认（提示 #13 单 70 分已作废）；
3. 跳到“补位预览”，记下“拟撤旧待评派单”的派单号（样例为 W08/J02 那张）；
4. 新标签打开“评委派单” → 对应评委页，给该单提交分数（预览不锁单，可提交）；
5. 回预览页点“确认执行”：出现红色“确认被拒绝…已收到新分，请重新计算”，
   且确认按钮被禁用/需重算；
6. 点“重新计算（生成新预览）”：刚交分的单变为保留，新预览列出另一张拟撤单，
   可顺手给任意一张“保留的待评派单”交分，再确认 → 成功；
7. 到被撤单评委的派单页：该单已置灰为“已作废 · 改派”；
   用 curl 直接打旧单接口可见拒收：
   ```bash
   curl -i -X POST http://localhost:8000/assignments/<旧单ID>/score \
        -d 'score=50'
   # HTTP/1.1 409 CONFLICT，页面含“旧单分数被拒收”
   ```

---

## 7. 目录结构

```text
.
├── app/
│   ├── __init__.py        # Flask 工厂、错误处理、模板过滤器
│   ├── __main__.py        # python -m app
│   ├── cli.py             # init / load / serve
│   ├── db.py              # 连接、PRAGMA、schema 初始化
│   ├── schema.sql         # works/judges/recusals/assignments/previews/events
│   ├── solver.py          # 最小费用最大流补位规划（纯逻辑、可单测）
│   ├── loader.py          # JSON 解析/校验/原子载入
│   ├── service.py         # 交分/回避/预览/确认/总览（事务与并发）
│   ├── routes.py          # 网页路由 + /api JSON 接口
│   ├── templates/         # 9 个服务端渲染模板
│   └── static/style.css
├── data/sample_data.json
├── examples/infeasible_demo.json
├── scripts/http_trial.py  # 纯标准库真实 HTTP 端到端试跑
├── tests/                 # 17 个 pytest 用例（含暴力对拍/并发/故障注入）
├── Dockerfile
├── compose.yml
├── entrypoint.sh
├── wsgi.py
├── requirements.txt
└── pytest.ini
```

---

## 8. 缺项与限制（如实说明）

1. **本交付环境没有安装/无法运行 Docker**（`docker: command not found`），
   因此 `docker compose up --build` 的镜像构建与 compose 实际起容器
   **未能在本环境实跑**；已做的替代验证是：校验 compose YAML/Dockerfile 引用、
   在本机用 venv 实际执行 `entrypoint.sh` 的 seed/跳过分支、
   并用 Dockerfile 中的同一条 gunicorn 命令真实起服务通过全部 HTTP 试跑。
   镜像基于官方 `python:3.11-slim`，仅 `pip install -r requirements.txt`，
   无特殊系统依赖，预期可直接构建。
2. **没有做登录/鉴权与多角色权限**：题目未要求；知道评委编号即可打开其派单页，
   属于内网组委会工具定位。若上线需加认证与 CSRF 防护。
3. 分数域按常见评审场景固定为 **0–100、最多两位小数**；题目未规定量纲，
   如需 1–5/1–10 分，改 `service.parse_score` 的边界即可。
4. 部署形态按 **单 gunicorn worker + 多线程**设计（SQLite 单写者模型）；
   不建议多 worker/多实例同时写同一个 SQLite 文件。需要横向扩展应换
   PostgreSQL（SQL 与事务写法可平滑迁移）。
5. `previews/events` 会持续累积（量很小），未做定期清理；预览 token 不过期，
   但确认时总会按当前数据库重新校验，陈旧计划不可能被错误执行。
6. “缺评委作品”的列表来自一个最大流最优填充方案下未满足的作品集合；
   在极端全局容量竞争下，具体哪几件被列为缺口可能随等价最优解略有差异，
   但“是否完全可补齐”的判断是精确的。
7. 未提供浏览器 JS 自动化截图；所有 UI 路径均通过 Flask test client
   与真实 HTTP 请求验证状态码和关键文案。
