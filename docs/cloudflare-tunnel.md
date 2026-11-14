# Cloudflare Tunnel 接入 runbook（思谛 STDeel 后端）

> 目标：把本机 `http://127.0.0.1:8500` 的思谛后端，通过 Cloudflare Tunnel 暴露成**一个** HTTPS 子域名；
> 不开放任何入站端口、不动 nginx、不动宝塔面板。
>
> 本机：snserver（Debian 12 / cloudflared 2026.8.2 / systemd 252 / 内网 192.168.2.60）。
>
> 状态（2026-11-14 11:37）：**已开通** —— `https://stdeel.snserver.dpdns.org` → `127.0.0.1:8500`，
> tunnel `stdeel` = `54baabb0-54ed-44cd-bab2-57205fc1a71e`，QUIC 4 条连接（lax08/10/13），systemd 开机自启。
> 开通记录、证书获取踩坑与运维见 §0.1–§0.2；QUIC/HTTP2 连通性实测见附录 A。
> 管理台密码登录已落地并部署（commit 81d5653 + 8a572ec，见 §2.1）；客户端同步 API 仍无鉴权（见 §2.2）。
> 所有需要 root 的命令都用 `sudo`（本机 sudo 需要密码，由 Lead 执行）。
>
> 安全说明：本文不包含任何 cert.pem / credentials / token 的内容。文中 `<...>` 是占位符，**必须替换成真实值**再执行，
> 不能原样粘贴进 shell（bash 会把 `<` 当重定向）。

## 0. 结论速览

| 项 | 结论 |
|---|---|
| cloudflared 安装 | 已装：`/usr/local/bin/cloudflared` → `/usr/bin/cloudflared`（dpkg 包 `cloudflared`，版本 2026.8.2） |
| 部署状态 | ✅ 已装 systemd 单元、`/etc/cloudflared/config.yml`、cert.pem 与隧道凭据，服务 enabled（2026-11-14） |
| 出网 / 隧道连通性 | ✅ 通过（quick tunnel 实测） |
| QUIC（出站 UDP 7844） | ✅ 可用：官方预检 `UDP Connectivity: QUIC connection successful`，实连 `protocol=quic` |
| HTTP/2 回退（出站 TCP 443） | ✅ 可用：强制后 `protocol=http2`，公网 GET 200 |
| 暴露面 | 只暴露 1 个子域名 → 127.0.0.1:8500；5480（宝塔）、5470（DSH）、80/443（nginx）都不进 ingress |
| 管理面鉴权 | ✅ 2026-11-14 已上线「管理台密码登录」：`/webui`、`/docs`、`/redoc` 未登录 303 跳登录页，`/openapi.json` 与管理端 JSON 接口未登录 401；未配置密码时 fail-closed。见 §2.1 |
| 客户端 API 鉴权 | ❌ 仍无（注册、解题记录、答案库、知识点、`/users/api-key` 都靠 `user_id`，契约如此）→ 拿到域名的人可读写同步数据，风险见 §2.2 |
| 决策 | ① 域名 = `stdeel.snserver.dpdns.org`；② 不用 Access，只靠后端密码；③ 8500 保持 `0.0.0.0` 不动（内网使用不受影响） |

### 0.1 当前线上状态（2026-11-14 已开通）

| 项 | 值 |
|---|---|
| 公网地址 | `https://stdeel.snserver.dpdns.org`（Cloudflare 代理，HTTP/2 200） |
| Cloudflare zone | `snserver.dpdns.org`（NS = archer/wanda.ns.cloudflare.com） |
| 隧道 | 名称 `stdeel`，ID `54baabb0-54ed-44cd-bab2-57205fc1a71e`（凭据 `/root/.cloudflared/<ID>.json`，root 600） |
| DNS | `stdeel.snserver.dpdns.org` → CNAME `<TUNNEL_ID>.cfargotunnel.com`（橙云代理） |
| 配置 | `/etc/cloudflared/config.yml`（由 `scripts/cloudflare/config.yml.example` 生成，`ingress validate` OK） |
| 服务 | `systemctl status cloudflared` → active (running) / enabled；ExecStart：`cloudflared --no-autoupdate --config /etc/cloudflared/config.yml tunnel run` |
| 协议 | QUIC（出站 UDP 7844），4 条连接分布 lax08/lax10/lax13 |
| 本机指标 | `curl -s http://127.0.0.1:20241/ready` → `readyConnections` |
| 线上验证 | `/healthz` 200 + `version=2.0.0`；`/webui` 303 → `/webui/login`；`/openapi.json`、`/users` 401；客户端 API 200/404（未被拦） |

