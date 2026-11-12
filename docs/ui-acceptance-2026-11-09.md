# DDNS 管理后台 UI 视觉验收 · 问题清单

> 初验：2026-11-09　判定：**HOLD**　总分：**56 / 100**（以下问题清单保留为整改基线）
> 复验：2026-11-09　判定：**PASS（自评 92 / 100）**　P0 **8/8**、P1 **25/25**、P2 **19/24** 关闭
> 复验证据与命令：`docs/ui-remediation-report-2026-11-09.md`；2026-11-13 第 8 批复审：pytest 218 passed、对比度 76 项（含装饰线豁免）、页面结构 5 页；视觉 2 主题 × 5 宽度全通过。最新详情性能（90 天、10 次请求）：2000 条 p95 21.8ms，10000 条 p95 69.5ms，均低于 200ms。
> 审查范围：`app/templates/` 下 6 个模板、`app/static/style.css`，对照 `app/main.py`、`app/util.py`
> 初验局限：当时环境无法启动服务与截图；复验已用 TestClient + Playwright 补齐自动化证据；2026-11-13 复审已查看 320px 首页截图，其余截图仍以自动检查为主。

## 0. 汇总

| 页面 / 模块 | 阻断 P0 | 重要 P1 | 建议 P2 | 合计 |
| --- | ---: | ---: | ---: | ---: |
| 全局（base.html + style.css） | 3 | 8 | 6 | 17 |
| 总览 index.html | 3 | 6 | 4 | 13 |
| 域名详情 domain.html | 2 | 5 | 5 | 12 |
| 添加域名 new.html | 0 | 2 | 2 | 4 |
| 设置 settings.html | 0 | 2 | 4 | 6 |
| 登录 login.html | 0 | 2 | 3 | 5 |
| **合计** | **8** | **25** | **24** | **57** |

**级别定义**

| 级别 | 含义 |
| --- | --- |
| P0 阻断 | 违反 WCAG 2.1 AA 硬指标，或核心任务在目标设备上需绕行；未清零不得发布 |
| P1 重要 | 明显增加理解成本或出错概率，应在本次迭代内修复 |
| P2 建议 | 打磨项，可排入后续迭代 |

**维度口径**：B1 视觉层级 · B2 栅格与间距 · B3 字体排版 · B4 色彩与对比（WCAG 1.4.3 / 1.4.11）· B5 组件一致性 · B6 状态设计（WCAG 3.3.1）· B7 响应式（WCAG 1.4.10）· B8 可访问性（WCAG 1.3.1 / 2.1.1 / 2.4.1 / 2.4.2 / 2.4.7 / 4.1.2）

## 1. 全局（base.html + style.css）

