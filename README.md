# DDNS 监控面板

基于 Python、FastAPI 和 SQLite 的单管理员 DNS 监控面板。定期查询指定域名的 A/AAAA 记录，展示当前 IP、解析耗时、失败状态、IP 变动时间线和历史记录；可通过 Telegram 发送 IP 变动、持续失败与恢复通知。支持自选 DNS 解析器、检查间隔、手动检查和检查记录保留时间。

本项目用于观察 DDNS 解析结果，不负责向 DNS 服务商更新记录。

## 索引与聚合

首页图表和今日检查统计读取 `check_hourly` 小时汇总表，因此读取成本随小时桶数增长，而不随检查明细条数增长。小时桶采用本地整点，适用于 UTC+5:30 等非整小时偏移时区。每次检查将明细与汇总计数在同一事务写入；`(domain_id, hour_ts)` 主键用于累加，`hour_ts` 索引支持首页时间范围查询。升级已有数据库时，启动过程按域名和小时分组回填历史检查，并用数据库设置标记保证回填只执行一次；中断时回填和标记会一起回滚，运行时区变化后会重新分桶。DST 回拨时重复的本地时钟小时合并到第一次出现的桶，合并计数保持总量正确。

检查记录仍按“设置 → 数据保留”中的天数精确清理，IP 变动记录不受影响。清理会删除过期汇总小时，并从尚在保留期内的明细重算 cutoff 所在小时；小时汇总不会延长明细记录的保留期。汇总表大约每个有检查活动的域名每小时占一行，建议保留默认 30 天或按实际排障需求缩短；例如 100 个持续活动域名约产生 72,000 个小时桶。减少保留天数后，下次计划的清理任务会同步收缩汇总表。


## Docker Compose 部署

需要 Docker 与 Docker Compose 插件。进入仓库后，先准备持久化目录，再启动：

```bash
# 容器固定以 UID/GID 10001:10001 运行；绑定挂载会覆盖镜像内目录的属主。
sudo mkdir -p data
sudo chown -R 10001:10001 data
sudo chmod 700 data
docker compose up -d --build
docker compose logs --tail=50 ddns-monitor
sudo cat data/initial_password.txt
```

已有部署升级前先按下文备份，再停止旧容器、为 `data/` 及其文件设置上述属主，最后重新构建启动。不要通过 `chmod 777` 或改回 root 来绕过写权限问题。Rootless Docker / user namespace 映射场景中，宿主机属主应换成容器 UID 10001 映射后的宿主机 UID。镜像已预建属主正确、权限为 0700 的 `/app/data`；使用绑定挂载时仍需准备宿主机目录。

Compose 默认只将端口发布到 **127.0.0.1:8300**，应由 nginx 或 Caddy 在外部终止 TLS，通过 `https://你的域名` 访问。生产部署必须提供 HTTPS，并保留 `COOKIE_SECURE=true`。不要直接把 8300 暴露到公网。

例如，宿主机 Caddy 可使用以下 Caddyfile（替换域名，确保域名解析和证书签发条件满足）：

```caddyfile
monitor.example.com {
    reverse_proxy 127.0.0.1:8300
}
```

Caddy 默认向上游设置 X-Forwarded-For（XFF）。使用 nginx 时，在已配置证书的 HTTPS `server` 中加入：

```nginx
location / {
    proxy_pass http://127.0.0.1:8300;
    proxy_set_header Host $host;
    proxy_set_header X-Forwarded-Proto $scheme;
    # 单层反代覆盖客户端提交的 XFF，避免伪造来源。
    proxy_set_header X-Forwarded-For $remote_addr;
}
```

HTTP 监听应跳转至 HTTPS。外部访问 nginx/Caddy 的 TLS 入口，代理到同机应用的这一段可以使用 HTTP。

仅在本地或可信内网调试时，可显式关闭 Secure Cookie：

```bash
COOKIE_SECURE=false docker compose up -d
```