> **遗留**：Cloudflare 账号里还有一条 2026-11-13 创建、从未连过的隧道 `STDeel`（`78167f8f-dc30-4425-b3d7-f9582aabc9e7`）。
> 本次已用 `route dns --overwrite-dns` 把 DNS 指到新隧道；确认没有别的服务依赖后，可在 Zero Trust → Networks → Tunnels 里删掉。

### 0.2 证书获取踩坑（重要）

本机网络对 `login.cloudflareaccess.org`（Tunnel 证书回调域名）**做 SNI 重置**：
`cloudflared tunnel login` 能打开授权页并完成授权，但随后下载证书时必定报
`read: connection reset by peer`，证书写不进 `/root/.cloudflared/cert.pem`。

- 实测：`curl https://login.cloudflareaccess.org/` → 连接被重置；`login.cloudflareaccess.com` 正常（但证书下载仍写死 `.org`）。
- `--callbackURL https://login.cloudflareaccess.com/` **不能绕过**：它只改授权页里的回调参数，cloudflared 取证书时仍连 `login.cloudflareaccess.org`。
- **可行做法**：授权后浏览器会直接下载 `cert.pem`，把该文件手动放到 `/root/.cloudflared/cert.pem`
  （`chmod 600`、root:root），之后 `cloudflared tunnel list` 即正常，不用重新登录。
- 注意：DSH 的安全网会拦截 agent 访问任何 `.pem` 路径（`secret.ext.pem`），所以这一步**必须由人操作**。

### 0.3 日常运维

```bash
systemctl status cloudflared --no-pager     # 是否在线
sudo journalctl -u cloudflared -n 50        # 连接/错误日志（Registered tunnel connection ... protocol=quic）
curl -s http://127.0.0.1:20241/ready        # 本机探活：readyConnections ≥ 1
sudo cloudflared tunnel info stdeel         # 连接列表
sudo systemctl restart cloudflared          # 改完 /etc/cloudflared/config.yml 后重启生效
sudo cloudflared tunnel --config /etc/cloudflared/config.yml ingress validate   # 改配置先校验
```

## 1. 拓扑与各组件位置

```text
客户端 / 浏览器
   │  HTTPS 443（证书由 Cloudflare 边缘签发）
   ▼
Cloudflare Edge
   │  加密隧道：出站 QUIC/UDP 7844（或回退 HTTP/2/TCP 443），本机不需要开放任何入站端口
   ▼
cloudflared（本机 systemd 服务） ── HTTP ──► 127.0.0.1:8500 uvicorn（思谛后端）
```

| 组件 | 位置 | 说明 |
|---|---|---|
| cloudflared 可执行文件 | `/usr/bin/cloudflared`（`/usr/local/bin` 有软链） | 由 dpkg 管理，升级用 apt；配置里建议写 `/usr/local/bin/cloudflared` |
| 登录证书 | `/root/.cloudflared/cert.pem` | `tunnel login` 生成；只用来管理 tunnel/DNS，**不要外传** |
| 隧道凭据 | `/root/.cloudflared/<TUNNEL_ID>.json` | `tunnel create` 生成；权限 600；**不要提交进版本库** |
| 隧道配置 | `/etc/cloudflared/config.yml` | 由模板 `scripts/cloudflare/config.yml.example` 生成 |
| systemd 单元 | `/etc/systemd/system/cloudflared.service` | 由 `cloudflared service install` 生成，或用手写模板 |
| 管理台认证配置 | 应用目录 `admin_auth.json`（线上 `/www/wwwroot/stdeel-backend/stdeel-backend/admin_auth.json`，0600、`www:www`） | 密码哈希 + 会话签名密钥；已 gitignore，`deploy.sh` 会保留并修正权限；**不要读/贴/提交，也不要写进备份文档示例** |
| 日志 | journald：`journalctl -u cloudflared` | 手写单元也可另加 `--logfile` |

## 2. 安全前提（做之前必读）

### 2.1 管理面：已有密码登录

2026-11-14 起后端自带管理台鉴权（`adminauth.py` + `AdminAuthMiddleware`）：

- `/webui`、`/docs`、`/redoc`：未登录 303 跳 `/webui/login`；
- `/openapi.json`、`GET /users`、`GET /users/stats`、`GET /users/{id}`、
  `GET /users/{id}/records`、`GET /users/{id}/mastery`、`DELETE /users/{id}`：未登录 401 JSON；
