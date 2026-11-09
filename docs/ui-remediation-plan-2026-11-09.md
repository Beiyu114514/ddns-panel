# DDNS 管理后台整改与落地方案

> 依据：`docs/ui-acceptance-2026-11-09.md`（HOLD，56/100，8 P0 / 25 P1 / 24 P2）；代码审查 cb349fb 的 6 项已确认问题（时间线图例采样、计数口径、OFFSET 分页、详情页双扫描、变动记录不随窗口、基线重置行语义）。
> 状态：**方案待确认**，确认后按里程碑实施；本文件不改动任何仓库代码。
> 约束：保持 Jinja 服务端渲染与单文件 CSS，不引入前端构建链；运行环境无 root。

## 1. 目标与放行标准

**硬门槛（未满足不得发布）**

1. 8 项 P0 全部清零：A2、A3、A7、B1、B5、B6、C1、C2。
2. 25 项 P1 修复率 ≥ 90%，剩余项需书面豁免并记录原因。
3. 代码审查 6 项问题全部关闭（R1 图例采样、R2 计数口径、R3 分页重复、R4 详情页性能、R5 变动记录窗口、R6 marker 行）。
4. 验收文档第 8 节 PASS 条件 1–7 全部通过。
5. 无功能回归：登录/登出、域名增删改查、立即检查、设置保存、Telegram 测试、分页与时间窗切换。

**质量门槛**

- 对比度脚本在浅色/深色两套主题下全绿（正文 ≥ 4.5:1，控件边界与图形 ≥ 3:1）。
- 320 / 390 / 768 / 1024 / 1440 五档宽度下 `document.scrollWidth === innerWidth`。
- 详情页在 1 万条 changes 下 p95 < 200ms（本地 SQLite）。
- 键盘可完成：导航 → 主内容 → 图表逐点 → 时间线逐段 → 表格翻页。

**目标评分**：≥ 90 / 100（当前 56）。

## 2. 范围

**本次做**

- `app/templates/` 六个模板、`app/static/style.css` 的视觉、响应式与可访问性整改。
- `app/main.py` 视图模型、`app/db.py` 查询、`app/util.py` 状态与配色工具。
- 最小测试与验收脚本（对比度、页面结构、视觉矩阵、数据种子）。

**本次不做（需确认，见 §8）**

- CSRF token、删除确认的 `onsubmit` 字符串拼接、立即检查异步化（验收文档第 9 节范围外三项）。
- 引入前端框架/组件库/构建链、重做视觉语言、国际化。

## 3. 环境判断（影响落地方案）

| 事项 | 现状 | 结论 |
| --- | --- | --- |
| Python | `python3.12.16` 在 `/data/dsh/home/.local/bin`，应用依赖未安装 | WP0 用 `uv`/`pip` 建 `.venv` 并安装 `requirements.txt`；PyPI 可达 |
| 浏览器 | Playwright 1.65 + chromium headless shell 已缓存，但缺 `libglib` 等 20+ 系统库，无 root | 优先方案 A：下载 `.deb` 解包到 `~/.local/playwright-deps`，用 `LD_LIBRARY_PATH` 启动；失败则方案 B：静态检查 + 用户人工截图复核 |
| 样式体系 | 单文件 CSS + `:root` 令牌，无构建步骤 | 采用 token-first 修复，风险低、可分批提交 |
| 图表 | 服务端生成 SVG + 内联 JS（`index.html` 内） | 保留服务端渲染，只重构数据与交互，不引库 |
| 时间线 | CSS 绝对定位色条 + `title` 悬浮 | 重建为可聚焦、有刻度、有文本等价物的组件 |

## 4. 整改工作包（WP）

| WP | 内容 | 覆盖条目 | 主要文件 | 依赖 | 预估 |
| --- | --- | --- | --- | --- | ---: |
| WP0 | 环境与验收基线：venv、依赖、演示数据、页面冒烟、截图管线 | 全部验收前置 | `.venv`、`scripts/` | — | 0.5 天 |
| WP1 | 设计令牌与基础组件：边界/强调/焦点/flash 令牌、间距与字号刻度、按钮与表单态 | A3、A6、A7、A12–A16、B8 | `style.css` | WP0 | 0.5 天 |
| WP2 | 全局框架：导航栅格与移动折叠、skip link、`aria-current`、页面标题、flash 语义 | A1、A2、A8–A11、C12 | `base.html`、`style.css` | WP1 | 0.5 天 |
| WP3 | 首页信息架构：异常置顶、监控项进首屏、18 个数字压缩、0 域名冷启动 | B1、B2、B3、B9 | `index.html`、`main.py` | WP1 | 1 天 |
| WP4 | 图表重构：量级分离、窄屏可读、roving tabindex、去掉 `role="img"`、tooltip | B4–B8、B13 | `index.html`、`style.css`、`main.py` | WP3 | 1 天 |
| WP5 | 时间线重构：日期刻度、可聚焦文本等价物、固定调色板、精确图例 | C1–C3、C8、R1 | `domain.html`、`style.css`、`main.py`、`db.py` | WP8-A | 1.5 天 |
| WP6 | 响应式：导航折叠、表格移动端降级、页头截断、grid 顺序 | A2、C4、C6、B12 | `index.html`、`domain.html`、`style.css` | WP2、WP3 | 1 天 |
| WP7 | 表单与反馈：间隔控件统一、pending/防重复、令牌保护、字段级错误、登录页 | D1–D4、E1–E6、F1–F5、A17、C7 | 四个模板、`main.py`、`util.py` | WP1 | 1 天 |
| WP8 | 后端正确性与性能：统计契约、keyset 分页、窗口过滤、marker 行、覆盖索引 | R2–R6、C5、C9、P3 | `db.py`、`main.py`、模板 | WP0 | 1.5 天 |
| WP9 | 打磨与一致性：间距网格、tabular-nums、空状态、分页禁用态、危险区、删除确认 | A12–A16、B10–B13、C5、C9–C12、D2、D4、E3–E6、F3–F5 | 全部模板、`style.css` | 全部 | 1 天 |