然后可在服务器本机用 `http://127.0.0.1:8300` 登录。恢复 HTTPS 后运行 `COOKIE_SECURE=true docker compose up -d`。默认 Secure Cookie 不会由浏览器通过普通 HTTP 回传，因此 HTTP 直连在默认配置下会出现登录后返回登录页。非 Secure 模式下登录页会显示明文登录警告。

## 环境变量

变量在进程启动时读取，修改后需要重建/重启进程；Compose 中修改环境变量后用 `docker compose up -d` 重建容器，单纯 `restart` 不会载入新的容器环境。

| 变量 | 默认值 | 用途 |
| --- | --- | --- |
| `DDNS_DATA_DIR` | 仓库下的 `data/`；镜像内为 `/app/data` | 数据库、WAL/SHM、初始密码存放目录。自定义时需保证应用用户可写，Docker 还需同步调整环境变量和卷挂载目的路径。 |
| `TRUSTED_PROXIES` | `172.16.0.0/12` | 可信 TCP 对端 IP/CIDR，逗号分隔；只有来自这些地址的 XFF 才会用于登录限速。空字符串表示完全忽略 XFF。生产应按实际对端缩小范围。 |
| `TRUSTED_PROXY_HOPS` | `1` | 可信代理的层数，从 XFF 右侧按固定层数定位客户端；CDN + 本机反代通常为 `2`。 |
| `TG_PROXY` | 未设置（直连） | Telegram API 的代理，例如 `socks5://192.168.1.1:1080` 或 `http://192.168.1.1:8080`；可在 Compose 的 environment 中配置。 |
| `COOKIE_SECURE` | `true` | 会话 Cookie 是否要求 HTTPS。`0` / `false` / `no` / `off`（忽略大小写及首尾空格）明确关闭；其他值及空值均保持开启。关闭仅限内网调试。 |

Compose 已透传 `TRUSTED_PROXIES`、`TRUSTED_PROXY_HOPS` 和 `COOKIE_SECURE`，可在仓库 `.env` 或启动命令中设置。`TG_PROXY` 需取消 Compose 中的注释并填写；`DDNS_DATA_DIR` 自定义需自行加入 environment。不要把 Bot Token 写进仓库。

## 反代与客户端地址

**只能运行一个 worker、一个应用实例。** 调度器、域名锁、通知队列与登录限速计数均在进程内；SQLite 使用进程内单个共享连接。多个 worker 各有一套状态，会重复检查/通知，限速也会被分摊，不能靠 SQLite 文件共享实现协调。不要使用 `--workers 2`、多副本或生产环境 `--reload`。镜像命令已固定 `--workers 1 --no-proxy-headers`。

推荐始终给 uvicorn 加上 `--no-proxy-headers`，由应用自己校验代理链。否则 uvicorn 先改写 TCP 对端，应用无法可靠判断 XFF 是否来自可信反代。本地进程运行示例（Python 3.13，与镜像一致）：

```bash
python3.13 -m venv .venv
.venv/bin/python -m pip install -r requirements.txt
TRUSTED_PROXIES=127.0.0.1/32,::1/128 .venv/bin/python -m uvicorn app.main:app \
  --host 127.0.0.1 --port 8300 --workers 1 --no-proxy-headers
```

不同部署方式看到的 TCP 对端不同：

| 链路 | `TRUSTED_PROXIES` | `TRUSTED_PROXY_HOPS` 与 XFF |
| --- | --- | --- |
| 浏览器 → 宿主机 nginx/Caddy → 同机 Python 进程 | 本机回环，如 `127.0.0.1/32,::1/128` | `1`；反代覆盖 XFF 为客户端 IP。 |
| 浏览器 → 宿主机反代 → 发布端口 → 应用容器 | 容器看到的 Docker 网桥网关地址，常见 `172.17.0.1/32`，以实际网络为准 | `1`；Compose 的默认 `172.16.0.0/12` 覆盖常见网桥，建议收窄为实际网关。 |
| 浏览器 → Docker 内的反代 → 应用容器 | 专用网络中反代容器的固定 IP 或受控子网 | `1`；同网络上游用 `http://ddns-monitor:8300`，独立配置网络，只有反代发布 80/443。 |
| 浏览器 → Cloudflare → nginx/Caddy → 应用 | 紧邻应用的反代地址（回环/网桥/反代容器地址） | `2`；XFF 应形如 `访客IP, Cloudflare回源IP`。应用会校验被跳过的中间地址属于可信网段或已知 Cloudflare 网段。 |
| 浏览器 → 应用，完全无反代（内网调试） | 空字符串 | 使用 TCP 对端，不采信 XFF；HTTP 调试需显式关闭 `COOKIE_SECURE`。 |