- 未配置密码时 fail-closed（页面仍跳登录页并提示设置，接口 503）；
- 密码只存加盐 PBKDF2-SHA256 哈希（`admin_auth.json`，权限 0600、属主 `www:www`）；登录失败 5 次锁 15 分钟。
- `admin_auth.json` 位于应用目录（线上 `/www/wwwroot/stdeel-backend/stdeel-backend/admin_auth.json`），
  已被 `stdeel-backend/.gitignore` 忽略，`deploy.sh` 会保留它并把权限修正为 `www:www 0600`。
  文件 mtime 变化会自动重载——**改密码后旧会话立即失效，且无需重启服务**。
  它只含哈希与会话签名密钥：**不要读、不要打印、不要提交、不要写进任何备份/文档示例**。

**首次设置密码**（隧道开通前先做掉；公网来源无法设置，原因见下方说明）：

- 内网浏览器打开 `http://192.168.2.60:8500/webui` → 页面显示「首次使用：设置管理密码」→ 输两次；
- 或服务器 CLI：`sudo -u www /www/server/pyporject_evn/3.11.2venv/bin/python /www/wwwroot/stdeel-backend/stdeel-backend/adminauth.py set-password`。

> 设计上 `POST /webui/setup` 只接受本机/内网直连（带 `CF-Connecting-IP`/`cdn-loop` 或非私有 IP 一律拒绝），
> 所以**不能**等隧道通了再从公网设置密码——那会被拒绝，必须先在局域网/本机设好。

### 2.2 客户端 API：仍无鉴权（已知风险，用户已接受）

`docs/API.md` §0 写明客户端接口「无 Token / Cookie；身份仅由 `user_id` 表达」，代码里也没有 Authorization 校验。
用户 2026-11-14 明确选择：**不加 Cloudflare Access，只靠后端密码**。这意味着：

- 「后端密码」只保护**管理台/文档/管理端接口**，保护不了 `/users/register`、`/users/api-key`、
  `/solve-records*`、`/answer-library*`、`/knowledge*`、`/files*`、`/uploads*` 这些客户端路径。
- 任何拿到子域名的人都能注册账号、按 `user_id` 读/写同步数据（`user_id` 是 `u_1` 这种可枚举值）。
- **最敏感的一处**：`GET /users/api-key?user_id=u_N` 会回传该用户已保存的 AI Provider Key，而 `user_id` 可枚举
  （`u_1`、`u_2`…）——等于「拿到域名就能批量拉 AI Key」。用户已知悉并接受该弱模型；
  若要收敛，**优先给这个接口加客户端令牌**，再考虑整域名套 Access。
- 缓解手段（按性价比）：
  1. **域名不公开传播**（域名即弱凭据）；不写进公开文档/截图/群聊。
  2. Cloudflare 侧对客户端路径加 **Rate limiting**（Zero Trust/WAF 规则），至少挡批量枚举。
  3. 长期根治：给客户端 API 加 per-user API Key / 签名，再考虑整域名套 Access（需要改客户端）。
  4. 8500 目前监听 `0.0.0.0`：局域网内设备可直连绕过隧道；如无局域网客户端依赖，
     可改宝塔启动参数为 `--host 127.0.0.1`（本次未改，避免影响现有局域网使用）。

另外：Cloudflare Tunnel 不需要在防火墙上放行任何**入站**端口；只需要出站 UDP 7844（QUIC）和/或 TCP 443（HTTP/2）。
不要把 5480（宝塔）、5470（DSH）、22（SSH）写进 ingress —— 隧道能到 127.0.0.1 的任何端口，写错就是直接暴露。

## 3. 前置检查

```bash
# 1) 版本（实测 2026.8.2）
cloudflared --version

# 2) 出网 & 协议预检：启动时会打印 precheck 结果，QUIC / HTTP2 / API 三项都应为 pass
#    （本次已实测全部 pass，见附录 A；需要复测时按附录 A 的临时 quick tunnel 方法，禁止指向 8500）

# 3) 域名是否已托管到 Cloudflare（拿到用户域名后执行；NS 必须指向 *.ns.cloudflare.com）
dig +short NS <你的域名>
# 期望输出形如：xxx.ns.cloudflare.com / yyy.ns.cloudflare.com

# 4) 确认端口现状：8500 是后端，80/443 是宝塔 nginx，别混用
ss -lntp | grep -E ':(80|443|8500|5480|5470)\s'

# 5) Cloudflare 账号：能登录 dash.cloudflare.com 即可（命名隧道不依赖 Zero Trust 套餐；
#    用户已决定不用 Access，one.dash.cloudflare.com 只是可选）

# 6) 管理台密码必须在开隧道前配好，否则管理面接口 fail-closed
curl -s -o /dev/null -w '/openapi.json -> %{http_code}\n' http://127.0.0.1:8500/openapi.json
# 期望 401（已配置）；503 = 还没设密码 → 先按 §2.1 设好再继续
```

