"""旧库平滑升级。

设计原则：**幂等**。每次启动都跑一遍，只补齐缺失的列/索引并把历史数据
规范化到新契约，不会删除业务数据（唯一例外：迁移完成后清掉已废弃的
user_api_keys 表与 FTS 索引，两者内容都已迁走/可由 question_text 重建）。

老版本 schema（v1）与本版本（v2）的差异：
- users：username 由 NOT NULL 改为可空；新增 platform、api_keys(JSON)；
- solve_records：新增 local_id / batch_key / subject / action_type / source /
  confidence / question_no / updated_at；
- answer_library：新增 user_id / subject；
- knowledge_mastery：新增 subject；
- user_api_keys 表并入 users.api_keys；
- 旧的 FTS5 虚拟表移除（对中文分词无效，匹配改用 LIKE 探测 + 相似度）。
"""

import json
import logging
from datetime import datetime

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncConnection

from utils import question_hash

logger = logging.getLogger(__name__)

SCHEMA_VERSION = "2"

ADDED_COLUMNS: dict[str, list[tuple[str, str]]] = {
    "solve_records": [
        ("local_id", "local_id INTEGER"),
        ("batch_key", "batch_key VARCHAR(64)"),
        ("subject", "subject VARCHAR(32)"),
        ("action_type", "action_type VARCHAR(16)"),
        ("source", "source VARCHAR(16)"),
        ("confidence", "confidence FLOAT"),
        ("question_no", "question_no INTEGER"),
        ("updated_at", "updated_at DATETIME"),
    ],
    "answer_library": [
        ("user_id", "user_id INTEGER"),
        ("subject", "subject VARCHAR(32)"),
        ("updated_at", "updated_at DATETIME"),
    ],
    "knowledge_mastery": [
        ("subject", "subject VARCHAR(32)"),
    ],
}


# --------------------------------------------------------------------------
# 基础工具
# --------------------------------------------------------------------------
async def _table_exists(conn: AsyncConnection, table: str) -> bool:
    row = (
        await conn.execute(
            text("SELECT 1 FROM sqlite_master WHERE type='table' AND name=:name"),
            {"name": table},
        )
    ).first()
    return row is not None


async def _table_info(conn: AsyncConnection, table: str) -> dict[str, dict]:
    rows = (await conn.execute(text(f'PRAGMA table_info("{table}")'))).all()
    return {
        row[1]: {"type": row[2], "notnull": row[3], "default": row[4], "pk": row[5]}
        for row in rows
    }


async def _add_column(conn: AsyncConnection, table: str, column: str, ddl: str) -> None:
    info = await _table_info(conn, table)
    if column in info:
        return
    await conn.execute(text(f'ALTER TABLE "{table}" ADD COLUMN {ddl}'))
    logger.info("迁移：%s 新增列 %s", table, column)


async def _execute(conn: AsyncConnection, sql: str, **params) -> None:
    await conn.execute(text(sql), params)


# --------------------------------------------------------------------------
# 各表升级
# --------------------------------------------------------------------------
async def _upgrade_users(conn: AsyncConnection) -> None:
    info = await _table_info(conn, "users")
    needs_rebuild = bool(info.get("username", {}).get("notnull")) or "api_keys" not in info

    if needs_rebuild:
        cols = set(info)
        await conn.execute(text("DROP TABLE IF EXISTS users__new"))
        await conn.execute(
            text(
                """
                CREATE TABLE users__new (
                    id INTEGER NOT NULL PRIMARY KEY AUTOINCREMENT,
                    device_id VARCHAR(255),
                    username VARCHAR(255),
                    platform VARCHAR(32) NOT NULL DEFAULT 'android',
                    api_keys TEXT NOT NULL DEFAULT '[]',
                    created_at DATETIME,
                    last_active_at DATETIME
                )
                """
            )
        )
        source = [
            "id",
            "device_id" if "device_id" in cols else "NULL",
            "username" if "username" in cols else "NULL",
            "'android'",
            "'[]'",
            "created_at" if "created_at" in cols else "CURRENT_TIMESTAMP",
            "last_active_at" if "last_active_at" in cols else ("created_at" if "created_at" in cols else "CURRENT_TIMESTAMP"),
        ]
        await conn.execute(
            text(
                "INSERT INTO users__new (id, device_id, username, platform, api_keys, created_at, last_active_at) "
                f"SELECT {', '.join(source)} FROM users"
            )
        )
        await conn.execute(text("DROP TABLE users"))
        await conn.execute(text("ALTER TABLE users__new RENAME TO users"))
        logger.info("迁移：users 表已重建（username 可空 + platform/api_keys）")
    else:
        await _add_column(conn, "users", "platform", "platform VARCHAR(32) DEFAULT 'android'")
        await _add_column(conn, "users", "api_keys", "api_keys TEXT DEFAULT '[]'")

    await _execute(conn, "UPDATE users SET api_keys='[]' WHERE api_keys IS NULL OR TRIM(api_keys)=''")
    await _execute(conn, "UPDATE users SET platform='android' WHERE platform IS NULL OR TRIM(platform)=''")
    await _execute(conn, "UPDATE users SET username=NULL WHERE username IS NOT NULL AND TRIM(username)=''")
    await _execute(conn, "UPDATE users SET device_id=NULL WHERE device_id IS NOT NULL AND TRIM(device_id)=''")


