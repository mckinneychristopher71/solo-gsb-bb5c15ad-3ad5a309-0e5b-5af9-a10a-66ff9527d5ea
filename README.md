# 功能开关判定服务（FastAPI + SQLite）

一个支持**版本化配置**、**前置开关依赖**、**可复用受众分组**、**有序属性规则**和**基于 user_id 稳定哈希百分比分流**的功能开关判定服务。

核心特性：

- 📦 **不可变版本**：客户端上传带 `version` 的 JSON 配置，版本一经发布即不可修改；新版本不会影响历史版本的判定。
- 🔗 **前置开关**：开关可声明 `prerequisites`（含期望值 `expect`），按依赖深度优先、短路求值，响应返回完整**依赖链**。
- 👥 **可复用受众分组**：顶层 `segments` 声明命名受众（一组 AND 条件），分组可互相嵌套引用，开关规则用 `{"op":"segment","value":"分组名"}` 复用；发布时拒绝悬空引用与直接/间接循环，判定时递归求值并在同一请求内复用结果。
- 📋 **有序规则**：`rules` 按顺序匹配、首条命中即返回，支持 `eq/ne/in/nin/gt/ge/lt/le/prefix/suffix` 及分组引用 `segment`。
- 🎯 **稳定百分比分流**：`sha1(flag_key:user_id) % 100`，同一用户在同一开关上桶位恒定，放量单调扩大；不同开关独立分布。
- 🔀 **版本影响分析**：对比任意两个已发布版本，给出开关级 `added/removed/modified` 差异，并沿两版本前置依赖边的并集反向传播，列出每个变化开关间接影响的上游开关及一条最短路径。
- 🔬 **评估预览**：发布前用两个已发布版本对一批样本（唯一 `subject_id` + `user_id` + `attributes`）逐一计算“样本 × 开关”的判定差异；**存在性或 `value` 的 JSON 类型/值不同才算 changed**，仅 `reason`/`rule_id` 不同不算；changed 明细按请求排序并回显双方的 `exists`/`value`/`reason`/`rule_id`，另按开关汇总 changed/unchanged；每个“版本 × 样本”使用独立判定器，分组缓存绝不跨样本复用。
- 🌍 **环境指针 + 乐观并发**：`production`/`staging` 等环境是指向已发布版本的**指针**，带单调递增的 `revision`。创建只接受 `expected_revision=0`（初始 revision 为 1），更新须凭当前 revision 换号；同一 `BEGIN IMMEDIATE` 事务内完成校验、版本存在性确认与 revision 加一，竞争或过期写入得到 `409 stale_revision` 且指针不变。环境级判定单次请求使用同一指针快照，响应回显 `version` 与 `revision`。
- 📜 **修订审计（不可变历史）**：每次成功创建/移动环境指针，都会在**同一事务**内向 `environment_history` 追加一条不可变记录（环境名、revision、目标 version、操作类型 `create`/`update`、时间）；失败或过期写入回滚、不产生任何记录。`GET /api/v1/environments/{name}/history` 按 revision 升序返回审计轨迹，支持 `from_revision`/`limit` 前向翻页，环境不存在返回 404。
- 🗳️ **环境变更审批**：移动指针可以走审批流——创建审批单时记录环境、`target_version`、创建时的 `base_revision`、`applicant` 与 `required_approvals`（1～5）；目标版本必须已发布且与当前指针不同。不同审批人只能投票一次（申请人不得自批）；票数达到门槛后才允许执行。执行在**同一个 `BEGIN IMMEDIATE` 事务**内再次校验环境 revision：未变化则移动指针、revision 加一并向现有 `environment_history` **追加**审计记录；已变化则把审批单标记为 `stale`、指针与历史均不动。详情查询返回 `pending`/`executed`/`stale` 状态、票数与还差几票。
- 🚫 **发布时强校验**：循环依赖（含自依赖）、悬挂依赖、非法比例（非整数或越界）、未知操作符、重复规则 ID、悬空分组引用与分组循环一律拒绝。
- 🧪 **纯函数引擎**：判定引擎与校验器零第三方依赖，可直接用系统 Python 跑单测。

---

## 一条命令启动

```bash
docker compose up -d --build
```

服务端口：**http://localhost:8000**

- API 文档（Swagger）：http://localhost:8000/docs
- 健康检查：http://localhost:8000/healthz

数据持久化在 Docker 卷 `flag-data`（容器内 `/data/flags.db`）。

停止：`docker compose down`（加 `-v` 可同时删除数据卷）。

---

## 示例请求

### 1. 发布一份配置

```bash
curl -s -X POST http://localhost:8000/api/v1/configs \
  -H 'Content-Type: application/json' \
  --data @examples/publish_v1.json
```

返回 `201`：

