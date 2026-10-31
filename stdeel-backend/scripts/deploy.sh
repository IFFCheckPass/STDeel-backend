#!/usr/bin/env bash
# 思谛 STDeel 后端 · 宝塔部署脚本
#
# 用法（在服务器上以 root 运行）：
#     sudo bash scripts/deploy.sh
#     sudo bash scripts/deploy.sh /path/to/local/clone     # 指定本地代码源（无法联网拉取时）
#
# 做的事：停服 → 备份 → 更新代码 → 恢复数据库 → 修权限 → 启动 → 健康检查。
# 任何一步失败都会停下并打印日志位置，不会静默继续。
set -euo pipefail

REPO_DIR="/www/wwwroot/stdeel-backend"
APP_DIR="$REPO_DIR/stdeel-backend"
BRANCH="main"
PORT="8500"
SERVICE="stdeel-backend"
PID_FILE="/www/server/python_project/vhost/pids/${SERVICE}.pid"
START_SCRIPT="/www/server/python_project/vhost/scripts/${SERVICE}_cmd.sh"
BACKUP_ROOT="/root/stdeel-backend-backups"
TS="$(date +%Y%m%d-%H%M%S)"
BACKUP_DIR="$BACKUP_ROOT/$TS"
SOURCE_CLONE="${1:-}"

log() { printf '\n== %s ==\n' "$*"; }
fail() { printf '\n[失败] %s\n' "$*" >&2; exit 1; }

[ "$(id -u)" -eq 0 ] || fail "请用 root 运行：sudo bash $0"
[ -d "$REPO_DIR/.git" ] || fail "找不到仓库：$REPO_DIR"
[ -f "$START_SCRIPT" ] || fail "找不到宝塔启动脚本：$START_SCRIPT"

# ---------------------------------------------------------------------------
log "1/6 停止服务"
# 只认 cmdline 里带 main:app 的进程，避免 pid 复用误杀
collect_pids() {
  local pids=""
  [ -f "$PID_FILE" ] && pids="$(cat "$PID_FILE" 2>/dev/null || true)"
  local listening
  listening="$(ss -ltnp 2>/dev/null | grep -E ":$PORT[[:space:]]" | grep -o 'pid=[0-9]*' | cut -d= -f2 || true)"
  {
    printf '%s\n' "$pids"
    printf '%s\n' "$listening"
  } | grep -E '^[0-9]+$' | sort -u | while read -r pid; do
    if grep -qa "main:app" "/proc/$pid/cmdline" 2>/dev/null; then
      echo "$pid"
    fi
  done
}

STOPPED=0
for _ in 1 2 3 4 5; do
  PIDS="$(collect_pids)"
  if [ -z "$PIDS" ] && ! ss -ltn 2>/dev/null | grep -q ":$PORT "; then
    STOPPED=1
    break
  fi
  if [ -n "$PIDS" ]; then
    # shellcheck disable=SC2086
    kill $PIDS 2>/dev/null || true
  fi
  sleep 2
done

if [ "$STOPPED" -ne 1 ]; then
  PIDS="$(collect_pids)"
  if [ -n "$PIDS" ]; then
    echo "优雅停止超时，强制结束：$PIDS"
    # shellcheck disable=SC2086
    kill -9 $PIDS 2>/dev/null || true
    sleep 2
  fi
fi

if ! ss -ltn 2>/dev/null | grep -q ":$PORT "; then
  STOPPED=1
fi
[ "$STOPPED" -eq 1 ] || fail "端口 $PORT 仍被占用：请先在宝塔面板停止「$SERVICE」项目，然后重跑本脚本"
echo "服务已停止"

# ---------------------------------------------------------------------------
log "2/6 备份（代码 + 数据库）"
mkdir -p "$BACKUP_DIR"
tar czf "$BACKUP_DIR/project.tar.gz" -C /www/wwwroot stdeel-backend
cp -a "$APP_DIR"/app.db* "$BACKUP_DIR"/ 2>/dev/null || true
[ -f "$APP_DIR/app.db" ] && [ ! -f "$BACKUP_DIR/app.db" ] && fail "数据库备份失败，已中止"
echo "备份目录：$BACKUP_DIR"
ls -lh "$BACKUP_DIR"