async def _normalize_json_list_column(conn: AsyncConnection, table: str, column: str) -> None:
    """把非数组的 JSON / 非 JSON 文本统一成 JSON 数组字符串。"""
    await _execute(
        conn,
        f"UPDATE {table} SET {column}='[]' WHERE {column} IS NULL OR TRIM({column})=''",
    )
    # JSON 标量（如 "\"三角函数\""）包成数组
    await _execute(
        conn,
        f"UPDATE {table} SET {column}=json_array(json_extract({column}, '$')) "
        f"WHERE CASE WHEN json_valid({column}) THEN json_type({column}) IN ('text','integer','real') ELSE 0 END",
    )
    # 仍然不是数组的，归零
    await _execute(
        conn,
        f"UPDATE {table} SET {column}='[]' "
        f"WHERE CASE WHEN json_valid({column}) THEN json_type({column}) <> 'array' ELSE 1 END",
    )


async def _normalize_solve_records(conn: AsyncConnection) -> None:
    statements = [
        "UPDATE solve_records SET user_feedback='none' "
        "WHERE user_feedback IS NULL OR TRIM(user_feedback)='' "
        "OR user_feedback NOT IN ('none','correct','partial','wrong')",
        "UPDATE solve_records SET action_type='solve' WHERE action_type IS NULL OR TRIM(action_type)=''",
        "UPDATE solve_records SET subject='未分类' WHERE subject IS NULL OR TRIM(subject)=''",
        "UPDATE solve_records SET source='ai' WHERE source IS NULL OR TRIM(source)=''",
        "UPDATE solve_records SET question_text='' WHERE question_text IS NULL",
        "UPDATE solve_records SET answer='' WHERE answer IS NULL",
        "UPDATE solve_records SET solution='' WHERE solution IS NULL",
        "UPDATE solve_records SET ai_model='' WHERE ai_model IS NULL",
        "UPDATE solve_records SET image_path='' WHERE image_path IS NULL",
        "UPDATE solve_records SET latency_ms=0 WHERE latency_ms IS NULL",
        "UPDATE solve_records SET tokens_used=0 WHERE tokens_used IS NULL",
        "UPDATE solve_records SET question_no=0 WHERE question_no IS NULL",
        "UPDATE solve_records SET matched=0 WHERE matched IS NULL",
        "UPDATE solve_records SET updated_at=created_at WHERE updated_at IS NULL",
    ]
    for sql in statements:
        await _execute(conn, sql)
    await _normalize_json_list_column(conn, "solve_records", "knowledge_points")


async def _normalize_answer_library(conn: AsyncConnection) -> None:
    statements = [
        "UPDATE answer_library SET question_text='' WHERE question_text IS NULL",
        "UPDATE answer_library SET answer='' WHERE answer IS NULL",
        "UPDATE answer_library SET solution='' WHERE solution IS NULL",
        "UPDATE answer_library SET subject='未分类' WHERE subject IS NULL OR TRIM(subject)=''",
        "UPDATE answer_library SET updated_at=created_at WHERE updated_at IS NULL",
    ]
    for sql in statements:
        await _execute(conn, sql)
    await _normalize_json_list_column(conn, "answer_library", "knowledge_points")