```json
{ "version": "2026-09-17", "flag_count": 4, "created_at": "2026-09-17T12:00:00+00:00" }
```

### 2. 批量判定

```bash
curl -s -X POST http://localhost:8000/api/v1/evaluate \
  -H 'Content-Type: application/json' \
  --data @examples/eval_request.json
```

响应中每个开关都包含**最终值 `value`**、**命中原因 `reason`**、**命中的规则 `rule_id`**、**分流桶位 `rollout`** 以及完整**依赖链 `chain`**（叶子在前，目标开关在最后）：

```json
{
  "version": "2026-09-17",
  "context": { "email": "ada@acme.com", "tier": "silver", "theme": "auto", "user_id": "user-1" },
  "results": [
    {
      "flag": "new_checkout",
      "value": true,
      "hit": true,
      "reason": "rule_match",
      "rule_id": "internal-testers",
      "rollout": null,
      "chain": [
        { "flag": "signed_in", "value": true, "hit": true, "reason": "rule_match",
          "rule_id": "has-user", "prerequisite_of": "new_checkout", "expected": true },
        { "flag": "new_checkout", "value": true, "hit": true, "reason": "rule_match",
          "rule_id": "internal-testers", "prerequisite_of": null, "expected": null }
      ]
    },
    {
      "flag": "dark_mode",
      "value": "system",
      "hit": true,
      "reason": "rollout_out",
      "rollout": { "bucket": 56, "percentage": 50 },
      "chain": [ { "flag": "dark_mode", "value": "system", "hit": true,
                   "reason": "rollout_out",
                   "rollout": { "bucket": 56, "percentage": 50 } } ]
    },
    {
      "flag": "legacy_button",
      "value": false, "hit": false, "reason": "disabled",
      "rule_id": null, "rollout": null,
      "chain": [ { "flag": "legacy_button", "value": false, "hit": false, "reason": "disabled" } ]
    }
  ],
  "missing_flags": []
}
```

> `hit` 是 `value` 的布尔化结果（兼容字符串/数字变体值）；布尔开关请直接使用 `true/false` 作为规则值与默认值。

### 3. 验证稳定性

连续请求 N 次，同一 `version` + 同一 `user_id` 的桶位与最终值完全一致；把 `percentage` 从 25 调到 100 只会新增命中用户，不会把任何人移出。

### 4. 发布新版本，历史版本不受影响

```bash
# 改好 examples/publish_v1.json 的 version（例如 2026-09-18）后再发布
curl -s -X POST http://localhost:8000/api/v1/configs \
  -H 'Content-Type: application/json' --data @examples/publish_v2.json

# 老版本的判定结果保持不变
curl -s -X POST http://localhost:8000/api/v1/evaluate \
  -H 'Content-Type: application/json' \
  -d '{"version":"2026-09-17","flags":["new_checkout"],"user_id":"user-1"}'
```

重复发布同一版本返回 `409 version_exists`；判定/查询不存在的版本返回 `404 version_not_found`。

### 5. 查询版本

```bash
curl -s http://localhost:8000/api/v1/configs                       # 列出全部版本
curl -s http://localhost:8000/api/v1/configs/2026-09-17            # 读取某版本完整配置
```

### 6. 版本影响分析（对比两个版本）

```bash
# 再发布一个修改了前置开关 signed_in 的版本（新增 staff-bypass 规则）
curl -s -X POST http://localhost:8000/api/v1/configs \
  -H 'Content-Type: application/json' --data @examples/publish_v3.json

# 对比 2026-09-18 → 2026-09-19
curl -s -X POST http://localhost:8000/api/v1/config-comparisons \
  -H 'Content-Type: application/json' --data @examples/compare_request.json
```

响应：

```json
{
  "from_version": "2026-09-18",
  "to_version": "2026-09-19",
  "added": [],
  "removed": [],
  "modified": ["signed_in"],
  "impacts": [
    {
      "flag": "signed_in",
      "impacted": [
        { "flag": "new_checkout", "path": ["signed_in", "new_checkout"] }
      ]
    }
  ]
}
```

说明：

- `added` / `removed` / `modified`：按开关名比较两个版本的**规范化定义**（与存储一致的规范化结构比较，键序差异不算修改）；
- `impacts`：在**两个版本前置依赖边的并集**上沿反向依赖传播，逐个列出每个直接变化开关间接影响的上游开关；`path` 是从变化开关到依赖者的一条**最短路径**（同长取字典序最小）；
- 所有名称与路径均按字典序稳定排序，同一受影响开关只出现一次；没有受影响下游的变化开关也会列出（`impacted` 为空）；
- 任一版本不存在返回 `404 version_not_found`。

### 7. 环境指针：把环境绑定到已发布版本

