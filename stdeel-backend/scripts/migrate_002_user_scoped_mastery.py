"""一次性迁移脚本: 为 knowledge_mastery 添加 user_id 列, 并把现有行绑定到系统用户下。

用法:
    python scripts/migrate_002_user_scoped_mastery.py
"""

import sqlite3
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
DB_PATH = PROJECT_ROOT / "app.db"
SYSTEM_USERNAME = "__system__"


def main() -> int:
    if not DB_PATH.exists():
        print(f"[migrate_002] 数据库不存在: {DB_PATH}, 跳过迁移(初始化时会自动建表)")
        return 0

    conn = sqlite3.connect(DB_PATH)
    cur = conn.cursor()

    # 1) 找到(或创建)系统用户
    cur.execute("SELECT id FROM users WHERE username = ?", (SYSTEM_USERNAME,))
    row = cur.fetchone()
    if row is None:
        cur.execute(
            "INSERT INTO users (username, device_id) VALUES (?, ?)",
            (SYSTEM_USERNAME, SYSTEM_USERNAME),
        )
        sys_user_id = cur.lastrowid
        print(f"[migrate_002] 创建系统用户 id={sys_user_id}")
    else:
        sys_user_id = row[0]
        print(f"[migrate_002] 复用系统用户 id={sys_user_id}")

    # 2) 检查 user_id 列是否存在
    cur.execute("PRAGMA table_info(knowledge_mastery)")
    cols = {r[1] for r in cur.fetchall()}
    if "user_id" not in cols:
        cur.execute("ALTER TABLE knowledge_mastery ADD COLUMN user_id INTEGER")
        print("[migrate_002] 添加列 knowledge_mastery.user_id")
    else:
        print("[migrate_002] 列 knowledge_mastery.user_id 已存在, 跳过 ADD COLUMN")

    # 3) 把 user_id 为空的行绑定到系统用户
    cur.execute("SELECT COUNT(*) FROM knowledge_mastery WHERE user_id IS NULL")
    null_count = cur.fetchone()[0]
    if null_count > 0:
        cur.execute(
            "UPDATE knowledge_mastery SET user_id = ? WHERE user_id IS NULL",
            (sys_user_id,),
        )
        print(f"[migrate_002] 已将 {null_count} 条 mastery 绑定到系统用户 id={sys_user_id}")
    else:
        print("[migrate_002] 没有 user_id 为空的 mastery, 无需绑定")

    # 4) 如果旧表有单独 UNIQUE(knowledge_point), 重建表以切换为联合唯一
    cur.execute("SELECT sql FROM sqlite_master WHERE type='table' AND name='knowledge_mastery'")
    create_sql = (cur.fetchone() or [""])[0] or ""
    upper_sql = create_sql.upper()
    if "UNIQUE" in upper_sql and "KNOWLEDGE_POINT" in upper_sql and "USER_ID" not in upper_sql:
        print("[migrate_002] 检测到旧 UNIQUE(knowledge_point) 约束, 重建表以切换为联合唯一")
        cur.execute("PRAGMA foreign_keys=off")
        cur.execute("ALTER TABLE knowledge_mastery RENAME TO knowledge_mastery__legacy")
        cur.execute(
            """
            CREATE TABLE knowledge_mastery (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER,
                knowledge_point VARCHAR(255) NOT NULL,
                correct_count INTEGER DEFAULT 0,
                wrong_count INTEGER DEFAULT 0,
                total_count INTEGER DEFAULT 0,
                error_rate FLOAT DEFAULT 0.0,
                updated_at DATETIME,
                CONSTRAINT uq_knowledge_mastery_user_point UNIQUE (user_id, knowledge_point)
            )
            """
        )
        cur.execute(
            """
            INSERT INTO knowledge_mastery
                (id, user_id, knowledge_point, correct_count, wrong_count, total_count, error_rate, updated_at)
            SELECT id, user_id, knowledge_point, correct_count, wrong_count, total_count, error_rate, updated_at
            FROM knowledge_mastery__legacy
            """
        )
        cur.execute("DROP TABLE knowledge_mastery__legacy")
        cur.execute("PRAGMA foreign_keys=on")
        print("[migrate_002] 表重建完成")
    else:
        print("[migrate_002] 无需重建表")

    conn.commit()
    conn.close()
    print("[migrate_002] 完成")
    return 0


if __name__ == "__main__":
    sys.exit(main())