> `cloudflared tunnel login` 依赖浏览器。本机无桌面浏览器时，把命令打印出的 URL 复制到**任意设备**的浏览器打开授权即可。

## 4. 第一步：登录并拿到 cert.pem

```bash
sudo cloudflared tunnel login
```

- 命令会打印一个 `https://dash.cloudflare.com/argotunnel?...` 链接；在浏览器打开 → 登录 Cloudflare → **选择要用的域名（zone）** → 授权。
- 成功后证书落在 **`/root/.cloudflared/cert.pem`**（因为是 sudo 跑的）。
- 验证：

```bash
sudo ls -l /root/.cloudflared/cert.pem
# 期望：-rw------- 1 root root ... cert.pem
```

**为什么用 login，而不是把 dashboard 生成的 tunnel token 贴进命令行？**

- token 是**长期有效凭据**，一旦贴到命令行就会进 `~/.bash_history`、`ps aux`、systemd 单元文件、截图和聊天记录；
  后续维护（创建/删除 tunnel、加 DNS 记录）还得回到 Dashboard 复制。
- `login` 只是用浏览器做一次 OAuth 式授权，把证书写到本机 `/root/.cloudflared/cert.pem`（root-only，600）；
  之后 `tunnel create / route dns / list / delete` 全都靠这个本地证书完成，**凭据不再经过命令行和日志**。
- 例外：如果以后改用 Dashboard 托管隧道（token 方式），token 也必须写进 root-only 文件（`service install <TOKEN>` 会落到
  `/etc/cloudflared/token`），不要贴进命令行或文档。

> 选错域名怎么办：`sudo rm /root/.cloudflared/cert.pem` 后重新 `login`，授权时会让你重新选 zone。

## 5. 第二步：创建隧道 + 加 DNS

```bash
# 1) 创建隧道（名字固定用 stdeel）。输出会打印 TUNNEL_ID（UUID）和 credentials 文件路径
sudo cloudflared tunnel create stdeel

# 2) 确认创建成功，记下 UUID
sudo cloudflared tunnel list

# 3) 把子域名 CNAME 到隧道（会自动创建一条 proxied CNAME：<TUNNEL_ID>.cfargotunnel.com）
sudo cloudflared tunnel route dns stdeel <DOMAIN>
```

- `<DOMAIN>` 是完整子域名，例如 `stdeel.example.com`、`api.example.com`；**必须在该 zone 内**。
- `route dns` 创建的记录是「橙云」代理状态（必须代理，隧道才能接管）。
- 验证 DNS（生效通常几秒到 1 分钟）：

```bash
dig +short <DOMAIN>
# 期望返回 Cloudflare 的 Anycast IPv4（如 104.x / 172.67.x），不是 192.168.2.60
```

> 注意：`tunnel route dns` 只能**创建**记录，没有删除子命令；回滚时在 Dashboard → DNS 里删（见 §11）。

## 6. 第三步：写 /etc/cloudflared/config.yml

用仓库里的模板生成（`<TUNNEL_ID>`、`<DOMAIN>` 替换成真实值）。以下命令假定当前目录为 `/home/sn/DSH Workspace`；在服务器部署路径 `/www/wwwroot/stdeel-backend/` 下，模板位于 `stdeel-backend/scripts/cloudflare/`：

```bash
sudo mkdir -p /etc/cloudflared
sudo cp stdeel-work/stdeel-backend/scripts/cloudflare/config.yml.example /etc/cloudflared/config.yml
sudo chmod 644 /etc/cloudflared/config.yml
sudo vi /etc/cloudflared/config.yml   # 替换 <TUNNEL_ID> 与 <DOMAIN>
```

配置要点（模板里每项都有注释）：

| 键 | 值 | 作用 |
|---|---|---|
| `tunnel` | `<TUNNEL_ID>` | 指定隧道（写 UUID 比写名字稳，重名时不会歧义） |
| `credentials-file` | `/root/.cloudflared/<TUNNEL_ID>.json` | `tunnel create` 生成的凭据，服务以 root 运行所以要 root 可读 |
| `protocol` | `quic`（默认） | 出站 UDP 7844；被墙时改 `http2`（出站 TCP 443） |
| `no-autoupdate` | `true` | 关闭自动更新，避免服务被自动重启；升级交给 apt |
| `metrics` | `127.0.0.1:20241` | 本机健康/指标端点（`/ready`、`/metrics`），只监听本机 |
| `ingress` | 见下 | 路由规则，**最后一条必须是 catch-all**，否则 cloudflared 拒绝启动 |