环境（如 `production`、`staging`）是一条指向**已发布不可变版本**的指针，记录在 SQLite 的 `environments` 表中（外键引用 `config_versions.version`），并带有一个单调递增的 `revision` 乐观锁：

```bash
# 新建环境：必须且只能带 expected_revision=0，创建成功后 revision=1，返回 201
# （示例把 version 指向已发布的 v1；也可使用 examples/environment_put.json）
curl -s -X PUT http://localhost:8000/api/v1/environments/production \
  -H 'Content-Type: application/json' \
  -d '{"version":"v1","expected_revision":0}'
# {"name":"production","version":"v1","revision":1,"created_at":"...","updated_at":"..."}

# 读取指针
curl -s http://localhost:8000/api/v1/environments/production

# 移动指针：凭上一次读到的 revision（当前为 1）更新，成功后 revision 加一，返回 200
curl -s -X PUT http://localhost:8000/api/v1/environments/production \
  -H 'Content-Type: application/json' \
  -d '{"version":"v2","expected_revision":1}'
```

并发与错误语义（校验与写入在**同一个 `BEGIN IMMEDIATE` 事务**内完成）：

- 新建时环境已存在（仍传 `expected_revision=0`），或更新时 `expected_revision` 与当前 revision 不一致（竞争失败 / 过期写入）→ `409 stale_revision`（更新失败时响应体带 `current_revision`），**指针与 revision 都不改动**；
- 目标版本从未发布 → `404 version_not_found`，指针不改动（revision 也不自增）；同时满足“过期 + 版本不存在”时优先返回 `409`；
- 新建一个不存在的环境却传非 0 revision → 同样返回 `409 stale_revision`；
- 读取/判定一个不存在的环境 → `404 environment_not_found`；
- 每次**成功**写入都会在同一事务追加一条不可变审计记录，失败写入不留痕；完整轨迹见第 9 节 `GET /api/v1/environments/{name}/history`。

### 8. 面向环境的判定（自动跟随指针，单次请求同一快照）

```bash
curl -s -X POST http://localhost:8000/api/v1/environments/production/evaluate \
  -H 'Content-Type: application/json' --data @examples/environment_eval.json
```

请求体只需要 `flags`、`user_id`、`attributes`，无需客户端知道版本号；服务端通过一条 JOIN 查询取出指针及其指向的版本，**整次请求固定在该快照上**（即使判定途中另一个请求移动了指针，本请求的全部开关仍来自同一版本）。响应回显指针的 `version` 与 `revision`：

```json
{
  "name": "production",
  "version": "v2",
  "revision": 2,
  "context": { "...": "..." },
  "results": [ { "...与 /api/v1/evaluate 结构一致..." } ],
  "missing_flags": []
}
```

指针本身随时可被移动到新版本（或回滚到旧版本），但版本文档不可变，因此回显的 `(version, revision)` 与结果组合永远可复现；想绕过指针固定判定某个历史版本时，继续使用 `POST /api/v1/evaluate`。

### 9. 环境修订历史（不可变审计）

每一次**成功**的指针写入（创建或移动）都会在同一个 SQLite 事务里向 `environment_history` 表追加一条不可变记录，与指针更新同生共死——指针移动成功则记录落库，指针写入失败（过期 revision、版本不存在）则整体回滚、**不留任何记录**：

```bash
# 创建 + 一次移动之后（沿用第 7 节的两次 PUT，更新示例见 examples/environment_update.json）
curl -s "http://localhost:8000/api/v1/environments/production/history"
```

响应按 **revision 升序**返回该环境的全部指针变迁：

```json
{
  "name": "production",
  "history": [
    { "revision": 1, "version": "v1", "operation": "create", "created_at": "2026-09-18T12:00:00+00:00" },
    { "revision": 2, "version": "v2", "operation": "update", "created_at": "2026-09-18T12:05:00+00:00" }
  ]
}
```

说明：

- 每条记录包含 `revision`、目标 `version`、操作类型 `operation`（`create` / `update`）与写入时间 `created_at`；记录只增不改，回滚指针（如从 v2 退回 v1）会**追加一条新记录**（revision 继续递增），历史永不重写；
- 同一环境的 revision 序列从 1 开始连续无空洞——过期写入、目标版本不存在等失败操作不会产生记录，也不会消耗 revision；
- 查询参数：`from_revision`（≥0，默认 0，只返回 `revision >= from_revision` 的记录）与 `limit`（1–1000，默认 100），两者组合实现前向翻页（取上一页最后一条的 `revision + 1` 作为下一次的 `from_revision`）；非法取值返回 `422 invalid_request`；
- 环境不存在返回 `404 environment_not_found`；环境存在但无匹配记录时返回 200 与空 `history` 列表；
- 历史记录与指针在同一事务写入，因此最新一条记录的 `created_at` 恒等于指针的 `updated_at`，审计轨迹与当前状态永不分叉。