| 编号 | 问题 | 位置 | 级别 | 依据 |
| --- | --- | --- | --- | --- |
| A1 | 导航与内容不在同一栅格：`nav` 无 `max-width`，`main` 是 1080px 居中，1440px 下出现两条垂直基准线 | `app/static/style.css:24-36`、`app/templates/base.html:14` | P1 | B2 |
| A2 | 窄屏导航无折叠策略：品牌 + 4 入口最小约 400px，390px 已超宽；`nav` 固定 52px 高无 overflow 处理 | `app/static/style.css:24-34`、`app/templates/base.html:16-21` | **P0** | B7 / WCAG 1.4.10 |
| A3 | 表单控件边界对比度不足：`--border:#d0d7de` 对白卡 1.45:1、对输入填充 1.36:1；深色 1.33:1 / 1.21:1 | `app/static/style.css:85-86` | **P0** | B4 / WCAG 1.4.11 |
| A4 | 焦点态不统一且偏弱：输入框 `outline:none` 仅换 1px 边框色，按钮与导航依赖 UA 默认环，无 `:focus-visible` 令牌 | `app/static/style.css:87`、`:71-78` | P1 | B8 / WCAG 2.4.7 |
| A5 | 复选框被通用 input 规则命中：继承 8px 内边距、灰底与圆角，原生勾选框缩在约 30px 灰框中间 | `app/static/style.css:85-89` | P1 | B5 |
| A6 | flash 文字对比度不足：绿字 4.30:1、红字 4.37:1（14px 正文需 4.5:1） | `app/static/style.css:38-39` | P1 | B4 / WCAG 1.4.3 |
| A7 | 深色模式主按钮白字 3.20:1（`#fff` on `#4c8dff`），hover 的 `brightness(1.12)` 使其更低 | `app/static/style.css:12`、`:71-73` | **P0** | B4 / WCAG 1.4.3 |
| A8 | 无 skip link，键盘用户每页先过 4 个导航项才能到内容 | `app/templates/base.html:13-26` | P1 | B8 / WCAG 2.4.1 |
| A9 | 当前页 / 当前时间窗只用 `class="on"` 表达，无 `aria-current="page"` | `app/templates/base.html:17-19`、`app/templates/domain.html:57` | P1 | B8 / WCAG 4.1.2 |
| A10 | 三页同名标题：index / new / login 均未覆盖 `block title`，标签页与历史全是「DDNS 监控」 | `app/templates/base.html:9`、`new.html:1`、`login.html:1` | P1 | B8 / WCAG 2.4.2 |
| A11 | flash 无 `role="status"` / `aria-live`，且消息来自 URL 参数，刷新重复出现 | `app/templates/base.html:24-25` | P2 | B6 |
| A12 | 间距尺度混用：4/6/8/10/12/14/16/18/20 全在用，卡片内距 18px、标题下距 14px 不在 4/8 网格 | `app/static/style.css:45-48`、`:84-91` | P2 | B2 |
| A13 | 标签 11px、提示 12px，中文 11px（「停用」「A」）笔画挤压 | `app/static/style.css:60-61`、`:91` | P2 | B3 |
| A14 | 无 `:active`、无 `:disabled`、无过渡，所有反馈瞬时 | `app/static/style.css:71-78` | P2 | B6 |
| A15 | 表格无 `tr:hover` / `:focus-within` 高亮，8 列宽表横向扫行易丢行 | `app/static/style.css:52-58` | P2 | B1 |
| A16 | 数字未统一 `font-variant-numeric:tabular-nums`（仅 `.chart-table td` 设了），数字列宽度抖动 | `app/static/style.css:171` vs `:119` | P2 | B3 |
| A17 | 空值与状态用词 5 套：`—`、`从未`、`待检查`、`ok`、`timeout`，同义不同表达 | `app/templates/index.html:204-205`、`domain.html:44-45` | P1 | B5 |

## 2. 总览 index.html