合计约 **9–10 人日**（单人）；P2 可在 WP9 内按剩余时间裁剪。

## 5. 数据与接口契约草案

### 5.1 统计契约（R2）

`db.change_stats(domain_id, since=None)` 返回：

```
{rows, changes, first_records, markers}
```

- `rows`：全部行数（含首次记录与 marker）；
- `changes`：`old_ip IS NOT NULL` 的变动次数；
- `first_records`：`old_ip IS NULL AND new_ip IS NOT NULL`；
- `markers`：`old_ip IS NULL AND new_ip IS NULL`。

页面文案统一为：

- 详情页状态卡：「累计变动 M 次」；
- 变动记录卡：「共 N 行 · M 次变动 · K 条基线重置」；
- 时间线提示：「该窗口内 M 次变动」；若存在窗口内 marker，改为「重置后 M 次变动」。

### 5.2 时间线（R1、C1–C3）

`main.py` 内新增 `build_timeline(domain_id, days, now)`，模板只消费视图模型：

```
{
  window: {start, end},
  sampled: bool,
  bucket_seconds: int | null,
  ticks: [{ts, label}],                 # 4–6 个日期刻度
  segments: [{start, end, ip, color_index, label}],
  legend: [{ip, dur, pct, color_index, exact}],
  counts: {window_changes, rows, markers}
}
```

- 图例 `dur` 由**未采样**的完整序列计算：`LEAD(ts) OVER (PARTITION BY domain_id ORDER BY ts, id)`，按 `new_ip` 汇总；采样只影响 `segments`。
- 采样时在提示与图例标注「采样显示，时长按完整记录计算」；「其他 N 个 IP」的 N 用真实 distinct IP 数。
- `util.ip_color` 改为固定 12 色调色板（按首次出现顺序分配 `color_index`），不再用 md5 取色相；相邻段用序号/纹理兜底。
- 模板加 `<details>` 文本等价表（段起止时间、IP、时长），满足键盘/读屏可达。

### 5.3 分页（R3、C9）

- `db.changes_list(domain_id, limit, before_ts=None, before_id=None)` 使用 `(ts, id)` keyset：
  `WHERE (ts < ? OR (ts = ? AND id < ?)) ORDER BY ts DESC, id DESC LIMIT ?`。
- URL：`?days=30&before=<ts>_<id>`；响应视图模型带 `next_cursor`。旧 `page=` 参数保留 303 兼容（跳转到等价游标或第一页）。
- 模板 pager：「上一页 / 下一页」分别在有/无游标时才可点，另一侧 `disabled`，不再用页码计算偏移。

### 5.4 状态与术语（A17、C7）

`util.py` 新增：

```
status_label(status) -> "正常" | "超时" | "失败" | "待检查"
fmt_empty(value, kind) -> "—" | "从未" | "暂无"
```

模板统一使用过滤器/函数，不再直接输出 `ok` / `timeout` 原始码；全站按钮文案带范围：「保存 Telegram 设置」「保存数据保留设置」。

### 5.5 图表（B4–B6）

- 拆为上下两张共享 x 轴的 SVG：上「检查次数」柱状（独立 y 轴），下「IP 变动 / 失败」折线（独立 y 轴）；或单图双轴，二选一由 §8 决策。
- 窄屏：图表放入 `.chart-scroll`，`min-width: 640px` 横向滚动，保证轴标签实际字号 ≥ 11px；不再整体等比缩放。
- 交互：整图一个 Tab 停靠，方向键在 24 个点间移动（roving tabindex），焦点环 ≥ 2px、对比度 ≥ 3:1；移除 `role="img"`，改用 `<title>`/`<desc>` + `role="status"` 播报。

## 6. 里程碑与出口标准

| 里程碑 | 时间 | 内容 | 出口标准 |
| --- | --- | --- | --- |
| M1 | D1–D2 | WP0、WP1、WP2 | A2/A3/A7 关闭；对比度脚本双主题全绿；390px 导航不溢出 |
| M2 | D3–D4 | WP8-A（数据契约）、WP3 | R1–R6 数据侧关闭；1440×900 首屏可见监控项；0 域名有 CTA |
| M3 | D5–D6 | WP4、WP5 | B5/B6/C1/C2 关闭；图表与时间线键盘可遍历；1 万条 p95 < 200ms |
| M4 | D7–D8 | WP6、WP7 | 8 项 P0 全清；320/390/768/1024/1440 无横向溢出；表单 pending 生效 |
| M5 | D9–D10 | WP9、全量回归、验收复跑 | P1 修复率 ≥ 90%；PASS 条件 1–7 全过；评分 ≥ 90 |