### 10. 可复用受众分组（segments）

发布一份带受众分组的配置（完整文件见 `examples/publish_segments.json`）：

```bash
curl -s -X POST http://localhost:8000/api/v1/configs \
  -H 'Content-Type: application/json' --data @examples/publish_segments.json
```

其中声明了三个分组：`acme-staff`（邮箱后缀匹配）、`valuable-users`（两个属性条件的 AND）、`vip`（**嵌套引用** `valuable-users` 再加一个属性条件）；开关 `concierge_support` 的两条规则分别用 `{"op":"segment","value":"acme-staff"}` 和 `{"op":"segment","value":"vip"}` 复用这些分组。

判定（`examples/eval_segments.json`）：

```bash
curl -s -X POST http://localhost:8000/api/v1/evaluate \
  -H 'Content-Type: application/json' --data @examples/eval_segments.json
```

`ada@acme.com` 命中 `acme-staff` 分组 → 首条规则 `staff-always` 命中；把邮箱换成其他域名、`tier` 换成 `gold`（且 `age>=18`、`region!=eu`）则命中 `vip-users`；都不满足时落到 `default`。读取该版本会回显分组定义：

```bash
curl -s http://localhost:8000/api/v1/configs/2026-09-20   # config.segments 原样返回
```

分组相关的非法配置在发布时被拒绝（`422 invalid_config`）：

```bash
# 引用了不存在的分组 → errors[].code = missing_segment（可定位到规则条件）
curl -s -X POST http://localhost:8000/api/v1/configs \
  -H 'Content-Type: application/json' --data @examples/invalid_segment_ref.json

# 分组互相引用成环 → errors[].code = circular_segment（可定位到分组）
curl -s -X POST http://localhost:8000/api/v1/configs \
  -H 'Content-Type: application/json' --data @examples/invalid_segment_cycle.json
```

### 11. 评估预览（两版本 × 样本 × 开关）

发布前想确认“新版本会改变哪些用户的判定结果”？`POST /api/v1/evaluation-previews` 用**两个已发布版本**对一批样本逐一计算 `样本 × 开关` 的判定，只报告**值层面**的差异：

```bash
# 对比 2026-09-17 → 2026-09-18（即示例中的 v1 → v2）
curl -s -X POST http://localhost:8000/api/v1/evaluation-previews \
  -H 'Content-Type: application/json' --data @examples/preview_request.json
```

请求体含 `from_version`、`to_version`、`flags`（≤ 50）、`subjects`（≤ 200）；每个样本含**唯一** `subject_id`、可选 `user_id` 与 `attributes`。响应：

```json
{
  "from_version": "2026-09-17",
  "to_version": "2026-09-18",
  "changed": [
    {
      "subject_id": "s-ada",
      "flag": "dark_mode",
      "from": { "exists": true, "value": "system", "reason": "rollout_out", "rule_id": null },
      "to": { "exists": false, "value": null, "reason": null, "rule_id": null }
    },
    {
      "subject_id": "s-bob",
      "flag": "new_checkout",
      "from": { "exists": true, "value": false, "reason": "rollout_out", "rule_id": null },
      "to": { "exists": true, "value": true, "reason": "rollout_in", "rule_id": null }
    },
    {
      "subject_id": "s-bob",
      "flag": "dark_mode",
      "from": { "exists": true, "value": "light", "reason": "rule_match", "rule_id": "forced-light" },
      "to": { "exists": false, "value": null, "reason": null, "rule_id": null }
    }
  ],
  "summary": [
    { "flag": "new_checkout", "changed": 1, "unchanged": 1 },
    { "flag": "dark_mode", "changed": 2, "unchanged": 0 }
  ]
}
```

判定规则：

- **changed 的判定**：开关只在一个版本存在（`exists` 不同），或双方 `value` 的 JSON 类型/值不同（结构化比较，布尔 `true` 不等于数字 `1`）；仅 `reason`/`rule_id` 不同**不算** changed——上例 `s-ada` 的 `new_checkout` 从 `rule_match` 变为 `rollout_in`，但值都是 `true`，因此未出现在明细中；
- **changed 明细**按请求顺序排列（先样本、后开关），每条含 `subject_id`、`flag` 和双方各自的 `exists`/`value`/`reason`/`rule_id`（不存在的一侧三者均为 `null`）；`summary` 按开关汇总 changed/unchanged 数，顺序为开关在请求中首次出现的顺序；
- **判定器作用域**：每个“版本 × 样本”使用独立判定器——同一样本的多个开关共享分组（segment）计算结果，但分组缓存**绝不跨样本复用**；
- **错误**：`subject_id` 重复、`subjects` 超过 200 或 `flags` 超过 50 → `422 invalid_request`；任一版本不存在 → `404 version_not_found`（先确认两个版本都存在再开始计算，不会返回部分结果）。