| 编号 | 问题 | 位置 | 级别 | 依据 |
| --- | --- | --- | --- | --- |
| B1 | 首屏三层数字墙：按 CSS 累加约 1100px 才到「监控项」，1440×900 下看不到任何域名 | `app/templates/index.html:7-59`、`:183` | **P0** | B1 |
| B2 | 「最近一次检查」取全局最新一条，可能是健康域名，真正要看的异常不在此 | `app/templates/index.html:9`、`:13-26`、`app/main.py:367` | P1 | B1 |
| B3 | 18 个数字等权：当前异常 = 1 与累计变动 = 37 视觉权重相同，无行动指引 | `app/templates/index.html:14-26`、`:34-58` | P1 | B1 |
| B4 | 三序列共用单轴，`ymax` 取三者最大值；日常检查 288/日 而变动/失败 0–2，两条线压成贴 0 直线 | `app/templates/index.html:65-96`、`app/main.py:289-308` | P1 | B1 |
| B5 | 图表标签窄屏不可读：`viewBox 0 0 720 240` 等比缩放，390px 下 11px 轴标签实渲 4.7px | `app/static/style.css:137`、`app/templates/index.html:65` | **P0** | B7 / WCAG 1.4.4 |
| B6 | 图表焦点不可见且 24 个 Tab 停靠：`.chart .hit{outline:none}` 后以 8% 蓝底替代（1.10:1），24 个格子各 `tabindex="0"` | `app/static/style.css:141-142`、`app/templates/index.html:91-94` | **P0** | B8 / WCAG 2.4.7、2.4.3 |
| B7 | `role="img"` 与内部可聚焦子节点冲突，ARIA 下子节点按 presentational 处理，aria-label 可能不播报 | `app/templates/index.html:65-66`、`:91-94` | P1 | B8 / WCAG 4.1.2 |
| B8 | 「IP 变动」折线 2.82:1（`#1baf7a` on `#fff`），低于数据图形 3:1 | `app/static/style.css:6`、`:146` | P1 | B4 / WCAG 1.4.11 |
| B9 | 0 域名冷启动仍渲染两张全 0 统计卡，空状态只有一句话、无按钮，且滚到页尾才见 | `app/templates/index.html:27-29`、`:32-59`、`:185-186` | P1 | B6 |
| B10 | 删除用原生 `confirm`，只说域名，不说不可恢复与将删多少条记录 | `app/templates/index.html:212-213` | P2 | B6 |
| B11 | 操作列按钮间距不等：两个 form 有 6px 右距、中间 `<a>` 无；按钮高 26px | `app/static/style.css:81-82`、`app/templates/index.html:209-214` | P2 | B5 |
| B12 | 7/30 天变动列左对齐且非等宽数字，纵向比较困难 | `app/templates/index.html:206-208` | P2 | B3 |
| B13 | tooltip 固定 `top:6px` 盖住峰值区，相邻格 `pointerleave→pointerenter` 造成闪烁 | `app/static/style.css:157`、`app/templates/index.html:171-177` | P2 | B6 |

## 3. 域名详情 domain.html

| 编号 | 问题 | 位置 | 级别 | 依据 |
| --- | --- | --- | --- | --- |
| C1 | 时间线无时间刻度：40px 色条只有百分比与悬浮 title，无法回答「什么时候变的」 | `app/templates/domain.html:64-69` | **P0** | B1 |
| C2 | 时间线信息只在 `title` 悬浮里，键盘 / 触屏 / 读屏均不可达，DOM 无文本等价物 | `app/templates/domain.html:67` | **P0** | B8 / WCAG 1.4.13、2.1.1、1.3.1 |
| C3 | 颜色是唯一编码：`ip_color` 用 md5 取色相，相邻色相可能撞色，无序号/纹理兜底 | `app/util.py:73-75`、`app/templates/domain.html:67`、`:72` | P1 | B4 / WCAG 1.4.1 |
| C4 | 移动端优先级倒置：`grid2` 折行后「监控设置」在前，「当前状态」被推到第二屏 | `app/static/style.css:49-50`、`app/templates/domain.html:14-50` | P1 | B7 |
| C5 | 「最近检查」表无空状态，无记录时只剩表头，也不说明只显示最近 20 条 | `app/templates/domain.html:116-128` | P1 | B6 |
| C6 | 页头无换行/截断策略，`page-head` 不换行、`h1` 无 `min-width:0`，长标签域名在 390px 溢出 | `app/static/style.css:41`、`app/templates/domain.html:4-8` | P1 | B7 |
| C7 | 状态直接吐原始英文码：详情「（ok，18ms）」、列表「timeout」，与中文「正常/成功」混排 | `app/templates/domain.html:44`、`:122`、`index.html:19` | P1 | B5 |
| C8 | 「7/30/90 天」仅靠颜色 + 字重标当前，无 `aria-current` | `app/templates/domain.html:55-59` | P2 | B8 |
| C9 | 分页按钮条件渲染，翻页时出现/消失导致布局跳动，也无 disabled 态 | `app/templates/domain.html:101-111` | P2 | B6 |
| C10 | `danger-zone` 只有一个右对齐红按钮，无标题、无删除范围说明，不在 `.card` 体系内 | `app/templates/domain.html:131-136` | P2 | B1 / B6 |
| C11 | 同语义两种 flash：Cloudflare 警告用卡内 `.flash.bad`，全局消息另一套，位置与宽度都不同 | `app/templates/domain.html:10-12` vs `base.html:24` | P2 | B5 |
| C12 | 跳 `#changes` 被 52px 粘性导航盖住标题，无 `scroll-padding-top` | `app/static/style.css:26`、`app/templates/domain.html:104-108` | P2 | B7 |

