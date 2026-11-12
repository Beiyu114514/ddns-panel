# DDNS 管理后台 UI 整改报告

> 日期：2026-11-09　对应方案：`docs/ui-remediation-plan-2026-11-09.md`
> 初验：`docs/ui-acceptance-2026-11-09.md`（HOLD 56/100）
> 复验结论：**PASS（自评 92/100）**；P0 8/8、P1 25/25、P2 19/24 关闭。
> 2026-11-13 第 8 批复审：pytest **218 passed**、对比度 **76 项通过（含装饰线豁免）**、页面结构 5 页通过；视觉与性能均按本次实测更新，保留原复验结论。

## 1. 交付范围

- 全局令牌与布局：`app/static/style.css`、`app/templates/base.html`
- 总览：`app/templates/index.html`、`app/main.py` 的 `_build_chart` / `index`
- 域名详情：`app/templates/domain.html`、`app/main.py` 的 `_build_timeline` / `domain_detail`
- 添加域名、设置、登录：`new.html`、`settings.html`、`login.html`
- 后端契约：`app/db.py`（keyset 分页、统计、时长聚合、索引）、`app/util.py`（状态/空值过滤器）
- 验收工具：`scripts/check_contrast.py`、`scripts/check_pages.py`、`scripts/seed_demo.py`、`scripts/visual.mjs`
- 测试：`tests/conftest.py`、`tests/test_db.py`、`tests/test_app.py`

原报告范围外项目已由后续批次处理：2026-11-13 已增加双提交 CSRF token、检查锁与异步 DNS 查询；删除操作使用 `<details>` 二次确认，无内联 `onsubmit` 拼接。残余限制见第 5 节。

## 2. 关键实现

### 2.1 P0 关闭

| 编号 | 实现 | 证据 |
| --- | --- | --- |
| A2 | ≤760px 隐藏桌面导航，`<details>` 抽屉；`Esc`/点外关闭 | visual 320/390 无横向溢出；`base.html` |
| A3 | `--border` 改为 #6e7781；浅色 4.1:1、深色 3.77:1 | check_contrast 76 项通过（含装饰线豁免） |
| A7 | 深色主按钮用 `--accent-solid:#1f6feb`，白字 4.63:1 | check_contrast |
| B1 | 首屏改为 4 张统计卡 + 异常面板 + 监控项表，图表下移 | 2026-11-13 visual 监控项 top≈551–802px；320px 异常列表限高 180px 后从 943px 提前至 802px，保留滚动内容 |
| B5 | 图表 `min-width:640px`、轴标签 14px；320/390 实渲 11.79px | visual 10 组全 PASS |
| B6 | 图表单 `tabindex=0` + `role=group`，←/→ 虚拟光标，24 个 hit 不再各自停靠 | `tests/test_app.py`、visual 方向键断言 |
| C1 | 时间线 5 个日期刻度（起/25%/50%/75%/止） | `check_pages.py`、`test_app.py` |
| C2 | 时间线整轨 `tabindex=0`，逐段 ←/→/Home/End，`#tl-tip` live 提示；`details` 文本等价物 | visual 焦点环 3px、`domain.html` |

### 2.2 P1 关闭（25/25）

- 全局：A1 导航与 main 共用 1080px 栅格；A4 统一 `:focus-visible` 3px 焦点环；A5 复选框恢复原生 16px；A6 flash 改用正文色；A8 skip link；A9 `aria-current`；A10 五页独立 title；A17 统一状态标签与 `fmt_empty`。
- 总览：B2 异常优先 + “最近异常”；B3 统计卡只保留 4 个核心数字；B4 上下双图共享 x 轴、各自 y 轴；B7 `role=group`；B8 折线改令牌色；B9 空状态有说明和添加入口。
- 详情：C3 固定 12 色按首次出现分配 + 序号/文本兜底；C4 状态卡前置；C5 最近检查 20 条说明与空态；C6 长域名 `overflow-wrap:anywhere`；C7 `status_label` 中文标签。
- 新建：D1 8 档间隔下拉；D3 `data-pending` 禁用重复提交。
- 设置：E1 token 不回显，留空保留旧值；E2 字段级错误 + `aria-invalid` + `aria-describedby`。
- 登录：F1 h1 + 主机名；F2 `autocomplete="current-password"` + 无 autofocus。

### 2.3 后端契约

- `db.changes_list` 改 keyset：`?days=30&before=<ts>_<id>` / `after=<ts>_<id>`，20 条/页；`has_older`/`has_newer` 决定按钮 disabled。
- `db.change_stats` 返回 `{rows, changes, first_records, markers}`；页面统计与时间线计数同源。
- `db.change_durations` 用 `LEAD` 按 `new_ip` 聚合完整时长，图例时长不受 240 段采样影响。
- `build_timeline(domain_id, days, now)` 公开返回 window/bucket/segments/legend/ticks/counts；`app/main.py:658`。
- 新增索引 `idx_changes_dom_ts_id`、`idx_changes_dom_ts_ip`、`idx_checks_ts`；1 万条详情走 keyset，无 OFFSET 全表重扫。

### 2.4 表单与反馈

- 所有 POST 表单带 `data-pending`（base 1、index 3、domain 3、login 1、new 1、settings 3 个表单；settings 测试按钮另有一个）。
- flash 改 30 秒 httponly cookie + base64url(JSON)，一次消费即删；修复中文写 `Set-Cookie` 的 `UnicodeEncodeError`。
- 服务端校验失败原位渲染 400（创建/更新/通知/数据保留），保留用户输入并标字段。

