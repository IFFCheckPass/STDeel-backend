# 数据库设计（schema v2）

SQLite，文件默认 `stdeel-backend/app.db`，开启 WAL。迁移由
[`migrations.py`](../stdeel-backend/migrations.py) 在应用启动时幂等执行。

## users

| 列 | 类型 | 约束 | 说明 |
|---|---|---|---|
| `id` | INTEGER | PK 自增 | 内部主键；对外编码为 `u_<id>` |
| `device_id` | VARCHAR(255) | 可空、唯一 | 设备稳定标识，同设备复用同一账号 |
| `username` | VARCHAR(255) | 可空、唯一 | 跨设备找回数据的唯一凭据 |
| `platform` | VARCHAR(32) | NOT NULL，默认 `android` | |
| `api_keys` | TEXT | NOT NULL，默认 `[]` | JSON：`[{api_key,name,enabled}]`，全量覆盖 |
| `created_at` | DATETIME | | UTC |
| `last_active_at` | DATETIME | | 注册/写入时刷新 |

索引：`uq_users_username`、`uq_users_device_id`（均为部分唯一索引，排除 NULL）。

## solve_records

| 列 | 类型 | 说明 |
|---|---|---|
| `id` | INTEGER PK | 数字主键，客户端用它 PATCH/DELETE |
| `user_id` | INTEGER | 归属用户（内部 id） |
| `local_id` | INTEGER 可空 | 客户端本地 SQLite 主键，`(user_id, local_id)` 唯一 |
| `batch_key` | VARCHAR(64) 可空 | 无 local_id 时的批量去重指纹 |
| `question_text` / `answer` / `solution` | TEXT | 可含 LaTeX |
| `knowledge_points` | TEXT | JSON 数组字符串 |
| `subject` | VARCHAR(32) | 默认「未分类」 |
| `ai_model` | VARCHAR(128) | |
| `latency_ms` / `tokens_used` | INTEGER | 默认 0 |
| `matched` | BOOLEAN | 是否命中答案库 |
| `action_type` | VARCHAR(16) | `solve/retry/detail/correct/partial/wrong` |
| `user_feedback` | VARCHAR(16) | `none/correct/partial/wrong` |
| `source` | VARCHAR(16) | `ai/answer_library/error` |
| `confidence` | FLOAT 可空 | 0~1 |
| `question_no` | INTEGER | 卷内题号，未知为 0 |
| `image_path` | TEXT | 客户端本地路径（不透明字符串） |
| `created_at` / `updated_at` | DATETIME | 服务端接收时间（UTC） |

索引：`uq_solve_records_user_local`（部分唯一）、`ix_solve_records_user_created`、
`ix_solve_records_batch_key`。

## knowledge_mastery

| 列 | 类型 | 说明 |
|---|---|---|
| `id` | INTEGER PK | |
| `user_id` | INTEGER | |
| `knowledge_point` | VARCHAR(255) | |
| `subject` | VARCHAR(32) | 默认「未分类」，取出现次数最多的科目 |
| `correct_count` / `wrong_count` | INTEGER | partial 会同时计入两边 |
| `total_count` | INTEGER | correct + wrong |
| `error_rate` | FLOAT | wrong / total |
| `updated_at` | DATETIME | |

唯一索引 `uq_knowledge_mastery_user_point`。整表是 `solve_records` 的派生数据：
每次写入/改反馈/删除后，按该用户全部记录重算（见 `services.recompute_mastery`）。

## answer_library

| 列 | 类型 | 说明 |
|---|---|---|
| `id` | INTEGER PK | |
| `user_id` | INTEGER 可空 | 为空表示历史共享题库 |
| `question_text` | TEXT | |
| `question_hash` | VARCHAR(64) | 规范化题干的 SHA-256（trim + 空白压缩） |
| `answer` / `solution` | TEXT | |
| `knowledge_points` | TEXT | JSON 数组字符串 |
| `subject` | VARCHAR(32) | |
| `created_at` / `updated_at` | DATETIME | |

唯一索引 `uq_answer_library_user_hash`（部分唯一）；`ix_answer_library_hash` 用于精确匹配。

## schema_meta

`key/value` 两列，记录 `schema_version = 2` 与 `migrated_at`。

## v1 → v2 迁移步骤

1. `users`：`username` 由 NOT NULL 改为可空，新增 `platform`、`api_keys`（重建表并保留 id）。
2. `solve_records`：新增 `local_id`、`batch_key`、`subject`、`action_type`、`source`、
   `confidence`、`question_no`、`updated_at`。
3. `answer_library`：新增 `user_id`、`subject`；补齐缺失的 `question_hash`。
4. `knowledge_mastery`：新增 `subject`，校正 `total_count` / `error_rate`。
5. `user_api_keys` → 并入 `users.api_keys` 后删除旧表。
6. 删除旧 FTS5 虚拟表与触发器（中文分词无效）。
7. 历史数据规范化：`user_feedback` 空值 → `none`、`action_type` → `solve`、
   `knowledge_points` 非数组 → `[]` 等。
8. 去重后创建唯一索引；写入 `schema_meta`。

整个迁移在事务内执行，重复启动不会重复生效。