# ---------------------------------------------------------------------------
log "3/6 更新代码"
cd "$REPO_DIR"
git config --global --add safe.directory "$REPO_DIR" >/dev/null 2>&1 || true
TARGET=""
if GIT_TERMINAL_PROMPT=0 timeout 40 git fetch origin "$BRANCH" >/dev/null 2>&1; then
  TARGET="origin/$BRANCH"
  echo "已从 origin 拉取"
elif [ -n "$SOURCE_CLONE" ] && [ -d "$SOURCE_CLONE/.git" ]; then
  git config --global --add safe.directory "$SOURCE_CLONE" >/dev/null 2>&1 || true
  git fetch "$SOURCE_CLONE" "$BRANCH"
  TARGET="FETCH_HEAD"
  echo "已从本地克隆拉取：$SOURCE_CLONE"
else
  fail "无法获取最新代码（origin 无凭据且未提供本地克隆路径）"
fi
git reset --hard "$TARGET"
[ -n "$TARGET" ] && git update-ref "refs/remotes/origin/$BRANCH" "$(git rev-parse HEAD)" 2>/dev/null || true
echo "当前提交：$(git log --oneline -1)"

# ---------------------------------------------------------------------------
log "4/6 恢复数据库"
# 新版本不再跟踪 app.db；reset --hard 可能删除它，这里用备份还原（含 WAL/SHM）
rm -f "$APP_DIR"/app.db "$APP_DIR"/app.db-wal "$APP_DIR"/app.db-shm
if [ -f "$BACKUP_DIR/app.db" ]; then
  cp -a "$BACKUP_DIR/app.db" "$APP_DIR"/
  # WAL 要一起还原；-shm 是共享内存索引，删掉让 SQLite 自己重建更安全
  [ -f "$BACKUP_DIR/app.db-wal" ] && cp -a "$BACKUP_DIR/app.db-wal" "$APP_DIR"/
  rm -f "$APP_DIR"/app.db-shm
  echo "数据库已还原：$(du -h "$APP_DIR/app.db" | cut -f1)"
else
  echo "备份中没有数据库（首次部署），将自动初始化"
fi

# ---------------------------------------------------------------------------
log "5/6 权限与缓存"
rm -rf "$APP_DIR"/__pycache__ "$APP_DIR"/routers/__pycache__
chown -R www:www "$REPO_DIR"
find "$APP_DIR" -type d -exec chmod 755 {} \;
find "$APP_DIR" -type f -name '*.py' -exec chmod 644 {} \;
[ -f "$APP_DIR/app.db" ] && chmod 664 "$APP_DIR/app.db"
echo "已归属 www:www"

# ---------------------------------------------------------------------------
log "6/6 启动并验证"
mkdir -p /www/wwwlogs/python/stdeel-backend
# 以 www 身份启动（与宝塔面板一致），进程归属正确才能被面板正常管理
su -s /bin/bash www -c "cd /tmp && bash '$START_SCRIPT'"

HEALTH_FILE="/tmp/stdeel-health-$$.json"
OK=0
for _ in $(seq 1 30); do
  sleep 1
  if curl -fsS -m 3 "http://127.0.0.1:$PORT/healthz" -o "$HEALTH_FILE" 2>/dev/null; then
    if grep -q '"version"' "$HEALTH_FILE"; then OK=1; break; fi
  fi
done

if [ "$OK" -eq 1 ]; then
  echo "健康检查通过：$(cat "$HEALTH_FILE")"
  echo
  echo "部署完成 ✔  备份在：$BACKUP_DIR"
  echo "管理台：http://127.0.0.1:$PORT/webui   接口文档：http://127.0.0.1:$PORT/docs"
  rm -f "$HEALTH_FILE"
  exit 0
fi

echo "健康检查失败，最近的错误日志：" >&2
tail -n 40 /www/wwwlogs/python/stdeel-backend/error.log >&2 || true
echo "回滚命令：sudo tar xzf $BACKUP_DIR/project.tar.gz -C /www/wwwroot && sudo chown -R www:www $REPO_DIR" >&2
rm -f "$HEALTH_FILE"
exit 1