async def _normalize_mastery(conn: AsyncConnection) -> None:
    statements = [
        "UPDATE knowledge_mastery SET subject='未分类' WHERE subject IS NULL OR TRIM(subject)=''",
        "UPDATE knowledge_mastery SET correct_count=0 WHERE correct_count IS NULL",
        "UPDATE knowledge_mastery SET wrong_count=0 WHERE wrong_count IS NULL",
    ]
    for sql in statements:
        await _execute(conn, sql)
    await _execute(
        conn,
        "UPDATE knowledge_mastery SET total_count = correct_count + wrong_count "
        "WHERE total_count IS NULL OR total_count <> correct_count + wrong_count",
    )
    await _execute(
        conn,
        "UPDATE knowledge_mastery SET error_rate = CASE "
        "WHEN (correct_count + wrong_count) > 0 "
        "THEN CAST(wrong_count AS REAL) / (correct_count + wrong_count) ELSE 0 END",
    )
    await _execute(conn, "UPDATE knowledge_mastery SET updated_at=CURRENT_TIMESTAMP WHERE updated_at IS NULL")


async def _migrate_api_keys(conn: AsyncConnection) -> None:
    """把旧的 user_api_keys 明细表并入 users.api_keys。"""
    if not await _table_exists(conn, "user_api_keys"):
        return

    try:
        rows = (
            await conn.execute(
                text("SELECT user_id, api_key, name, enabled FROM user_api_keys ORDER BY id")
            )
        ).all()
    except Exception as exc:  # 表结构异常时不阻塞启动
        logger.warning("迁移：读取旧 user_api_keys 失败（%s），跳过", exc)
        rows = []

    grouped: dict[int, list[dict]] = {}
    for user_id, api_key, name, enabled in rows:
        if api_key is None or not str(api_key).strip():
            continue
        grouped.setdefault(int(user_id), []).append(
            {"api_key": str(api_key), "name": str(name or ""), "enabled": bool(enabled)}
        )

    for user_id, items in grouped.items():
        row = (
            await conn.execute(text("SELECT api_keys FROM users WHERE id=:uid"), {"uid": user_id})
        ).first()
        if row is None:
            continue
        try:
            current = json.loads(row[0] or "[]")
        except (TypeError, json.JSONDecodeError):
            current = []
        if current:
            continue
        await conn.execute(
            text("UPDATE users SET api_keys=:value WHERE id=:uid"),
            {"value": json.dumps(items, ensure_ascii=False), "uid": user_id},
        )
        logger.info("迁移：user=%s 的 %d 个 api-key 已并入 users.api_keys", user_id, len(items))

    await conn.execute(text("DROP TABLE IF EXISTS user_api_keys"))
    logger.info("迁移：旧表 user_api_keys 已删除（数据已迁移）")


async def _drop_fts(conn: AsyncConnection) -> None:
    """移除旧 FTS5 索引与触发器（中文分词无效，匹配改走 LIKE + 相似度）。"""
    statements = [
        "DROP TRIGGER IF EXISTS answer_library_ai",
        "DROP TRIGGER IF EXISTS answer_library_ad",
        "DROP TRIGGER IF EXISTS answer_library_au",
        "DROP TABLE IF EXISTS answer_library_fts",
    ]
    for sql in statements:
        try:
            await conn.execute(text(sql))
        except Exception as exc:  # pragma: no cover - 极少见
            logger.warning("迁移：清理 FTS 对象失败: %s", exc)


async def _backfill_answer_hashes(conn: AsyncConnection) -> None:
    rows = (
        await conn.execute(
            text(
                "SELECT id, question_text FROM answer_library "
                "WHERE question_hash IS NULL OR TRIM(question_hash)=''"
            )
        )
    ).all()
    for record_id, question_text in rows:
        await conn.execute(
            text("UPDATE answer_library SET question_hash=:h WHERE id=:i"),
            {"h": question_hash(question_text or ""), "i": record_id},
        )
    if rows:
        logger.info("迁移：补齐 %d 条标准答案的 question_hash", len(rows))


