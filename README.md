# DDNS 监控面板

基于 Python、FastAPI 和 SQLite 的单管理员 DNS 监控面板。定期查询指定域名的 A/AAAA 记录，展示当前 IP、解析耗时、失败状态、IP 变动时间线和历史记录；可通过 Telegram 发送 IP 变动、持续失败与恢复通知。支持自选 DNS 解析器、检查间隔、手动检查和检查记录保留时间。

本项目用于观察 DDNS 解析结果，不负责向 DNS 服务商更新记录。

## 索引与聚合

首页图表和今日检查统计读取 `check_hourly` 小时汇总表，因此读取成本随小时桶数增长，而不随检查明细条数增长。小时桶采用本地整点，适用于 UTC+5:30 等非整小时偏移时区。每次检查将明细与汇总计数在同一事务写入；`(domain_id, hour_ts)` 主键用于累加，`hour_ts` 索引支持首页时间范围查询。升级已有数据库时，启动过程按域名和小时分组回填历史检查，并用数据库设置标记保证回填只执行一次；中断时回填和标记会一起回滚，运行时区变化后会重新分桶。DST 回拨时重复的本地时钟小时合并到第一次出现的桶，合并计数保持总量正确。

检查记录仍按“设置 → 数据保留”中的天数精确清理，IP 变动记录不受影响。清理会删除过期汇总小时，并从尚在保留期内的明细重算 cutoff 所在小时；小时汇总不会延长明细记录的保留期。汇总表大约每个有检查活动的域名每小时占一行，建议保留默认 30 天或按实际排障需求缩短；例如 100 个持续活动域名约产生 72,000 个小时桶。减少保留天数后，下次计划的清理任务会同步收缩汇总表。

清理在后台线程使用独立 SQLite 连接，删除明细、删除过期汇总与边界小时重算在同一事务完成，失败全部回滚；WAL 允许清理期间继续读取页面，但其他写入仍可能等待 SQLite 写锁（最多 5 秒）。退出时会等待正在执行的清理事务结束并关闭连接。删除域名及其检查、汇总和变动记录也使用同一事务，失败全部保留。


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
| `TRUSTED_PROXIES` | `172.16.0.0/12` | 可信 TCP 对端 IP/CIDR，逗号分隔；只有来自这些地址的转发头才会用于登录限速与 HTTPS 判定。空字符串表示完全忽略转发头。生产应按实际对端缩小范围。 |
| `TRUSTED_PROXY_HOPS` | `1` | 可信代理的层数，从 XFF 右侧按固定层数定位客户端；CDN + 本机反代通常为 `2`。 |
| `TG_PROXY` | 未设置（直连） | Telegram API 的代理，例如 `socks5://192.168.1.1:1080` 或 `http://192.168.1.1:8080`；可在 Compose 的 environment 中配置。 |
| `COOKIE_SECURE` | `true` | 会话与 CSRF Cookie 是否要求 HTTPS。`0` / `false` / `no` / `off`（忽略大小写及首尾空格）明确关闭；其他值及空值均保持开启。关闭仅限内网调试。 |

Compose 已透传 `TRUSTED_PROXIES`、`TRUSTED_PROXY_HOPS` 和 `COOKIE_SECURE`，可在仓库 `.env` 或启动命令中设置。`TG_PROXY` 需取消 Compose 中的注释并填写；`DDNS_DATA_DIR` 自定义需自行加入 environment。不要把 Bot Token 写进仓库。

## 反代与客户端地址

**只能运行一个 worker、一个应用实例。** 调度器、域名锁、通知队列与登录限速计数均在进程内；请求和监控共享一个 SQLite 连接，后台清理另用独立连接。多个 worker 各有一套状态，会重复检查/通知，限速也会被分摊，不能靠 SQLite 文件共享实现协调。不要使用 `--workers 2`、多副本或生产环境 `--reload`。镜像命令已固定 `--workers 1 --no-proxy-headers`。

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

