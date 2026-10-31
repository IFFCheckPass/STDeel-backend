# STDeel 后端 API（v2）

> 与 `backend_requirements.md`（v0.9.11 客户端契约）逐条对齐。所有响应均为 JSON，
> 时间统一为 ISO-8601 UTC（`2026-10-31T12:00:00Z`）。

## 0. 通用约定

| 项 | 说明 |
|---|---|
| Base URL | 后端同时监听「根路径」与「任意路径前缀」。`https://host/api/v1/users/register`、`https://host/stapi/users/register`、`https://host/users/register` 等价 |
| 鉴权 | 无 Token / Cookie；身份仅由 `user_id` 表达（与当前客户端一致） |
| user_id | 对外形如 `u_1`；同时兼容老客户端的纯数字 `1`（入参、query 均可） |
| 成功判定 | 2xx |
| 幂等 | `POST /solve-records`（形态 B）、`PATCH`、`DELETE`、`PUT /users/api-key` 均幂等 |
| 交互文档 | `/docs`（Swagger UI）、`/redoc` |

**路径前缀实现**：服务端把请求路径里第一个「已注册资源段」（`users` / `solve-records` /
`answer-library` / `knowledge` / `files` / `healthz` / `webui` / `uploads` / `docs` …）
之前的部分整体当作前缀丢弃，因此不需要为每个前缀单独配置路由。

---

## 1. 账号

### POST /users/register

请求（设备注册 / 用户名绑定，二者可同时传）：

```json
{ "device_id": "550e8400-...", "platform": "android" }
{ "username": "张三", "platform": "android" }
{ "device_id": "550e8400-...", "username": "张三", "platform": "android" }
```

响应：

```json
{ "user_id": "u_1", "id": "u_1", "username": "张三", "device_id": "550e8400-...",
  "platform": "android", "created_at": "2026-10-31T12:00:00Z", "last_active_at": "..." }
```

规则：
- 先按 `device_id` 查，命中则复用；否则按 `username` 查，命中则复用；都不命中则新建。
- 同一 `device_id`、同一 `username` 永远返回同一个 `user_id`（唯一索引兜底）。
- 已有设备用户首次带 `username` 登录时会自动补绑（不抢占别人的用户名）。
- 两个字段都缺省 → 400。

### PUT /users/api-key

```json
{ "user_id": "u_1", "api_keys": [ { "api_key": "sk-xxx", "name": "通义千问", "enabled": true } ] }
```

全量覆盖（空数组即清空）。响应 `{ "ok": true, "user_id": "u_1", "count": 1 }`。

### GET /users/api-key?user_id=u_1

返回该用户已保存的 Key 列表（换机同步 / 管理台使用）。

---

## 2. 解题记录

### POST /solve-records — 形态 A（批量）

```json
{
  "user_id": "u_1", "ai_model": "qwen-vl-max", "latency_ms": 3200, "tokens_used": 512,
  "source": "ai", "image_path": "/data/.../x.jpg",
  "questions": [
    { "id": 18, "content": "题干", "knowledge_points": ["三角函数"], "answer": "答案",
      "solution": "过程", "confidence": 0.95, "subject": "数学", "question_no": 0,
      "action_type": "solve", "user_feedback": "none" }
  ]
}
```

响应（**必须**，顺序与 `questions` 一致）：

```json
{ "items": [ { "id": 1001 }, { "id": 1002 } ] }
```

- `questions[].id` 是客户端本地主键，只用于去重（落库为 `local_id`），不会当作后端主键。
- `action_type` / `user_feedback` 缺失时按 `solve` / `none` 落库（兼容 ≤0.9.10 客户端）。
- 整批都**没有** `id` 时，用「内容指纹 + 批次位置 + 时间窗口（默认 900s）」兜底去重，
  重传不会产生重复行，且返回同一批 id。

### POST /solve-records — 形态 B（单条补传）

```json
{ "user_id": "u_1", "local_id": 18, "question_text": "题干", "answer": "答案", "solution": "过程",
  "ai_model": "qwen-vl-max", "latency_ms": 3200, "tokens_used": 512, "matched": false,
  "action_type": "correct", "user_feedback": "correct", "image_path": "/data/...", "subject": "数学" }
```

响应（**恰好一个数字 id**）：`{ "id": 1001 }`

按 `(user_id, local_id)` upsert：同一条重复补传返回同一个 id，不新增行。

### PATCH /solve-records/{id}/feedback

```json
{ "feedback": "correct", "user_id": "u_1" }
```

`feedback ∈ none | correct | partial | wrong`。幂等；记录不存在或不属于该用户也返回 2xx
（`{"ok": true, "missing": true}`），避免客户端墓碑永远清不掉。

### DELETE /solve-records/{id}

