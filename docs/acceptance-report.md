# 验收自测记录

对应 `backend_requirements.md` 第 8 章的 13 条验收用例。脚本：
[`stdeel-backend/scripts/acceptance_test.py`](../stdeel-backend/scripts/acceptance_test.py)（纯标准库）。

## 环境

| 项 | 值 |
|---|---|
| 时间 | 2026-10-31 23:22 CST |
| 系统 | Debian 12 / Linux 6.1.0-51-amd64 |
| Python | 3.11.2（宝塔 venv `/www/server/pyporject_evn/3.11.2venv`） |
| 依赖 | FastAPI 0.124.0 / SQLAlchemy 2.0.41 / Pydantic 2.12.5 / aiosqlite 0.21.0 |
| 数据库 | SQLite 3.40.1（WAL） |
| 被测实例 | 临时库 `STDEEL_DB_PATH=/tmp/stdeel-final.db`，端口 8597 |

## 运行方式

```bash
# 1) 根路径
python3 scripts/acceptance_test.py --base-url http://127.0.0.1:8597
# 2) 带任意前缀（Base URL 兼容性）
python3 scripts/acceptance_test.py --base-url http://127.0.0.1:8597/stapi
```

## 结果：15/15 通过（根路径 + 前缀两种模式各跑一遍，均 exit 0）

| # | 用例 | 结果 | 实测 |
|---|---|---|---|
| 1 | 首次启动注册，同 device_id 复用 | ✅ | 两次注册均返回 `u_1` |
| 2 | 用户名绑定，同名复用 | ✅ | 两次注册均返回 `u_2` |
| 3 | 批量上传 2 道题返回 2 个数字 id | ✅ | `items=[{"id":1},{"id":2}]` |
| 4 | 标记「正确」→ PATCH → 再 GET 状态不被清空 | ✅ | `user_feedback=correct`，二次同步仍为 correct |
| 5 | 标记「部分错误」 | ✅ | `feedback=partial`；知识点 correct=2、wrong=1（correct 记录 +1，partial 两边各 +1） |
| 6 | PATCH 重复调用幂等 | ✅ | 掌握度保持 correct=2 / wrong=1 |
| 7 | 删除记录 + 重复 DELETE | ✅ | GET 不再返回；重复 DELETE 仍 200 |
| 8 | 离线单条补传 + 同 local_id 重传 | ✅ | 两次返回同一 id，总行数不变 |
| 9 | 批量上传未拿到 id 后重传 | ✅ | 两次返回同一批 id，行数不变（内容指纹去重） |
| 10 | 换机 + username 绑定 | ✅ | 新设备拿到同一 `u_1`，历史含 `partial` 与 `action_type` |
| 11 | 答案库上传 + 匹配 | ✅ | 精确 `hit/similarity=1.0`，模糊 `0.9375`，`knowledge_points` 为 JSON 字符串 `["勾股定理"]` |
| 12 | `/healthz` 根路径 + 带前缀 | ✅ | 两种地址均 200 |
| 13 | 缺字段容错（无 action_type/user_feedback） | ✅ | 回传 `subject=未分类`、`action_type=solve`、`user_feedback=none`、`knowledge_points=[]` |
| A1 | PUT /users/api-key 全量覆盖幂等 | ✅ | 重复 PUT 后仍是 2 条 |
| A2 | 未知 user_id 查询 | ✅ | 200 + `total=0`，不 5xx |

## 迁移验证（真实库副本）

把线上 `app.db` 复制到 `/tmp/stdeel-mig.db` 后启动新版本：

- `users` 表重建（username 可空 + `platform` / `api_keys`），2 条用户数据完整保留；
- `solve_records` / `answer_library` / `knowledge_mastery` 通过 `ALTER TABLE` 补列，原有数据保留；
- 旧 `user_api_keys` 表数据并入 `users.api_keys` 后删除该表；
- 旧 FTS5 索引移除；
- `answer_library` 的 `question_hash` 校验与原值一致；
- 迁移后再次启动：`schema v2 -> v2`，无重复变更（幂等）；
- 迁移后的库再跑一遍验收：15/15 通过。

## 已知偏差

见 [API.md § 7](API.md#7-与契约的偏差记录)。

## 额外回归验证（v0.9.11「状态被清空」）

先用形态 A 上传一条 `action_type=correct` / `user_feedback=correct` 的记录，
再用形态 B（同一 `local_id`）故意带 `solve` / `none` 重传，模拟客户端迟到的补传：

- GET 回传仍为 `action_type=correct`、`user_feedback=correct`、`knowledge_points` 原样；
- 掌握度 `correct=1 / wrong=0`；随后 PATCH 改判 `partial` → `correct=1 / wrong=1`。

脚本内同样覆盖了这一路径（用例 4/6 与附加校验）。