### 12. 环境变更审批（投票 → 门槛执行 → revision 复核）

移动环境指针除了第 7 节的直接 PUT，还可以走多人审批流。审批单创建时快照当前指针 revision（`base_revision`），执行时在**同一事务**内复核它，防止“审批期间指针被别人先动了”：

```bash
# 1) 创建审批单：目标版本必须存在且与当前指针不同；required_approvals 取 1～5
curl -s -X POST http://localhost:8000/api/v1/environments/production/approvals \
  -H 'Content-Type: application/json' --data @examples/approval_create.json
# {"id":1,"environment":"production","target_version":"v2","base_revision":2,
#  "applicant":"alice","required_approvals":2,"status":"pending",
#  "votes":[],"vote_count":0,"remaining_approvals":2, ...}

# 2) 不同审批人逐一投票（申请人 alice 不能投自己；同一人重复投返回 409）
curl -s -X POST http://localhost:8000/api/v1/environments/production/approvals/1/votes \
  -H 'Content-Type: application/json' --data @examples/approval_vote.json
curl -s -X POST http://localhost:8000/api/v1/environments/production/approvals/1/votes \
  -H 'Content-Type: application/json' -d '{"approver":"carol"}'

# 3) 票数达到门槛后执行；成功则指针移动、revision+1、environment_history 追加一条 update
curl -s -X POST http://localhost:8000/api/v1/environments/production/approvals/1/execute
# {"id":1,...,"status":"executed","vote_count":2,"remaining_approvals":0,
#  "votes":[{"approver":"bob",...},{"approver":"carol",...}], "executed_at":"..."}

# 4) 详情查询：状态（pending/executed/stale）、base_revision、票数与投票明细
curl -s http://localhost:8000/api/v1/environments/production/approvals/1
```

规则与错误语义：

- **创建**（`POST /api/v1/environments/{name}/approvals`，请求体 `{target_version, applicant, required_approvals}`）：环境不存在 → `404 environment_not_found`；目标版本未发布 → `404 version_not_found`；目标版本与当前指针相同 → `409 target_matches_pointer`；`required_approvals` 越界（不在 1～5）或字段非法 → `422 invalid_request`。响应 201 回显 `base_revision`（创建时的指针 revision）、`remaining_approvals` 与空票箱；
- **投票**（`.../approvals/{id}/votes`，请求体 `{approver}`）：申请人自批 → `409 applicant_cannot_approve`；同一审批人重复投票 → `409 already_voted`；审批单已执行/已失效 → `409 approval_not_pending`；不存在或不属于该环境 → `404 approval_not_found`。投票不会自动执行，只累计票数；
- **执行**（`.../approvals/{id}/execute`）：票数不足 → `409 insufficient_approvals`（响应带当前 `votes` 与 `required_approvals`），指针不动；审批单已终结 → `409 approval_not_pending`；
- **执行时的 revision 复核（同一 `BEGIN IMMEDIATE` 事务）**：若环境当前 revision 仍等于 `base_revision`，则把指针移动到 `target_version`、revision 加一，并**追加一条普通 `update` 审计记录**到现有 `environment_history`（与直接 PUT 完全一致，审批单同时变为 `executed`）；若 revision 已变化（审批期间被直接 PUT 或另一张审批单抢先执行），则审批单变为 `stale`、`executed_at` 记录判定时间，**指针不移动、历史不追加**，接口返回 `409 approval_stale`（带 `base_revision` 与 `current_revision`）。因此并发执行 N 张同 revision 的审批单时，恰有一张执行，其余全部 stale，revision 只增加一次；
- **详情**（`GET /api/v1/environments/{name}/approvals/{id}`）：返回全部创建字段、`status`（`pending` 待审 / `executed` 已执行 / `stale` 已失效）、`vote_count`、`remaining_approvals`、按投票先后排列的 `votes`（含 `approver`/`voted_at`）以及 `created_at`/`executed_at`；审批 id 不属于路径中的环境时同样返回 `404 approval_not_found`，跨环境操作无法借道生效。

### 非法配置会被拒绝

```bash
# 循环依赖 → 422 invalid_config，errors[].code = circular_dependency
curl -s -X POST http://localhost:8000/api/v1/configs \
  -H 'Content-Type: application/json' --data @examples/invalid_cycle.json

# 比例越界（120）→ 422 invalid_config，errors[].code = invalid_percentage
curl -s -X POST http://localhost:8000/api/v1/configs \
  -H 'Content-Type: application/json' --data @examples/invalid_percentage.json
```

---

## 配置 JSON 结构