## 4. 添加域名 new.html

| 编号 | 问题 | 位置 | 级别 | 依据 |
| --- | --- | --- | --- | --- |
| D1 | 同一字段两种控件：「检查间隔」此处 8 档下拉（1 分钟–24 小时），详情页是秒数输入（60–86400） | `app/templates/new.html:21-32` vs `domain.html:30-32` | P1 | B5 |
| D2 | 域名输入无 `autocapitalize="off"`、`autocorrect="off"`、`spellcheck="false"`、`inputmode="url"`，移动端会首字母大写/纠错 | `app/templates/new.html:6-8` | P2 | B6 |
| D3 | 提交按钮同步触发 DNS 查询，全程无 pending / disabled，可重复提交 | `app/templates/new.html:34`、`app/main.py:433-436` | P1 | B6 |
| D4 | Cloudflare 说明贴在按钮下方，真正需要它的时刻是填写时 | `app/templates/new.html:35-36` | P2 | B3 |

## 5. 设置 settings.html

| 编号 | 问题 | 位置 | 级别 | 依据 |
| --- | --- | --- | --- | --- |
| E1 | Bot Token 明文放在 `type=text` 输入框与页面源码里，截图/肩窥即泄露 | `app/templates/settings.html:10-12` | P1 | B6 |
| E2 | 错误与字段脱节：保存失败只在顶部 flash，Telegram 表单在第二屏，无 `aria-invalid` 与字段级文案 | `app/templates/settings.html:8-36`、`base.html:24` | P1 | B6 / WCAG 3.3.1 |
| E3 | 三个表单三个「保存」，文案相同、不说明保存范围 | `app/templates/settings.html:24-27`、`:43`、`:53` | P2 | B5 |
| E4 | Telegram 获取流程（三步 + 原始 URL）12px 挤在两个按钮之后，且 `<br>` 硬断行 | `app/templates/settings.html:28-34` | P2 | B3 |
| E5 | 改密表单无 `autocomplete="new-password"`、无显示/隐藏、无字段级校验 | `app/templates/settings.html:40-44` | P2 | B6 / WCAG 1.3.5 |
| E6 | 数字输入无单位后缀、无 `inputmode="numeric"`，分钟/天数全写在 label 里 | `app/templates/settings.html:18-23`、`:50-51` | P2 | B6 |

## 6. 登录 login.html

| 编号 | 问题 | 位置 | 级别 | 依据 |
| --- | --- | --- | --- | --- |
| F1 | 无 h1，卡片标题用 h2；页面无品牌/主机标识，用户无法确认登的是哪台机器 | `app/templates/login.html:4-5` | P1 | B8 / WCAG 1.3.1 |
| F2 | 密码框缺 `autocomplete="current-password"` | `app/templates/login.html:7` | P1 | B8 / WCAG 1.3.5 |
| F3 | 错误 flash 在 1080px 容器里，登录卡仅 360px，视觉上像两条不同栏目的消息 | `app/static/style.css:37`、`app/templates/login.html:3-11` | P2 | B2 |
| F4 | 初始密码写在 `initial_password.txt`，页面无「去哪儿找密码」提示 | `app/main.py:642` | P2 | B6 |
| F5 | `autofocus` 在移动端立刻弹键盘，可能顶掉卡片 | `app/templates/login.html:7` | P2 | B7 |

## 7. 修复批次（按投入产出排序）