Cloudflare 两层链路要限制源站入口只允许 Cloudflare 回源，并让源站反代保留 Cloudflare 的 XFF、追加自己的 TCP 对端。例如 nginx 使用 `proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;`，同时避免 real_ip 模块提前替换 `$remote_addr` 导致层数变化。Caddy 则需配置可信 Cloudflare 代理网段以保留其 XFF，并核对实际输出链；不能直接套用单层示例后把层数改成 2。如果反代已将可信链归一化为单个访客 IP，则按单层配置 `TRUSTED_PROXY_HOPS=1`。不要保留任意公网请求提供的 XFF，也不要用 `0.0.0.0/0` 信任全部对端。

## 密码管理

首次启动将随机密码写入 `data/initial_password.txt`，不会输出到日志。使用自定义数据目录时位置随之变化。登录后在“设置 → 修改密码”填写两次新密码并保存；会话会失效，需要重新登录，初始密码文件会被删除。

忘记密码时，保留域名和历史数据，按以下步骤重置（不要删除整个数据库）：

```bash
# 停止唯一应用实例，避免后台任务和密码重置同时写库。
docker compose stop ddns-monitor
# 包含 WAL 的完整备份；备份也含密钥，限制目录访问。
sudo install -d -m 700 backups
sudo tar -czf "backups/data-before-reset-$(date +%Y%m%d-%H%M%S).tar.gz" data

# 使用相同数据卷、相同应用 UID 的临时容器，不启动 Web/调度器。
docker compose run --rm --no-deps -T ddns-monitor python - <<'PY'
import secrets
from app import db

c = db.conn()
with c:
    c.execute("DELETE FROM settings WHERE key = ?", ("password_hash",))
    c.execute("INSERT INTO settings (key, value) VALUES (?, ?) "
              "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
              ("auth_secret", secrets.token_hex(32)))
# 同时轮换签名密钥，使所有旧会话失效，再生成新的初始密码文件。
db.init()
PY

sudo cat data/initial_password.txt
docker compose up -d ddns-monitor
```

重新登录后立即修改密码。本地进程部署同样先停服务、备份，再在虚拟环境下运行上述 Python 代码；必须使用服务相同的 `DDNS_DATA_DIR` 和文件属主。不要在应用运行中单独删除 `password_hash`，也不要只重置 hash 却保留旧 `auth_secret`。

## 数据权限与备份

应用启动连接数据库时会将数据目录收紧为 **0700**、`ddns.db` / `ddns.db-wal` / `ddns.db-shm` 收紧为 **0600**，新建文件也使用私有权限。权限调整失败会记录告警并继续启动；应检查日志并在宿主机修正属主、挂载或文件系统权限。实际不可写的目录仍会导致 SQLite 无法启动。数据库包含密码 hash、会话签名密钥和明文 Telegram Bot Token，备份应按同样的敏感程度保护。

建议定期停服务后备份整个 `data/`，使用上面相同的 tar 命令，再启动服务。**不要在运行中只复制 `ddns.db`**：尚未 checkpoint 的提交可能在 `ddns.db-wal` 中；随意分别复制 DB/WAL/SHM 也可能得到不一致快照。无法停机时，用 SQLite 在线备份 API 或支持一致性快照的备份方案。

恢复时先停服务，将备份恢复到 `data/`，保留匹配的数据库及 WAL 文件，执行 `sudo chown -R 10001:10001 data`，确认目录 0700、文件 0600，再启动。备份和初始密码不要进入 Git、镜像构建上下文或公开下载目录。