## 3. 验收证据

2026-11-13 复审环境：`/tmp/ddnsvenv`（Python 3.13.5）；本机 Playwright 与 `/usr/bin/chromium`。性能数据为本机 TestClient 实测，受机器负载影响，不代表部署环境保证。以下命令可在已装依赖的 `.venv` 中运行。

```bash
.venv/bin/python -m pytest -q
.venv/bin/python scripts/check_contrast.py
.venv/bin/python scripts/check_pages.py
PYTHON=.venv/bin/python \
PLAYWRIGHT_MODULE=/absolute/path/to/node_modules/playwright \
CHROME_BIN=/usr/bin/chromium node scripts/visual.mjs
# 使用全新目录；2000 条对应 CI，10000 条对应完整压测。
DDNS_BENCH_DIR=$(mktemp -d /tmp/ddns-bench-XXXXXX)
.venv/bin/python scripts/seed_demo.py --data-dir "$DDNS_BENCH_DIR" --changes 10000
.venv/bin/python scripts/bench_detail.py --data-dir "$DDNS_BENCH_DIR" --domain-id 1 --days 90 --requests 10
```

| 门禁 | 结果 |
| --- | --- |
| pytest | **218 passed**（2026-11-13 复审；1 条 Starlette/httpx 弃用警告） |
| 对比度 | **76 项通过（含装饰线豁免）**；浅色按钮白字 5.2:1、深色主按钮 4.63:1、深色控件边框 3.77:1、焦点环 6.85:1、12 色 IP ≥3:1 |
| 页面结构 | 5 页 PASS：唯一 h1/title、lang、skip link、aria-current、autocomplete、图表 Tab、时间线 5 刻度/details、分页 disabled、8 档间隔、所有 POST pending |
| 视觉矩阵 | 2 主题 × 5 宽度（320/390/768/1024/1440）全 PASS；无横向溢出；轴标签实渲 11.79/12.34/17.06/18.09px；焦点环 3px；截图 20 张 |
| 性能 | 2026-11-13 最新 bench：`/domain/1?days=90`（TestClient，含模板渲染，10 次请求）；2000 条 min 18.1ms / median 18.3ms / **p95 21.8ms** / max 49.6ms；10000 条 min 39.2ms / median 44.0ms / **p95 69.5ms** / max 97.0ms，页面约 86KB；两组均通过 p95 < 200ms |
| 数据边界 | seed 精确 10000 / 240 / 241 行；marker、9 个 IP、90 天窗口 |

## 4. PASS 1–7

1. 首屏可见异常面板和「立即检查」，监控项表在图表之前：PASS。
2. 控件边界/焦点环 ≥3:1，深色文字 ≥4.5:1：PASS（76 项，含装饰线豁免）。
3. 390px 无横向溢出、导航折叠、表格卡片化且保留状态/操作：PASS。
4. 图表标签 ≥11px、单 Tab、方向键遍历、焦点清晰：PASS。
5. 时间线有 5 个日期刻度、可聚焦、details 文本等价：PASS。
6. 字段错误就近 + `aria-invalid`；所有 POST 有 pending：PASS。
7. 状态/空值术语统一（`status_label`/`fmt_empty`）：PASS。

## 5. 残余项与限制

| 项 | 状态 | 说明 |
| --- | --- | --- |
| A12 间距网格 | 部分 | 主要间距已收敛，个别 6/10px 仍存在 |
| A14 `:active`/过渡 | 部分 | hover/focus/disabled 已有，缺按压态 |
| C11 flash 统一 | 未做 | Cloudflare 卡内 warning 与全局 flash 仍是两种视觉 |
| D4 新建页 CF 说明 | 未做 | 仍只在按钮下方提示 |
| E5 密码显示切换 | 未做 | 已补 autocomplete 与字段校验，未加显示/隐藏按钮 |
| 截图人工目检 | 部分 | 2026-11-13 已查看 320px 首页截图，其他截图主要由自动门禁校验 |
| CSRF token 未签名 | 未覆盖 | 已有双提交与 SameSite，尚未签名或绑定会话 |
| chunked 大请求 | 未覆盖 | Content-Length 限制不覆盖 chunked 流式总量，表单解析前尚无统一体积上限 |
| 当前密码暴力尝试 | 未覆盖 | 改密验证有 scrypt 并发上限，尚无独立尝试次数限速 |

`artifacts/` 为本地生成物，已加入 `.gitignore`；如需归档证据，可在有图像能力的环境复核后另行提交。

## 6. 变更文件

```
.gitignore
app/db.py
app/main.py
app/static/style.css
app/templates/base.html
app/templates/domain.html
app/templates/index.html
app/templates/login.html
app/templates/new.html
app/templates/settings.html
app/util.py
scripts/check_contrast.py
scripts/check_pages.py
scripts/bench_detail.py
scripts/seed_demo.py
scripts/visual.mjs
tests/conftest.py
tests/test_db.py
tests/test_app.py
requirements-dev.txt
docs/ui-acceptance-2026-11-09.md
docs/ui-remediation-report-2026-11-09.md
```

## 7. 复现与回滚

- 全部检查可重复执行；`scripts/seed_demo.py` 只写 `DDNS_DATA_DIR` 指向的临时库，不碰生产 `data/`。
- 回滚：按 WP 提交逐段 revert；核心兼容点是 `db.changes_list` 签名与 `build_timeline` 返回值，模板已同步。