| 批次 | 内容 | 覆盖条目 | 预估 |
| --- | --- | --- | --- |
| 1 | 令牌层：`--border-strong`、`--accent-fill`、`--flash-ok-fg`、`--flash-bad-fg`、`--s-changes` | A3、A6、A7、B8 | 1 小时 |
| 2 | 焦点环统一 + 图表 roving tabindex + 去掉 svg 的 `role="img"` | A4、B6、B7 | 半天 |
| 3 | 首页重排：异常横幅置顶、监控项进首屏、统计压缩为一行 | B1、B2、B3 | 半天 |
| 4 | 图表窄屏横向滚动 + IP 变动/失败量级分离 + tooltip 跟随 | B4、B5、B13 | 半天 |
| 5 | IP 时间线补日期刻度、可聚焦文本等价物、固定调色板 | C1、C2、C3 | 1 天 |
| 6 | 移动端导航折叠 + 表格降级为 5 列卡片 + 页头截断 | A1、A2、C4、C6 | 1–1.5 天 |
| 7 | 表单 pending 态、字段级错误、术语与空值统一 | A17、C7、D3、E2 | 1 天 |
| 8 | 打磨项：间距网格、tabular-nums、aria-current、skip link、空状态、分页禁用态 | A8–A16、B9–B12、C5、C8–C12、D2、D4、E1、E3–E6、F1–F5 | 1–2 天 |

## 8. PASS 条件

1. 首屏可见「当前异常」与「立即检查」，监控项表格进入首屏或一键可达。
2. 所有表单控件边界 ≥ 3:1，焦点环 ≥ 3:1 且 ≥ 2px；深色模式全部文字 ≥ 4.5:1。
3. 390px 下 `document.scrollWidth === innerWidth`，导航不折行不溢出，表格无横向滚动且保留状态 + 操作。
4. 320–1440px 全区间图表标签实际字号 ≥ 11px；Tab 一次进入、方向键遍历、焦点清晰可见。
5. 时间线有日期刻度、可聚焦/可点击、含文本等价物。
6. 表单错误就近字段显示并带 `aria-invalid`；所有 POST 动作有 pending 且不可重复提交。
7. 术语与空值表达全站统一。

## 9. 范围外观察（不计入 57 项）

- 所有 POST 表单无 CSRF token（依赖 `SameSite=Lax` 缓解）。
- `app/templates/domain.html:133` 把域名拼进 `onsubmit` 的 JS 字符串，当前被域名正则限制，放宽校验即成注入点。
- 「立即检查」在请求内同步做 DNS 查询（`app/main.py:600`），网络差时 POST 会挂数秒，建议异步化。


## 10. 复验记录（2026-11-09）

| 级别 | 原数量 | 关闭 | 残余 | 结论 |
| --- | ---: | ---: | ---: | --- |
| P0 | 8 | 8 | 0 | 清零，阻断项全部关闭 |
| P1 | 25 | 25 | 0 | 修复率 100%（目标 ≥90%） |
| P2 | 24 | 19 | 5 | 剩余 A12/A14/C11/D4/E5，均为非阻断打磨项 |

**PASS 1–7 逐条**：1 首屏异常面板 + 立即检查（2026-11-13 visual 首屏监控项 top 551–802px；320px 为 802px，异常列表限高 180px 并滚动，900px 视口高度与断言保持不变）；2 对比度 76 项通过（含装饰线豁免）、焦点环 3px；3 五档宽度 `scrollWidth === innerWidth`、390 表格卡片化；4 图表标签实渲 ≥11.79px、单 Tab + 方向键；5 时间线 5 刻度 + details 文本等价 + 键盘逐段；6 字段级 `aria-invalid` + 所有 POST `data-pending`；7 `status_label`/`fmt_empty` 统一状态与空值。

**残余与限制**：A12 间距未完全网格化、A14 缺 `:active`、C11 Cloudflare 警告与全局 flash 仍是两种视觉、D4 添加页缺 Cloudflare 即时说明、E5 无密码显示切换；2026-11-13 已查看 `artifacts/ui/` 的 320px 首页截图，其他截图主要由自动门禁校验，完整人工视觉签字仍待复核。

**复验评分口径**：按 100 分制自评 92；P0 0 项、P1 25/25 不扣分，5 个残余 P2 与截图文检限制合计扣 8 分。该分数是工程自评，不替代独立验收人签字。