## 7. 验收与回归方案

### 7.1 自动化

- `scripts/check_contrast.py`：解析 `style.css` 令牌，按 WCAG 公式计算浅/深两套主题的文本、边界、图形对比度，低于阈值即失败。
- `scripts/check_pages.py`：用 FastAPI `TestClient` 渲染五个页面，断言标题唯一、`aria-current`、表单 `autocomplete`、时间线文本等价表、分页 disabled 属性。
- `scripts/visual.mjs`：Playwright 可用时截取 5 档宽度 × 2 主题，断言 `scrollWidth`、字号、焦点环；不可用时降级为人工截图清单。
- `scripts/seed_demo.py`：生成 3 个域名、1 万条 changes、含 marker、9 个不同 IP，覆盖 240/241 条边界与重置场景。
- 数据库单测：统计契约、keyset 分页（翻页期间插入新行不重复/不漏）、marker 行渲染、图例时长与完整序列一致。

### 7.2 手工矩阵

- 主题：浅色 / 深色；
- 宽度：320 / 390 / 768 / 1024 / 1440；
- 键盘：Tab 顺序、图表方向键、时间线逐段、skip link；
- 流程：登录 → 总览 → 添加域名 → 详情改设置 → 立即检查 → 删除 → 设置保存 → 分页/时间窗切换。

### 7.3 性能

- 用 1 万条 changes 的种子库测详情页 p50/p95；
- `EXPLAIN QUERY PLAN` 检查是否仍有临时 B-tree 与回表；
- 新增索引：`(domain_id, ts, new_ip)`、`(domain_id, ts DESC, id DESC)`。

## 8. 需用户拍板的决策点

1. **视觉基调**：保持现有 GitHub 风格只做修复（推荐），还是借本次重做视觉语言？
2. **首页信息架构**：把「最近一次检查 / 今日检查 / 监控概况」压缩为一条摘要 + 可展开，监控项表格置顶（推荐）？「最近一次检查」是否改为「最近异常」？
3. **图表方案**：上下两图（推荐，量级分离最彻底），还是单图双轴（改动小）？
4. **时间线采样**：240 段阈值是否保留？图例是否强制按完整记录计算（推荐是）？
5. **变动记录范围**：全量历史 + 明确标注（推荐），还是跟随 7/30/90 天窗口？
6. **分页**：改用 keyset 游标（推荐，彻底消除重复/漏行），还是保留页码 + 越界重定向？
7. **Bot Token**：改为「不回显、留空不修改」（推荐，安全），还是密码框 + 显示/隐藏按钮（仍会回显）？
8. **移动端表格**：降级为 5 列卡片（推荐），还是横向滚动保留 8 列？
9. **P2 范围**：24 项全部纳入（推荐，排在最后可裁剪），还是只做与 P0/P1 重叠部分？
10. **验证资源**：允许下载缺失 `.so` 搭 Playwright（约 200MB，方案 A），还是接受静态检查 + 人工截图复核（方案 B）？
11. **范围外三项**：CSRF、`onsubmit` 拼接、检查异步化，本次做还是下期？
12. **测试与 CI**：是否补最小 pytest 用例与本地门禁脚本（推荐补）？

## 9. 风险与回滚

| 风险 | 影响 | 缓解 |
| --- | --- | --- |
| CSS 令牌全局改动 | 所有页面视觉偏移 | 按 WP 分批提交，每批跑截图矩阵；令牌保留旧值别名 |
| Bot Token 留空不改逻辑 | 误清空已存令牌 | 后端单测 + 保存前二次确认 |
| keyset 分页改 URL | 老书签失效 | `page=` 参数 303 兼容跳转 |
| 图表/时间线重构 | tooltip 或键盘交互回归 | 保留 payload 契约，逐点手工回归 |
| Playwright 缺库 | 验收证据不足 | 方案 A 失败即转方案 B，并在验收报告注明 |
| 排期 | P2 挤压 P0/P1 | M4 前不碰 P2；WP9 可裁剪 |

## 10. 交付物

- 按里程碑的代码提交（每 WP 一个可回滚提交）；
- 本方案与实施记录；
- `scripts/` 下的对比度、页面结构、视觉矩阵、数据种子脚本；
- 验收复跑报告（更新 `ui-acceptance-2026-11-09.md` 的判定与分数）；
- 变更说明（CHANGELOG 或 commit message 汇总）。

## 11. 待确认清单（实施前）

- [ ] §8 的 12 个决策点逐项确认；
- [ ] 验收资源方案（A/B）确认；
- [ ] P2 范围与截止点确认；
- [ ] 是否同步修复范围外三项确认；
- [ ] 确认后从 WP0 开始，不改动未列入本方案的文件。