首次启动将随机密码写入 `data/initial_password.txt`，不会输出到日志。使用自定义数据目录时位置随之变化。登录后在“设置 → 修改密码”填写当前密码和两次新密码并保存；当前密码错误返回 400 并在字段旁提示，不修改任何设置。新密码须为 8–1024 位；成功后轮换会话签名密钥，所有旧会话失效，需要重新登录，初始密码文件会被删除。登录仅限制最多 1024 位，已有不足 8 位的密码仍可登录。

登录在解析表单前拒绝声明 `Content-Length` 超过 16 KiB 的请求（413）；无此头的 chunked 请求仍在密码校验前检查 1024 位上限，超长密码返回 400。登录验证与改密共用最多 4 个 scrypt 运算名额，满额立即返回 429，不排队；请求取消后名额保留到实际运算结束。分 IP 登录限速和全局满额的 429 响应均带 `Retry-After: 60`，提示稍后重试。XFF 定位出的客户端必须是合法 IPv4/IPv6，链不完整、中间跳不可信或客户端条目无效时均退回 TCP 对端计数。

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

## 表单与通知设置

DNS 服务器必须填写有效的 IPv4 或 IPv6 地址；校验失败会保留输入并显示字段错误。监控项切换记录类型发生冲突时，也会保留备注、间隔等编辑草稿。

启用 Telegram 通知和发送测试消息都需要有效配置中的 Bot Token 与 Chat ID。Token 留空沿用已保存值；清除 Token 需先关闭通知，清除选项优先于同时填写的新 Token。关闭通知时允许保存不完整草稿。“发送测试消息”会先校验并保存表单，再使用同一份配置发送。状态徽标反映已保存配置，“已启用”表示开关开启且凭据齐全，发送结果以测试消息为准。

## 浏览器安全

所有响应（包括静态资源、重定向和错误）带 `X-Content-Type-Options: nosniff`、`X-Frame-Options: DENY`、`Referrer-Policy: no-referrer`、`Permissions-Policy: geolocation=(), microphone=(), camera=()` 与以下 CSP：

```text
default-src 'self'; img-src 'self' data:; style-src 'self' 'unsafe-inline'; script-src 'self'; base-uri 'none'; form-action 'self'; frame-ancestors 'none'; object-src 'none'
```

脚本由本站静态文件加载，禁止内联脚本。`style-src` 保留 `'unsafe-inline'`，因为时间线位置和宽度随数据动态生成内联样式，无法使用固定 hash。认证页面与 HTML 响应使用 `Cache-Control: no-store`；静态资源沿用原有缓存行为。

仅请求本身为 HTTPS，或可信代理链确认 `X-Forwarded-Proto` 为 HTTPS 时，才发送 `Strict-Transport-Security: max-age=31536000; includeSubDomains`。紧邻应用的反代必须覆盖协议头，或由可信代理逐跳追加；追加模式按 `TRUSTED_PROXY_HOPS` 从右侧定位入口协议，忽略额外的客户端前缀。多层链路同时要求 XFF 链完整且中间跳可信；直连 HTTP 的伪造协议头不会开启 HSTS。

已认证的所有 POST 表单使用双提交 CSRF token，并在写库、查 DNS 或发通知之前校验。GET HTML 页面首次生成随机 `ddns_csrf` Cookie（HttpOnly、SameSite=Lax，Secure 跟随 `COOKIE_SECURE`），表单用隐藏字段 `_csrf` 回传，服务端与 Cookie 做常量时间比较。缺失或不匹配返回 403 HTML；刷新页面后重试。使用 curl 等脚本直接 POST 时，现在需要先 GET 表单页保存 Cookie，并将隐藏字段 `_csrf` 原样随表单提交。`/login` 豁免 CSRF：本应用只有单管理员，登录不会提升已有用户权限，且已有分 IP 限速；因此登录不依赖预先获取 token。SameSite=Lax 继续提供额外防护。

## 数据权限与备份

