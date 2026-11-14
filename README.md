# 思谛 STDeel 后端

客户端直连 AI 推理，本仓库只负责**账号归属与数据同步**：设备/用户名注册、解题记录上传与拉取、
题目状态（反馈）、删除同步、知识点统计、标准答案库、用户 AI Key 跨端同步。

- 技术栈：FastAPI + SQLAlchemy(async) + SQLite（WAL）
- 契约来源：`backend_requirements.md`（v0.9.11），接口细节见 [docs/API.md](docs/API.md)
- 验收自测：[scripts/acceptance_test.py](stdeel-backend/scripts/acceptance_test.py)（覆盖需求第 8 章 13 条 + 2 条附加）

## 目录

```
stdeel-backend/
├── main.py            # 应用入口：建表 → 迁移 → 路由 → /healthz /webui /webui/login
├── config.py          # 环境变量配置
├── database.py        # 引擎/会话（SQLite WAL）
├── models.py          # ORM
├── schemas.py         # Pydantic 请求/响应模型
├── services.py        # 幂等 upsert、掌握度重算、答案匹配
├── migrations.py      # 旧库幂等升级（v1 → v2）
├── middleware.py      # 任意路径前缀适配 + 管理台鉴权 + 访问日志
├── adminauth.py       # 管理台密码哈希 / 会话令牌 / 登录限流 / set-password CLI
├── routers/           # users / solve_records / answer_library / knowledge / files
├── scripts/           # 验收自测、部署脚本、历史迁移脚本
└── webui/             # 管理台页面（index.html）与登录页（login.html）
```

## 本地运行

```bash
cd stdeel-backend
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
uvicorn main:app --host 0.0.0.0 --port 8500
```

环境变量（均可选）：

| 变量 | 默认值 | 说明 |
|---|---|---|
| `STDEEL_DB_PATH` | `./app.db` | SQLite 文件路径 |
| `STDEEL_UPLOAD_DIR` | `./uploads` | 图片上传目录 |
| `STDEEL_BATCH_DEDUPE_WINDOW` | `900` | 批量兜底去重窗口（秒） |
| `STDEEL_MATCH_THRESHOLD` | `0.85` | 答案匹配阈值 |
| `STDEEL_CORS_ORIGINS` | `*` | 允许的来源 |
| `STDEEL_ADMIN_AUTH_FILE` | `./admin_auth.json` | 管理台凭据文件（只存加盐哈希） |
| `STDEEL_ADMIN_SESSION_TTL` | `12` | 管理台登录保持小时数 |
| `STDEEL_ADMIN_COOKIE` | `stdeel_admin` | 管理台会话 Cookie 名 |
| `STDEEL_ADMIN_LOGIN_MAX_FAILURES` | `5` | 登录失败几次后锁定 |
| `STDEEL_ADMIN_LOGIN_LOCK` | `900` | 锁定秒数 |
| `LOG_LEVEL` | `INFO` | 日志级别 |

## 验收自测

```bash
# 用临时库起一个实例，避免污染正式数据
STDEEL_DB_PATH=/tmp/stdeel-test.db uvicorn main:app --port 8599 &
python3 scripts/acceptance_test.py --base-url http://127.0.0.1:8599
python3 scripts/acceptance_test.py --base-url http://127.0.0.1:8599/api/v1   # 前缀模式
```

## 管理台鉴权（输密码才能进入）

`/webui`（管理台）、`/docs`、`/redoc`、`/openapi.json` 以及管理端 JSON 接口
（`GET /users`、`GET /users/stats`、`GET /users/{id}`、`GET /users/{id}/records`、
`GET /users/{id}/mastery`、`DELETE /users/{id}`）都需要先输管理密码。

**密码怎么设**：只存加盐 PBKDF2-SHA256 哈希，明文不落盘、不进日志、不进命令行参数。
在服务器上用服务账号（`www`）执行，按提示输入两次：

```bash
sudo -u www /www/server/pyporject_evn/3.11.2venv/bin/python \
    /www/wwwroot/stdeel-backend/stdeel-backend/adminauth.py set-password
```

- 文件是 `admin_auth.json`（0600，已在 `.gitignore`，部署脚本会保留并收回权限）。
- 写完立即生效（按文件 mtime 自动重载），**不用重启服务**；旧会话全部失效。
- 改密码 = 直接再跑一次 `set-password`；查看状态用 `adminauth.py status`。
- **未配置密码时管理台一律拒绝访问**（fail-closed），登录页会显示设置命令。
- 登录失败 5 次锁 15 分钟（按来源 IP，Cloudflare Tunnel 下取 `CF-Connecting-IP`）。
- 客户端同步接口（注册、解题记录、答案库、知识点、`/users/api-key`）**不受影响**。

## 部署（宝塔 Python 项目）

线上路径：`/www/wwwroot/stdeel-backend/stdeel-backend`，进程由宝塔「Python 项目」以 `www` 用户拉起：

```bash
uvicorn main:app --host 0.0.0.0 --port 8500 --workers 1
```

升级步骤见 [stdeel-backend/scripts/deploy.sh](stdeel-backend/scripts/deploy.sh)：
停服 → 备份 → 更新代码 → 恢复数据库 → 语法自检 → `chown www:www` → 启动 → 健康检查。
管理台凭据 `admin_auth.json` 不随代码更新，部署脚本会保留它并把权限收回 0600。

数据库变更由 `main.py` 的 lifespan 自动执行（`migrations.run_migrations`，幂等）。

## 数据与备份

- `app.db` 及其 `-wal` / `-shm` 属于运行时数据，**不入版本库**（见 `.gitignore`）。
- 部署脚本会在 `/root/stdeel-backend-backups/<时间戳>/` 留一份完整备份。