ingress 只需要两条：

```yaml
ingress:
  - hostname: <DOMAIN>
    service: http://127.0.0.1:8500
  - service: http_status:404   # 兜底：其它 Host/路径一律 404，不落任何本地服务
```

- 只写 8500 一个 origin；不要加 5480/5470/80/443。
- 无 `path` 规则 = 整个子域名都转发到 8500（后端中间件本身支持任意路径前缀，客户端 Base URL 可以带 `/api/v1`）。
- 校验配置（不需要先启服务）：

```bash
sudo cloudflared tunnel --config /etc/cloudflared/config.yml ingress validate
# 期望输出：Validating rules from /etc/cloudflared/config.yml / OK
# 也可以用 ingress rule 测试某条 URL 命中哪条规则：
# sudo cloudflared tunnel --config /etc/cloudflared/config.yml ingress rule https://<DOMAIN>/healthz
```

## 7. 第四步：安装为 systemd 服务（开机自启）

### 方式 A（推荐）：用 cloudflared 自带命令生成

```bash
sudo cloudflared service install      # 自动找 /etc/cloudflared/config.yml 并生成 systemd 单元
sudo systemctl enable --now cloudflared
sudo systemctl status cloudflared --no-pager
```

- `service install` 会生成 `/etc/systemd/system/cloudflared.service`（`ExecStart` 里带 `--config /etc/cloudflared/config.yml tunnel run`），
  并且**自动更新**机制会把二进制升级后重启服务；如不希望这样，用 `sudo cloudflared service install --no-update-service`。
- 若报 "could not find any config file with credentials"，说明 `/etc/cloudflared/config.yml` 不在或 `credentials-file` 路径不可读 —— 回 §6 检查。
- 查实际生成的单元：`systemctl cat cloudflared`。

### 方式 B：用手写单元（模板：`scripts/cloudflare/cloudflared.service.example`）

```bash
sudo cp stdeel-work/stdeel-backend/scripts/cloudflare/cloudflared.service.example /etc/systemd/system/cloudflared.service
sudo systemctl daemon-reload
sudo systemctl enable --now cloudflared
sudo systemctl status cloudflared --no-pager
```

两种方式二选一，不要同时装。`cloudflared service uninstall` 只认方式 A 生成的单元。

### 日志与自启

```bash
sudo journalctl -u cloudflared -n 50 --no-pager     # 最近 50 行
sudo journalctl -u cloudflared -f                   # 跟踪
systemctl is-enabled cloudflared                    # 期望 enabled
```

正常启动的关键日志（脱敏示例）：

```text
INF Starting tunnel tunnelID=<TUNNEL_ID>
INF Registered tunnel connection connIndex=0 connection=<uuid> event=0 ip=<edge-ip> location=lax protocol=quic
INF Connection <uuid> registered connIndex=0 ...
```

看到 `Registered tunnel connection ... protocol=quic`（或 `http2`）就说明隧道已连上 Cloudflare 边缘。

## 8. 第五步：验证（从本机 + 从公网各一遍）

```bash
# 1) 源站先自证（本机直连，不经过隧道）
curl -sS -i http://127.0.0.1:8500/healthz
# 期望：HTTP/1.1 200 OK，body {"status":"ok","version":"2.0.0"}

# 2) 公网验证（从本机走公网 DNS，也可用手机 4G/热点复核）
curl -sS -i https://<DOMAIN>/healthz
# 期望：HTTP/2 200，server: cloudflare，body {"status":"ok","version":"2.0.0"}

# 2b) 只看响应头（-I 不取 body；版本号在 body 里，所以两者一起看）
curl -I https://<DOMAIN>/healthz
# 期望：HTTP/2 200 / server: cloudflare

# 只看状态码：curl -s -o /dev/null -w '%{http_code}\n' https://<DOMAIN>/healthz

# 3) 隧道侧状态：连接数应为 1+，并带边缘位置
sudo cloudflared tunnel info stdeel
curl -s http://127.0.0.1:20241/ready && echo

# 4) 管理面密码保护（当前决定：不用 Access，所以看到的是后端自己跳登录页）
curl -s -o /dev/null -w '%{http_code} %{redirect_url}\n' https://<DOMAIN>/webui
# 期望：303，redirect_url 是 https://<DOMAIN>/webui/login?next=/webui
curl -s -o /dev/null -w '%{http_code}\n' https://<DOMAIN>/openapi.json
# 期望：401（503 = 管理密码还没配置，回 §2.1）
# 浏览器打开 https://<DOMAIN>/webui 应看到登录页，输密码后能进

# 4b) 若将来启用了 Access（§9），/webui 的期望改为 302 到 cloudflareaccess；
#     过完 Access 还会再要后端密码（双因子，属预期）。

# 5) 客户端 API 必须直达（不能被 Access/密码拦）：应 200/4xx，而不是 302 到登录页
curl -s -o /dev/null -w '%{http_code}\n' https://<DOMAIN>/healthz
```