```jsonc
{
  "version": "2026-09-17",          // 必填、非空、全局唯一、不可变
  "segments": {                     // 可选：可复用受众分组，顶层声明
    "vip": {
      "conditions": [               // 一组 AND 条件（空数组 = 匹配所有人）
        { "op": "segment", "value": "valuable-users" },  // 引用其他分组，无需 attr
        { "attr": "region", "op": "ne", "value": "eu" }  // 普通属性条件
      ]
    }
  },
  "flags": {
    "new_checkout": {
      "enabled": true,              // 关闭的开关直接取 default，reason=disabled
      "default": false,             // 所有规则/分流都未命中时的兜底值（任意 JSON）
      "prerequisites": [            // 前置开关，按顺序短路
        { "flag": "signed_in", "expect": true }
      ],
      "rules": [                    // 有序规则，首条命中即返回
        {
          "id": "internal-testers", // 同一开关内规则 ID 不可重复（可省略）
          "conditions": [           // 空数组 = 全量兜底规则
            { "attr": "email", "op": "suffix", "value": "@acme.com" },
            { "op": "segment", "value": "vip" }   // 规则条件同样可引用分组
          ],
          "value": true             // 命中后返回的值（任意 JSON）
        }
      ],
      "rollout": {                  // 规则未命中时进行百分比分流
        "percentage": 25,           // 整数，0–100，发布时强校验
        "key": "user_id",           // 目前仅支持 user_id
        "value": true               // 落入分流时返回的值
      }
    }
  }
}
```

### 判定优先级（每个开关）

1. **前置开关**：任一前置的实际布尔值不等于 `expect` → `prerequisite_failed`，短路返回 `false`；
2. **开关关闭**（`enabled=false`）→ 返回 `default`，`reason=disabled`；
3. **有序规则**：首个全部条件满足的规则命中 → `rule_match`，返回规则 `value`；
4. **百分比分流**：`bucket = sha1("开关名:user_id") 前 8 位十六进制 % 100`，`bucket < percentage` → `rollout_in`，否则 `rollout_out`；无 `user_id` 时一律落在外；
5. 都未命中 → 返回 `default`，`reason=default`。

支持的操作符：`eq`、`ne`、`in`、`nin`（值可为数组或逗号分隔字符串）、`gt`、`ge`、`lt`、`le`、`prefix`、`suffix`，以及分组引用 `segment`（`value` 为分组名，条件无需 `attr`）。类型不兼容的比较安全地返回不命中。

### 受众分组（segments）

- 顶层 `segments` 是 `分组名 → { conditions: [...] }` 的映射；每个分组是一组 **AND** 条件，普通条件沿用上述属性操作符；
- 分组可以嵌套：分组条件里用 `{"op": "segment", "value": "另一分组名"}` 引用其他分组，引用条件**无需 `attr`**；开关规则的 `conditions` 里同样可用；
- **发布时校验**：引用不存在的分组 → `missing_segment`；引用值不是非空字符串 → `invalid_segment_reference`；直接（自引用）或间接循环 → `circular_segment`。错误均带可定位信息：分组内的问题带 `segment` 字段与 `segments.<名>.conditions[i]` 路径，规则内的问题带 `flag` 字段与 `rules[i].conditions[j]` 路径；
- **判定语义**：分组递归求值，不匹配只视为该条件失败（继续尝试后续规则，最终落到 `default`）；同一请求内分组结果只计算一次并复用（批量判定多个开关共享）；分组与前置开关互不影响，首条规则命中与默认值语义保持不变；
- 分组定义随版本文档整体存储，`GET /api/v1/configs/{version}` 会原样回显 `segments`；环境判定与固定版本判定共用同一引擎，行为一致。

### 稳定性保证

- 同一版本的配置以规范化 JSON 整体存入 SQLite（`config_versions` 表，主键即版本号），**只增不改**；
- 环境指针存于 `environments` 表（`version` 外键指向 `config_versions`），指针可移动但版本文档永不变更；更新在单个 `BEGIN IMMEDIATE` 事务内完成“校验 revision → 确认版本存在 → revision+1 写入”，不会出现丢失更新或悬空指针；
- 每次成功的指针写入在同一事务内向 `environment_history` 表追加一条不可变审计记录（`UNIQUE(environment, revision)`，只插入、永不更新/删除）；失败写入整体回滚，因此每个环境的 revision 序列从 1 起连续无空洞，审计轨迹与指针状态严格一致；
- 变更审批的“校验票数 → 复核 revision → 移动指针 → 追加历史 → 回写审批状态”在单个 `BEGIN IMMEDIATE` 事务内完成：revision 未变才移动并追加审计，revision 已变则只把审批单标记 `stale`。审批票通过 `UNIQUE(approval_id, approver)` 保证一人一票，申请人投票在应用层拒绝；
- 判定是纯函数：`(配置, user_id, attributes) → 结果`，无随机数、无时间因子；
- 哈希同时混入开关名（盐），不同开关互不相关；放量从 0→100 单调，不会反复抖动。