HTTP DELETE 可带 body `{ "user_id": "u_1" }`（也支持 `?user_id=`）。**永远 2xx**：

```json
{ "ok": true, "deleted": true, "id": 1001 }
```

### GET /solve-records

Query：`user_id`（必填）、`correct_days`（默认 30）、`wrong_days`（默认 90）、`limit`（可选）。

返回 `{ "total": N, "items": [...] }`，`items` 按 `created_at` 倒序。窗口规则：
- `correct` / `partial`：近 `correct_days` 天；
- `wrong`：近 `wrong_days` 天；
- `none`（未标注）：**始终返回**，否则换机后未标注的历史会消失。

每个条目包含：`id`（数字）、`question_text`、`answer`、`solution`、`knowledge_points`（**数组**）、
`subject`、`ai_model`、`latency_ms`、`tokens_used`、`matched`、`action_type`、`status`（= action_type，
兼容老客户端）、`user_feedback`、`source`、`confidence`、`question_no`、`image_path`、`created_at`。

---

## 3. 标准答案库

### POST /answer-library

```json
{ "user_id": "u_1", "question_text": "题干", "question_hash": "sha256hex",
  "answer": "标准答案", "solution": "过程", "knowledge_points": ["三角函数"], "subject": "数学" }
```

按 `(user_id, question_hash)` upsert。`question_hash` 由服务端按「trim + 空白压缩 + UTF-8 SHA-256」
重新计算，保证与匹配侧一致。

### POST /answer-library/match

```json
{ "user_id": "u_1", "question_text": "题干", "question_hash": "sha256hex" }
```

响应：

```json
{ "hit": true, "similarity": 0.93, "matched": true,
  "answer": { "id": 3, "answer": "答案", "solution": "过程", "subject": "数学",
              "knowledge_points": "[\"三角函数\",\"两角和差公式\"]" } }
```

> 注意：`answer.knowledge_points` 是 **JSON 字符串**（客户端会 `jsonDecode`），
> 与 `GET /solve-records` 里的数组不同。

匹配策略：先精确 hash，再「LIKE 关键词探测候选 + `difflib` 相似度」，阈值 0.85。

---

## 4. 知识点

| 接口 | 说明 |
|---|---|
| `GET /knowledge/mastery?user_id=u_1` | 知识点掌握列表（不传 user_id 返回全局聚合） |
| `GET /knowledge/weak?user_id=u_1&threshold=0.5` | 错误率 ≥ 阈值的薄弱知识点 |
| `GET /knowledge/points` | 全部知识点（去重） |

掌握度口径：`correct` → correct+1；`partial` → correct+1 且 wrong+1；`wrong` → wrong+1。
掌握度是**派生数据**：每次写记录 / 改反馈 / 删记录后，按该用户的全部解题记录重算，
所以重复 PATCH 不会重复计数。

---

## 5. 其它

| 接口 | 说明 |
|---|---|
| `GET /healthz` | 健康检查，host 根路径（忽略 Base URL 前缀），不访问数据库 |
| `GET /health` | 同上（兼容旧地址） |
| `POST /files/upload` | multipart 图片上传（当前客户端未调用） |
| `GET /webui` | 管理台（用户 / API Key / 掌握度 / 解题记录） |
| `GET /users`、`GET /users/{id}`、`GET /users/{id}/records`、`DELETE /users/{id}` | 管理端接口 |

---

## 6. 枚举

| 字段 | 取值 |
|---|---|
| `user_feedback` | `none` / `correct` / `partial` / `wrong` |
| `action_type` | `solve` / `retry` / `detail` / `correct` / `partial` / `wrong` |
| `source` | `ai` / `answer_library` / `error` |
| `subject` | `数学` `语文` `英语` `物理` `化学` `生物` `政治` `历史` `地理` `其他` `未分类`（后端宽容：非空即存） |
| `platform` | 恒为 `android`（含 Windows 桌面版） |

---

## 7. 与契约的偏差记录

| 项 | 说明 |
|---|---|
| `user_id` 类型 | 返回字符串 `u_1`（契约允许字符串/数字）。同时接受纯数字入参，老客户端不受影响 |
| 批量去重 | 契约建议「内容哈希 + 时间窗口」或「questions[].id」。实现优先用 `questions[].id`（存为 `local_id`），整批没有 id 时才用内容指纹 + 900s 窗口 |
| `POST /solve-records` 未知 user_id | 返回 404（客户端总会先注册） |
| `GET /solve-records` 未知 user_id | 返回 `200 + 空列表`（不阻塞客户端同步流程） |
| `PATCH` 记录不存在 | 返回 `200 + missing`（客户端不重试，避免墓碑堆积） |
| `/files/upload` | 保留实现，当前客户端未调用 |