> `/healthz` 返回 200 且 body 带版本号，是「隧道通 + 源站活」的最小验证。
> 如果公网 200 但内容不对，问题在源站；如果 502/1033，问题在隧道侧或源站没起（见 §10）。

## 9. Cloudflare Zero Trust Access（管理面保护）

**当前决定（2026-11-14）：不加 Access，只用后端密码。** 本节保留为可选加固方案，
等以后要「整域名套 Access」或「管理台单独子域名」时再启用。

Access 只保护**匹配到的路径**：即使加了，也只能保护 `/webui*`、`/docs*` 这类路径，
业务 API（客户端要直连）不能套，否则手机端会被 302 到登录页导致同步失败。

### 9.1 开启一次性邮箱验证码（One-time PIN）

Zero Trust → Settings → Authentication → Login methods，确认 **One-time PIN** 为 enabled（默认就有）。
这样被 Access 拦住的请求，会要求输入邮箱 → 收 6 位验证码 → 通过，不需要额外 IdP。

### 9.2 建三个 Self-hosted 应用（路径用尾部通配）

Zero Trust → Access → Applications → **Add an application → Self-hosted**：

| Application name | Public hostname | Path | Policy |
|---|---|---|---|
| STDeel 管理台 | `<DOMAIN>` | `/webui*` | Allow → Include → Emails → 用户邮箱 |
| STDeel API 文档 | `<DOMAIN>` | `/docs*` | 同上（或合并进管理台应用） |
| STDeel 文档附加 | `<DOMAIN>` | `/redoc*`、`/openapi.json` | 同上 |

- Path 支持尾部 `*` 通配（`/webui*` 同时覆盖 `/webui` 与 `/webui/`）；如果当前面板不接受通配，
  就为 `/webui`、`/docs` 各建一条**精确**应用，再用 §8 的 curl 逐条确认 302。
- Policy 里 **Include = Emails = 用户自己的邮箱**（多邮箱就多行）；Action = Allow。
- 建议 Session duration 设 24h，别设「永久」。
- **不要**给业务 API 路径建应用：`/users*`、`/solve-records*`、`/answer-library*`、`/knowledge*`、
  `/files*`、`/uploads*`、`/healthz` 都不要被 Access 覆盖，否则手机客户端会被 302 到登录页而同步失败。
- 建完用 §8 第 4b、5 条复验：`/webui` 302 到 cloudflareaccess；`/healthz` 200。

### 9.3 进阶（等以后整域名套 Access / 拆独立管理子域名时）

- 给 API 也套 Access 时，客户端需要带 `CF-Access-Client-Id` / `CF-Access-Client-Secret`（Service Token）；
  当前客户端「不带自定义 Header」，所以现在做不了。
- 若未来把管理台拆到独立子域名（`admin.<domain>`，ingress 里多加一条 hostname 规则），
  就能「整条 Host 都套 Access」，比按路径保护更干净。多一条 hostname 到同一个 8500 是可以的（多暴露一个 Host，不是多暴露端口），
  但要先和用户确认。

## 10. 排错清单