---

## API 一览

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| `POST` | `/api/v1/configs` | 发布一个新版本（语义校验失败返回 422；版本重复返回 409） |
| `GET` | `/api/v1/configs` | 列出所有版本 |
| `GET` | `/api/v1/configs/{version}` | 获取某版本完整配置（不存在返回 404） |
| `POST` | `/api/v1/config-comparisons` | 版本影响分析，请求体 `{from_version, to_version}`，返回开关差异与反向依赖影响（任一版本不存在返回 404） |
| `POST` | `/api/v1/evaluation-previews` | 评估预览，请求体 `{from_version, to_version, flags[]≤50, subjects[]≤200}`（`subject_id` 唯一）；按“样本 × 开关”对比两版本判定，返回按请求排序的 changed 明细（含双方 `exists`/`value`/`reason`/`rule_id`）与按开关的 changed/unchanged 汇总；版本不存在返回 404 且无部分结果 |
| `PUT` | `/api/v1/environments/{name}` | 创建/移动环境指针，请求体 `{version, expected_revision}`；新建仅接受 `expected_revision=0`（201，revision 从 1 起），更新成功 revision 加一（200）；竞争/过期返回 409 `stale_revision`，版本不存在返回 404 `version_not_found` |
| `GET` | `/api/v1/environments/{name}` | 读取环境指针，返回 `{name, version, revision, created_at, updated_at}`（环境不存在返回 404 `environment_not_found`） |
| `GET` | `/api/v1/environments/{name}/history` | 读取环境修订审计轨迹，按 revision 升序返回 `{name, history[]}`（每条含 `revision`/`version`/`operation`/`created_at`）；支持 `from_revision`（≥0，默认 0）与 `limit`（1–1000，默认 100）前向翻页；环境不存在返回 404 `environment_not_found` |
| `POST` | `/api/v1/environments/{name}/approvals` | 创建环境变更审批单，请求体 `{target_version, applicant, required_approvals(1–5)}`，201 回显 `base_revision`/`status=pending`/票数；环境不存在 404、目标版本不存在 404、目标与当前指针相同 409 `target_matches_pointer` |
| `GET` | `/api/v1/environments/{name}/approvals/{id}` | 审批详情：状态 `pending`/`executed`/`stale`、`base_revision`、`vote_count`、`remaining_approvals`、投票明细与时间（id 不存在或不属于该环境返回 404 `approval_not_found`） |
| `POST` | `/api/v1/environments/{name}/approvals/{id}/votes` | 投票，请求体 `{approver}`；申请人自批 409 `applicant_cannot_approve`、重复投票 409 `already_voted`、审批单已终结 409 `approval_not_pending` |
| `POST` | `/api/v1/environments/{name}/approvals/{id}/execute` | 达门槛后执行：同事务复核 revision，未变则移动指针、追加历史并置 `executed`；票数不足 409 `insufficient_approvals`，revision 已变则置 `stale`、指针不动并返回 409 `approval_stale` |
| `POST` | `/api/v1/environments/{name}/evaluate` | 面向环境的批量判定，请求体 `{flags[], user_id?, attributes?}`，响应回显指针 `version`/`revision`，单次请求使用同一指针快照（环境不存在返回 404） |
| `POST` | `/api/v1/evaluate` | 按显式版本批量判定，请求体 `{version, flags[], user_id?, attributes?}` |
| `GET` | `/healthz` | 健康检查 |

错误统一使用信封格式：`{"error": {"code": "...", "message": "...", "errors": [...]}}`。
请求体校验失败（缺字段、类型错误、`extra` 字段、重复 `subject_id`、超长列表、非法 JSON 等）一律返回 `422 invalid_request`；`errors` 每项保留 `loc`/`type`/`msg`/`input`，`ctx` 中异常等非 JSON 值会被递归转成稳定字符串（例如 `ctx.error = "duplicate subject_id 's-1'"`），重复标识始终出现在 `msg` 中。
批量判定中个别开关不存在不会使整个请求失败：结果里该开关 `reason=flag_not_found`，同时出现在顶层 `missing_flags` 中。

---

## 本地开发与测试（不使用 Docker）

```bash
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt

FLAG_DB_PATH=data/flags.db uvicorn app.main:app --reload --port 8000
```

核心引擎与校验器只依赖标准库，可直接运行单测（无需安装任何依赖；缺少 FastAPI/httpx 时 `tests/test_api_validation.py` 的接口回归会自动跳过）：

```bash
python3 -m unittest discover -s tests -v
```