应用启动连接数据库时会将数据目录收紧为 **0700**、`ddns.db` / `ddns.db-wal` / `ddns.db-shm` 收紧为 **0600**，新建文件也使用私有权限。权限调整失败会记录告警并继续启动；应检查日志并在宿主机修正属主、挂载或文件系统权限。实际不可写的目录仍会导致 SQLite 无法启动。数据库包含密码 hash、会话签名密钥和明文 Telegram Bot Token，备份应按同样的敏感程度保护。

建议定期停服务后备份整个 `data/`，使用上面相同的 tar 命令，再启动服务。**不要在运行中只复制 `ddns.db`**：尚未 checkpoint 的提交可能在 `ddns.db-wal` 中；随意分别复制 DB/WAL/SHM 也可能得到不一致快照。无法停机时，用 SQLite 在线备份 API 或支持一致性快照的备份方案。

恢复时先停服务，将备份恢复到 `data/`，保留匹配的数据库及 WAL 文件，执行 `sudo chown -R 10001:10001 data`，确认目录 0700、文件 0600，再启动。备份和初始密码不要进入 Git、镜像构建上下文或公开下载目录。

镜像内置健康检查：每 30 秒用 Python 标准库访问 `http://127.0.0.1:8300/healthz`（200 为健康），启动宽限 10 秒，单次超时 5 秒，连续失败 3 次标记 unhealthy；无需 curl。该端点验证 HTTP 与 SQLite 连接上的 `SELECT 1` 查询，连接或查询失败返回 503。无需登录，仅公开 `status`，不暴露版本、路径或异常细节，也不加入页面导航。此只读探针不保证数据库可写或数据完整性，不能检出所有锁定或损坏情形。

仍未覆盖的安全限制：chunked 大请求在表单解析前缺少统一体积上限；修改密码时的当前密码尝试尚无独立次数限速（已有 scrypt 并发上限）。

## 测试与门禁

使用 Python 3.13，在虚拟环境中安装运行及开发依赖后执行：

```bash
.venv/bin/python -m pip install -r requirements.txt -r requirements-dev.txt
.venv/bin/python -m pytest -q
.venv/bin/python scripts/check_pages.py
.venv/bin/python scripts/check_contrast.py
# 使用全新临时目录，避免改动现有数据；造数会写入显式指定的目录。
DDNS_BENCH_DIR=$(mktemp -d /tmp/ddns-bench-XXXXXX)
.venv/bin/python scripts/seed_demo.py --data-dir "$DDNS_BENCH_DIR" --changes 2000
.venv/bin/python scripts/bench_detail.py --data-dir "$DDNS_BENCH_DIR" --domain-id 1 --days 90 --requests 10
```

页面门禁始终创建独立临时目录并覆盖外部 `DDNS_DATA_DIR`，退出时自动清理，不会写入生产库。详情性能门禁要求 p95 < 200ms；CI 使用 2000 条 changes，完整压测可将造数改为 `--changes 10000`。GitHub Actions 只做测试与门禁，不发布或部署。

视觉门禁需要 Node.js（支持内置 fetch）、Playwright 模块及能在本机启动的 Chromium（含所需系统库），依赖齐备后运行：

```bash
export PYTHON=.venv/bin/python
export PLAYWRIGHT_MODULE=/absolute/path/to/node_modules/playwright
export CHROME_BIN=/absolute/path/to/chromium
node scripts/visual.mjs
# 使用相同环境变量，运行原生表单提交、错误定位、分页和触屏时间线回归：
node scripts/check_interactions.mjs
```

也可通过 `NODE_PATH` 提供 Playwright；特殊浏览器依赖通过 `LD_LIBRARY_PATH` 指定。脚本自行创建临时数据并启动服务，检查明暗主题 × 320/390/768/1024/1440px，截图保存到 `artifacts/ui/`；浏览器不可用时退出码为 2。默认 CI 不安装浏览器、不运行视觉门禁，需在具备这些前置条件的环境另行执行。

交互回归脚本同样自行创建和清理临时数据库及本机服务，DNS 与 Telegram 使用模拟实现。它包含一次 31 秒慢请求检查；结果与截图写入 `artifacts/ui-interactions/`。