| 现象 | 可能原因 | 处理 |
|---|---|---|
| 启动日志 `failed to dial to edge with quic` / `timeout: no recent network activity` | 出站 UDP 7844 被运营商/防火墙/本机代理封掉 | 先看启动 precheck 的 `UDP Connectivity`；不通就改用 `protocol: http2`（或 `cloudflared tunnel ... --protocol http2`），只走 TCP 443 |
| 日志 `protocol=http2` 但速度慢/偶发断 | UDP 被干扰后自动回退或手动 http2 | 正常现象；国内网络建议优先试 quic，不行再 http2。本机实测两者都通 |
| 公网 502 Bad Gateway / 1033 | 源站没起、端口写错、uvicorn 崩了 | `curl 127.0.0.1:8500/healthz`；`ss -lntp` 里 8500 的监听；看宝塔 Python 项目进程与 `journalctl -u cloudflared` 里的 origin 错误 |
| 公网 530 / 1033 / DNS 未生效 | DNS 记录没建、zone 不是 Active、代理没开 | `dig +short <DOMAIN>`；Dashboard → DNS 确认 CNAME 指向 `<TUNNEL_ID>.cfargotunnel.com` 且橙云开启；等待 1–5 分钟 |
| `tunnel create` 报 `failed to fetch certificate` / 无 cert.pem | 没 login / cert 在别人家目录 | `sudo cloudflared tunnel login`；确认 `/root/.cloudflared/cert.pem` 存在；普通用户跑的话在 `~/.cloudflared/` |
| `service install` 报找不到 config/credentials | `/etc/cloudflared/config.yml` 缺失或 `credentials-file` 路径错 | 按 §6 重建；`sudo cloudflared tunnel --config ... ingress validate`；确认 JSON 文件存在且 root 可读 |
| systemd 起不来，`status=1` / 立即退出 | ingress 缺 catch-all、YAML 缩进错、UUID 写错 | `sudo journalctl -u cloudflared -n 30`；`ingress validate`；注意 YAML 用空格不用 Tab |
| 访问 /webui 显示「尚未配置管理密码」/ 接口 503 | 管理密码没设（fail-closed） | 内网直连 `http://192.168.2.60:8500/webui` 设置，或服务器 CLI（§2.1）；设置后无需重启 |
| 登录页提示「失败次数过多」 | 5 次失败锁 15 分钟（按真实 IP，隧道下看 CF-Connecting-IP） | 等 15 分钟或换网络；密码忘了用 `adminauth.py set-password` 重置 |
| 登录后反复跳回登录页 | Cookie 没带住：Secure Cookie/HTTPS 判断、代理层改写 `X-Forwarded-Proto` | 统一用 https 域名访问；不要混用内网 IP 与公网域名；无痕窗口复测 |
| 部署后管理台登录失效 | `admin_auth.json` 属主/权限被改（应为 `www:www` 0600） | `sudo chown www:www <应用目录>/admin_auth.json && sudo chmod 600 ...`；`deploy.sh` 会自动修正 |
| 上传图片失败 | 后端限制 10MB（`STDEEL_MAX_UPLOAD_BYTES`）；Cloudflare 免费版请求体上限 100MB | 小于 10MB 即可；大文件分片或调后端上限 |
| 本机同时开 TUN/全局代理 | 全局代理可能劫持 cloudflared 的 UDP/TCP 出站 | 在代理规则里让 `*.argotunnel.com`、`*.cloudflare.com` 直连，或直接用 `protocol: http2` |
| 宝塔 nginx 干扰 | 不会：隧道不走 80/443，是 cloudflared 主动出站 | 无需改 nginx；不要在 ingress 里写 80/443 |

日志位置汇总：

- systemd：`sudo journalctl -u cloudflared -f`（方式 A/B 默认都在 journald）
- 手写单元如果加了 `--logfile /var/log/cloudflared.log`：`sudo tail -f /var/log/cloudflared.log`
- 配置/凭据：`/etc/cloudflared/config.yml`、`/root/.cloudflared/{cert.pem,<TUNNEL_ID>.json}`
- 连接页：Dashboard → Networks → Tunnels → stdeel（看 connector 在线状态）

## 11. 回滚（按顺序：停服务 → 删 DNS → 删隧道 → 清配置/凭据）

```bash
# 1) 停服务并取消开机自启
sudo systemctl disable --now cloudflared

# 2) 卸掉服务（方式 A 生成的单元）；手写单元则直接删文件
sudo cloudflared service uninstall || true
sudo rm -f /etc/systemd/system/cloudflared.service
sudo systemctl daemon-reload

# 3) 删配置（凭据/cert.pem 先留着，删隧道还要用 cert.pem）
sudo rm -rf /etc/cloudflared
```

4. **删 DNS**（Dashboard → 该 zone → DNS）：删除 CNAME `<DOMAIN> → <TUNNEL_ID>.cfargotunnel.com`。
   `cloudflared` 没有 `route dns` 的删除子命令，只能走 Dashboard 或 API。

5. **删隧道**（`/root/.cloudflared/cert.pem` 还在就行；先停服务，避免连接未释放）：

```bash
sudo cloudflared tunnel delete stdeel        # 若报还有活动连接：sudo cloudflared tunnel delete -f stdeel
sudo cloudflared tunnel list                 # 确认 stdeel 已不在列表
```