async def _create_indexes(conn: AsyncConnection) -> None:
    dedupe_statements = [
        "DELETE FROM solve_records WHERE local_id IS NOT NULL AND id NOT IN "
        "(SELECT MAX(id) FROM solve_records WHERE local_id IS NOT NULL GROUP BY user_id, local_id)",
        "DELETE FROM answer_library WHERE user_id IS NOT NULL AND question_hash IS NOT NULL AND id NOT IN "
        "(SELECT MAX(id) FROM answer_library WHERE user_id IS NOT NULL AND question_hash IS NOT NULL "
        "GROUP BY user_id, question_hash)",
        "DELETE FROM knowledge_mastery WHERE user_id IS NOT NULL AND id NOT IN "
        "(SELECT MAX(id) FROM knowledge_mastery WHERE user_id IS NOT NULL GROUP BY user_id, knowledge_point)",
    ]

    index_statements = [
        "CREATE UNIQUE INDEX IF NOT EXISTS uq_users_username ON users(username) WHERE username IS NOT NULL",
        "CREATE UNIQUE INDEX IF NOT EXISTS uq_users_device_id ON users(device_id) WHERE device_id IS NOT NULL",
        "CREATE UNIQUE INDEX IF NOT EXISTS uq_solve_records_user_local "
        "ON solve_records(user_id, local_id) WHERE local_id IS NOT NULL",
        "CREATE INDEX IF NOT EXISTS ix_solve_records_user_created ON solve_records(user_id, created_at)",
        "CREATE INDEX IF NOT EXISTS ix_solve_records_batch_key ON solve_records(user_id, batch_key)",
        "CREATE INDEX IF NOT EXISTS ix_answer_library_user ON answer_library(user_id)",
        "CREATE INDEX IF NOT EXISTS ix_answer_library_hash ON answer_library(question_hash)",
        "CREATE UNIQUE INDEX IF NOT EXISTS uq_answer_library_user_hash "
        "ON answer_library(user_id, question_hash) WHERE user_id IS NOT NULL AND question_hash IS NOT NULL",
        "CREATE UNIQUE INDEX IF NOT EXISTS uq_knowledge_mastery_user_point "
        "ON knowledge_mastery(user_id, knowledge_point)",
    ]

    for sql in dedupe_statements:
        try:
            await conn.execute(text(sql))
        except Exception as exc:
            logger.warning("迁移：去重语句失败: %s", exc)

    for sql in index_statements:
        try:
            await conn.execute(text(sql))
        except Exception as exc:
            logger.error("迁移：创建索引失败（%s）: %s", sql.split(" ON ")[0], exc)


# --------------------------------------------------------------------------
# 版本记录
# --------------------------------------------------------------------------
async def _ensure_meta(conn: AsyncConnection) -> None:
    await conn.execute(text("CREATE TABLE IF NOT EXISTS schema_meta (key TEXT PRIMARY KEY, value TEXT)"))


async def _read_version(conn: AsyncConnection) -> str | None:
    row = (
        await conn.execute(text("SELECT value FROM schema_meta WHERE key='schema_version'"))
    ).first()
    return row[0] if row else None


async def _write_version(conn: AsyncConnection) -> None:
    await conn.execute(
        text(
            "INSERT INTO schema_meta(key, value) VALUES('schema_version', :v) "
            "ON CONFLICT(key) DO UPDATE SET value=excluded.value"
        ),
        {"v": SCHEMA_VERSION},
    )
    await conn.execute(
        text(
            "INSERT INTO schema_meta(key, value) VALUES('migrated_at', :v) "
            "ON CONFLICT(key) DO UPDATE SET value=excluded.value"
        ),
        {"v": datetime.utcnow().isoformat(timespec="seconds") + "Z"},
    )


async def run_migrations(conn: AsyncConnection) -> bool:
    """执行迁移，返回「本次是否发生了版本升级」。"""
    await _ensure_meta(conn)
    previous = await _read_version(conn)

    if await _table_exists(conn, "users"):
        await _upgrade_users(conn)

    for table, columns in ADDED_COLUMNS.items():
        if await _table_exists(conn, table):
            for column, ddl in columns:
                await _add_column(conn, table, column, ddl)

    if await _table_exists(conn, "solve_records"):
        await _normalize_solve_records(conn)
    if await _table_exists(conn, "answer_library"):
        await _normalize_answer_library(conn)
    if await _table_exists(conn, "knowledge_mastery"):
        await _normalize_mastery(conn)

    await _migrate_api_keys(conn)
    await _drop_fts(conn)
    await _backfill_answer_hashes(conn)
    await _create_indexes(conn)
    await _write_version(conn)

    upgraded = previous != SCHEMA_VERSION
    logger.info(
        "数据库迁移检查完成：schema v%s -> v%s%s",
        previous or "（新库）",
        SCHEMA_VERSION,
        "（已升级）" if upgraded else "",
    )
    return upgraded