如需运行接口回归（`tests/test_api_validation.py`，基于 FastAPI TestClient），在开发环境额外安装 `httpx`：

```bash
pip install httpx   # 仅测试需要；生产镜像只安装 requirements.txt
```

测试覆盖：有序规则与短路前置、依赖链顺序、禁用开关、哈希桶稳定性/单调性/边界（0 与 100）、二/三节点循环与自依赖拒绝、非法比例与未知操作符拒绝、受众分组的嵌套引用/AND 语义/不匹配落默认/请求内结果复用/悬空引用与循环的防御性求值、分组发布校验（悬空引用、非法引用值、直接与间接循环、分组内未知操作符、错误可定位到分组或规则条件）、SQLite 版本不可变与历史保留、分组定义随版本存储与回显、固定版本重复判定稳定性、版本差异对比与影响传播（边并集、最短路径、字典序平局、并集成环、重复去重）、评估预览（值变化回显双方明细、reason/rule_id 差异忽略、JSON 类型敏感的结构化比较、存在性差异、双版本均缺失、明细按请求排序、按开关汇总、分流按各样本 user_id 计算、分组缓存在“版本 × 样本”内共享且不跨样本复用）、环境指针的创建/更新/revision 自增/过期写入/版本不存在/409 优先于 404/指针快照一致性，以及多线程并发更新下“每个 revision 恰有一个胜者、无丢失更新”；修订审计覆盖创建/更新各写一条记录、记录与指针同事务（失败/过期/未知版本写入不留记录）、revision 序列连续无空洞、回滚追加新记录而不改写历史、按 revision 升序、`from_revision`/`limit` 过滤与翻页、多环境历史相互独立、并发竞争失败者不留记录、记录时间与指针 `updated_at` 一致；环境变更审批覆盖创建记录环境/目标版本/base_revision/申请人/门槛（1～5 边界）、环境/版本不存在、目标与指针相同拒绝、申请人不得自批、一人一票、未达门槛禁止执行、达门槛后同事务移动指针并追加历史、revision 已变则标记 stale 且指针/历史不动、执行后终态拒绝再投票/再执行、两张同 revision 审批单竞争恰一执行另一 stale、多线程并发执行仅一个胜者且 revision 只增一次、跨环境 id 不可见不可操作；另有 `tests/test_api_validation.py` 通过 FastAPI TestClient 做接口回归：重复 `subject_id` 返回 422 `invalid_request` 信封（而非 500）、`errors[]` 保留 `loc`/`type`/`msg`/`input` 且 `ctx` 异常被字符串化、普通字段错误（含 `ctx` 数值）、缺失/多余字段与非法 JSON 走同一处理器、版本不存在仍为 404、合法预演仍返回 200，历史接口的升序返回/404/查询参数过滤/失败写入不留记录/非法参数 422，以及审批接口的完整创建→投票→执行→详情链路、422/404/409 各类错误信封、stale 执行 409 并保持指针与历史、终态再操作 409、未知/跨环境审批 404。

## 项目结构

```
app/
  main.py        FastAPI 路由与错误处理
  schemas.py     Pydantic v2 请求/响应模型（含 segments 与条件模型）
  db.py          SQLite 不可变版本存储 + 环境指针（WAL、外键、BEGIN IMMEDIATE 乐观并发）+ 不可变修订审计表 + 环境变更审批（投票门槛与执行时 revision 复核）
  validation.py  发布时语义校验（循环依赖/比例/操作符/分组引用与分组循环）
  evaluator.py   纯函数判定引擎（前置、分组递归求值与请求内复用、规则、稳定哈希分流）
  comparison.py  版本对比与反向依赖影响分析（纯函数）
  preview.py     评估预览：两版本 × 样本 × 开关的判定差异（纯函数，分组缓存不跨样本）
tests/           标准库 unittest 测试（test_engine.py）+ FastAPI TestClient 接口回归（test_api_validation.py）
examples/        发布/判定/受众分组/版本对比/评估预览/环境指针/变更审批/非法配置示例
Dockerfile, docker-compose.yml
```

> 升级到包含环境指针的版本时，服务启动会自动对已有的 `flags.db` 执行 `CREATE TABLE IF NOT EXISTS environments` 与 `CREATE TABLE IF NOT EXISTS environment_history`，卷中历史版本数据不受影响，无需手动迁移。升级前已存在的环境指针没有历史记录属正常——审计从升级后的第一次成功写入开始累积，`GET .../history` 会返回空列表（环境本身仍返回 200）。
>
> 升级到包含变更审批的版本时，启动还会自动创建 `CREATE TABLE IF NOT EXISTS environment_approvals`、`approval_votes`（含 `UNIQUE(approval_id, approver)`）与一个按环境检索的索引，已有数据与指针不受影响，无需手动迁移。