6. **清本地凭据**（可选；删掉 cert.pem 后要重新 `login` 才能再管理隧道）：

```bash
sudo rm -f /root/.cloudflared/<TUNNEL_ID>.json
# sudo rm -f /root/.cloudflared/cert.pem
```

回滚后验证：`https://<DOMAIN>` 应无法访问（或返回 Cloudflare 1033/522），`systemctl status cloudflared` 应提示单元不存在/未运行。

## 附录 A：本机实测记录（2026-11-14）

目的：验证 cloudflared 能否建立到 Cloudflare 边缘的隧道（QUIC 与 HTTP/2 两条路），以及公网访问是否真的能通。
**测试只用了临时 `127.0.0.1:18080` 的 python http.server，绝不把 8500 暴露到公网；验证后进程与临时地址立即清理。**

环境：cloudflared 2026.8.2、Python 3.11.2、出口为家庭宽带直连（无代理环境变量）。
方法：quick tunnel（`cloudflared tunnel --url ...`，Cloudflare 分配 `*.trycloudflare.com` 临时域名，无需登录，最适合做连通性验证）。

### A.1 QUIC（默认，出站 UDP 7844）

关键日志（已脱敏，临时域名用 `<random>` 代替）：

```text
INF precheck component="DNS Resolution" ... status=pass target=region1.v2.argotunnel.com
INF precheck component="UDP Connectivity" details="QUIC connection successful" status=pass
INF precheck component="TCP Connectivity" details="HTTP/2 connection successful" status=pass
INF precheck component="Cloudflare API" details="API is reachable" status=pass target=api.cloudflare.com:443
INF precheck complete hard_fail=false suggested_protocol=quic
INF Registered tunnel connection connIndex=0 ... ip=198.41.192.67 location=lax12 protocol=quic
```

公网访问结果：`GET https://<random>.trycloudflare.com/` → **HTTP 200**，body `STDEEL-CF-VERIFY-OK`。
结论：QUIC 可用（UDP 7844 出站正常），Cloudflare 建议协议也是 quic。

### A.2 HTTP/2（强制回退路径）

`cloudflared tunnel --url ... --protocol http2`：

```text
INF Registered tunnel connection connIndex=0 ... ip=198.41.192.47 location=lax07 protocol=http2
```

公网访问结果：**HTTP 200**，body `STDEEL-CF-VERIFY-OK`。结论：TCP 443 回退可用。

### A.3 配置项验证：`protocol` 写在 config 文件里也生效

用临时 config（`protocol: http2`）+ quick tunnel 启动，日志显示 `Settings: map[... protocol:http2 ...]`，
连接注册为 `protocol=http2`。结论：模板里的 `protocol` 键有效，隧道被墙时可只改配置、不改命令行。

### A.4 清理确认

验证结束后已 `kill` 掉临时 http.server 与 cloudflared；确认 `ss -lnt` 无 18080 监听、`pgrep cloudflared` 无残留、
8500 未被本次验证触碰。临时日志已删除，未记录任何凭据。

### A.5 需要复测时（临时，禁止指向 8500）

```bash
# 1) 临时源站：只绑 127.0.0.1:18080
python3 -m http.server 18080 --bind 127.0.0.1 &
# 2) quick tunnel（约 5–10 秒后打印 https://<random>.trycloudflare.com）
cloudflared tunnel --url http://127.0.0.1:18080 --protocol quic --no-autoupdate --grace-period 1s
# 3) 另开一个终端验证后，回到第 2 步终端 Ctrl-C，并 kill 掉 http.server
```

## 附录 B：待用户/Lead 确认

1. ✅ 已定（2026-11-14）：域名 = `stdeel.snserver.dpdns.org`（zone `snserver.dpdns.org`），隧道已开通并验证。
2. ✅ 已定（2026-11-14）：管理台密码已由主人自行设置（公开 `/openapi.json` 返回 401，说明已生效）。
3. ✅ 已定（2026-11-14）：不加 Cloudflare Access，只靠后端管理密码；接受客户端 API 公开的弱模型（见 §2.2，尤其 `/users/api-key` 可枚举回传 AI Key）。
4. ✅ 已定（2026-11-14）：8500 暂保持 `0.0.0.0`（不影响隧道；局域网客户端可能依赖，后续再评估收窄）。
5. ✅ 已定（2026-11-14）：用 `cloudflared service install --no-update-service`，版本升级交给 apt，不自动重启。
6. 提醒：`admin_auth.json`（0600、`www:www`、已 gitignore、`deploy.sh` 保留）**不要读/贴/提交，也不要写进任何备份文档示例**。
