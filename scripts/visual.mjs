#!/usr/bin/env node
/**
 * 视觉/响应式门禁（Playwright）。
 *
 * 用法（仓库已装好 Playwright 驱动时）：
 *   NODE_PATH=/data/dsh/home/.local/playwright-driver/node_modules \
 *   LD_LIBRARY_PATH=/data/dsh/home/.local/playwright-deps/root/usr/lib/x86_64-linux-gnu:/data/dsh/home/.local/playwright-deps/root/lib/x86_64-linux-gnu \
 *   node scripts/visual.mjs
 *
 * 默认自行 seed 演示数据并启动 uvicorn；也可：
 *   node scripts/visual.mjs --base-url http://127.0.0.1:8000 --password xxx
 *
 * 断言：5 档宽度 × 明/暗主题下页面无横向溢出、图表轴标签实际字号 ≥11px、
 * 图表/时间线焦点环可见、方向键可操作；同时输出截图到 artifacts/ui/。
 * 浏览器无法启动时输出人工检查清单并以状态码 2 退出。
 */
import { createRequire } from 'node:module';
import { spawn, spawnSync } from 'node:child_process';
import fs from 'node:fs';
import os from 'node:os';
import path from 'node:path';
import { fileURLToPath } from 'node:url';

const require = createRequire(import.meta.url);
const playwrightCandidates = [
  process.env.PLAYWRIGHT_MODULE,
  ...(process.env.NODE_PATH || '').split(':').filter(Boolean).map((p) => path.join(p, 'playwright')),
  '/data/dsh/home/.local/playwright-driver/node_modules/playwright',
].filter(Boolean);
let chromium = null;
for (const candidate of playwrightCandidates) {
  try {
    ({ chromium } = require(candidate));
    break;
  } catch { /* 试下一个 */ }
}
if (!chromium) {
  console.error('找不到 Playwright 模块；请设置 PLAYWRIGHT_MODULE 或 NODE_PATH。');
  process.exit(2);
}

const ROOT = path.dirname(path.dirname(fileURLToPath(import.meta.url)));
const PY = process.env.PYTHON || path.join(ROOT, '.venv', 'bin', 'python');
const CHROME = process.env.CHROME_BIN ||
  '/data/dsh/home/.cache/ms-playwright/chromium_headless_shell-1254/chrome-headless-shell-linux64/chrome-headless-shell';
const WIDTHS = (process.env.WIDTHS || '320,390,768,1024,1440').split(',').map(Number);
const THEMES = (process.env.THEMES || 'light,dark').split(',');
const OUT_DIR = path.join(ROOT, 'artifacts', 'ui');

const args = process.argv.slice(2);
function arg(name, fallback = null) {
  const i = args.indexOf(`--${name}`);
  return i >= 0 ? args[i + 1] : fallback;
}
const baseUrlArg = arg('base-url');
const passwordArg = arg('password');
const keep = args.includes('--keep');
const screenshots = !args.includes('--no-screenshots');

const failures = [];
let server = null;
let dataDir = null;

function fail(message) {
  failures.push(message);
  console.log(`  FAIL ${message}`);
}

function ok(message) {
  console.log(`  PASS ${message}`);
}

async function waitForServer(url, timeoutMs = 20000) {
  const started = Date.now();
  while (Date.now() - started < timeoutMs) {
    try {
      const res = await fetch(url + '/login');
      if (res.ok) return;
    } catch { /* 还没起来 */ }
    await new Promise((r) => setTimeout(r, 120));
  }
  throw new Error(`服务器在 ${timeoutMs}ms 内没有就绪: ${url}`);
}

async function ensureServer() {
  if (baseUrlArg) return baseUrlArg.replace(/\/$/, '');
  dataDir = fs.mkdtempSync(path.join(os.tmpdir(), 'ddns-visual-'));
  console.log(`准备演示数据: ${dataDir}`);
  const seeded = spawnSync(PY, [path.join(ROOT, 'scripts', 'seed_demo.py'),
    '--data-dir', dataDir, '--changes', '4000'], {
    cwd: ROOT, encoding: 'utf8',
  });
  if (seeded.status !== 0) {
    throw new Error(`seed_demo.py 失败: ${seeded.stderr || seeded.stdout}`);
  }
  const port = 8300 + Math.floor(Math.random() * 400);
  const url = `http://127.0.0.1:${port}`;
  server = spawn(PY, ['-m', 'uvicorn', 'app.main:app', '--host', '127.0.0.1',
    '--port', String(port)], {
    cwd: ROOT,
    env: { ...process.env, DDNS_DATA_DIR: dataDir, PYTHONPATH: ROOT },
    stdio: ['ignore', 'pipe', 'pipe'],
  });
  let log = '';
  server.stderr.on('data', (chunk) => { log += chunk.toString(); });
  server.on('exit', (code) => {
    if (code && code !== 0 && server) console.error(`uvicorn 退出: ${code}\n${log.slice(-2000)}`);
  });
  await waitForServer(url);
  console.log(`临时服务: ${url}`);
  return url;
}

function readPassword() {
  if (passwordArg) return passwordArg;
  const dir = dataDir || process.env.DDNS_DATA_DIR;
  if (dir) {
    const file = path.join(dir, 'initial_password.txt');
    if (fs.existsSync(file)) return fs.readFileSync(file, 'utf8').trim();
  }
  throw new Error('外部 --base-url 模式需要 --password 或 DDNS_DATA_DIR 中的 initial_password.txt');
}

async function login(page, base, password) {
  // 会话 cookie 带 Secure，本地 http 门禁里浏览器不会保存它。
  // 先用 fetch 拿到 Set-Cookie，再以非 Secure 方式注入当前测试上下文；
  // 这只影响本地视觉门禁，生产仍由浏览器按 Secure 规则处理。
  const resp = await fetch(`${base}/login`, {
    method: 'POST',
    headers: { 'content-type': 'application/x-www-form-urlencoded' },
    body: new URLSearchParams({ password }).toString(),
    redirect: 'manual',
  });
  const raw = resp.headers.getSetCookie
    ? resp.headers.getSetCookie()
    : [resp.headers.get('set-cookie')].filter(Boolean);
  const session = raw.map((c) => c.split(';')[0])
    .find((c) => c.startsWith('ddns_session='));
  if (!session) throw new Error('登录失败：未获取到会话 cookie');
  const eq = session.indexOf('=');
  await page.context().addCookies([{
    name: session.slice(0, eq),
    value: session.slice(eq + 1),
    url: base,
    httpOnly: true,
    secure: false,
    sameSite: 'Lax',
  }]);
  await page.goto(`${base}/`, { waitUntil: 'domcontentloaded' });
}

async function focusWithTab(page, selector, maxTabs = 100) {
  await page.evaluate(() => { if (document.activeElement) document.activeElement.blur(); });
  for (let i = 0; i < maxTabs; i += 1) {
    await page.keyboard.press('Tab');
    const hit = await page.evaluate((sel) => {
      const el = document.activeElement;
      return !!el && el.matches && el.matches(sel);
    }, selector);
    if (hit) return true;
  }
  return false;
}

async function checkFocusRing(page, selector) {
  const focused = await focusWithTab(page, selector);
  if (!focused) {
    fail(`${selector} 无法通过 Tab 到达`);
    return;
  }
  const ring = await page.evaluate((sel) => {
    const cs = getComputedStyle(document.querySelector(sel));
    return { width: parseFloat(cs.outlineWidth) || 0, style: cs.outlineStyle, color: cs.outlineColor };
  }, selector);
  if (ring.width >= 2 && ring.style !== 'none') {
    ok(`${selector} 焦点环 ${ring.width}px ${ring.style} ${ring.color}`);
  } else {
    fail(`${selector} 焦点环不足：${JSON.stringify(ring)}`);
  }

  // 计算样式有 outline 不代表看得见：祖先 overflow 容器可能把环裁掉
  const clip = await page.evaluate((sel) => {
    const el = document.querySelector(sel);
    if (!el) return { clipped: false };
    const cs = getComputedStyle(el);
    const pad = (parseFloat(cs.outlineWidth) || 0) + (parseFloat(cs.outlineOffset) || 0);
    const r = el.getBoundingClientRect();
    let node = el.parentElement;
    while (node && node !== document.documentElement) {
      const s = getComputedStyle(node);
      const clipsX = s.overflowX !== 'visible';
      const clipsY = s.overflowY !== 'visible';
      if (clipsX || clipsY) {
        const nr = node.getBoundingClientRect();
        const top = nr.top + node.clientTop;
        const left = nr.left + node.clientLeft;
        const bottom = top + node.clientHeight;
        const right = left + node.clientWidth;
        // 可滚动方向上的边缘本来就可能滚出视口，只检查不可滚动方向
        const checkX = clipsX && node.scrollWidth <= node.clientWidth + 1;
        const checkY = clipsY && node.scrollHeight <= node.clientHeight + 1;
        const edges = {
          top: checkY && r.top - pad < top,
          bottom: checkY && r.bottom + pad > bottom,
          left: checkX && r.left - pad < left,
          right: checkX && r.right + pad > right,
        };
        if (edges.top || edges.bottom || edges.left || edges.right) {
          return { clipped: true, node: node.className || node.tagName, edges };
        }
      }
      node = node.parentElement;
    }
    return { clipped: false };
  }, selector);
  if (clip.clipped) {
    fail(`${selector} 焦点环被 ${clip.node} 裁切：${JSON.stringify(clip.edges)}`);
  } else {
    ok(`${selector} 焦点环未被裁切`);
  }
}

async function pageMetrics(page) {
  return page.evaluate(() => ({
    scrollWidth: document.documentElement.scrollWidth,
    bodyScrollWidth: document.body.scrollWidth,
    innerWidth: window.innerWidth,
    clientWidth: document.documentElement.clientWidth,
  }));
}

async function checkOverflow(page, label) {
  const m = await pageMetrics(page);
  if (m.scrollWidth > m.innerWidth + 1 || m.bodyScrollWidth > m.innerWidth + 1) {
    fail(`${label} 横向溢出：scrollWidth=${m.scrollWidth} innerWidth=${m.innerWidth}`);
  } else {
    ok(`${label} 无横向溢出 (${m.scrollWidth}/${m.innerWidth})`);
  }
}

async function checkChart(page, width, themeLabel) {
  const exists = await page.locator('#chart-svg').count();
  if (!exists) {
    fail(`${themeLabel}/${width} 缺少图表`);
    return;
  }
  const label = await page.evaluate(() => {
    const text = document.querySelector('.axis-label');
    if (!text) return null;
    const cs = getComputedStyle(text);
    const ctm = text.getScreenCTM();
    const scale = ctm ? ctm.a : 1;
    return {
      css: parseFloat(cs.fontSize),
      scale: Number(scale.toFixed(3)),
      actual: Number((parseFloat(cs.fontSize) * scale).toFixed(2)),
    };
  });
  if (!label) {
    fail(`${themeLabel}/${width} 图表轴标签缺失`);
  } else if (label.actual + 0.01 < 11) {
    fail(`${themeLabel}/${width} 轴标签实际字号 ${label.actual}px（css ${label.css}px × scale ${label.scale}）`);
  } else {
    ok(`${themeLabel}/${width} 轴标签实际字号 ${label.actual}px`);
  }

  if (width <= 390) {
    const scroller = await page.evaluate(() => {
      const el = document.querySelector('.chart-scroll');
      const svg = document.querySelector('#chart-svg');
      return {
        scrollWidth: el ? el.scrollWidth : 0,
        clientWidth: el ? el.clientWidth : 0,
        svgWidth: svg ? svg.getBoundingClientRect().width : 0,
      };
    });
    if (scroller.svgWidth < 600 || scroller.scrollWidth <= scroller.clientWidth) {
      fail(`${themeLabel}/${width} 图表窄屏未保持最小宽度/横向滚动：${JSON.stringify(scroller)}`);
    } else {
      ok(`${themeLabel}/${width} 图表内部横向滚动 svg=${Math.round(scroller.svgWidth)}px`);
    }
  }

  await checkFocusRing(page, '#chart-svg');
  const before = await page.textContent('#chart-tip');
  await page.keyboard.press('ArrowRight');
  await page.waitForTimeout(60);
  const after = await page.textContent('#chart-tip');
  if (before === after) {
    fail(`${themeLabel}/${width} 图表方向键未更新提示`);
  } else {
    ok(`${themeLabel}/${width} 图表方向键可用`);
  }
}

async function checkTimeline(page, themeLabel, width) {
  if (await page.locator('#tl-track').count()) {
    await checkFocusRing(page, '#tl-track');
    const before = await page.textContent('#tl-tip');
    await page.keyboard.press('ArrowRight');
    await page.waitForTimeout(60);
    const after = await page.textContent('#tl-tip');
    if (before === after) {
      fail(`${themeLabel}/${width} 时间线方向键未更新提示`);
    } else {
      ok(`${themeLabel}/${width} 时间线方向键可用`);
    }
  } else {
    fail(`${themeLabel}/${width} 缺少时间线`);
  }
}

async function checkFirstScreen(page, label) {
  const info = await page.evaluate(() => {
    const domains = document.querySelector('#domains');
    const chart = document.querySelector('.chart-card');
    return {
      domainsTop: domains ? domains.getBoundingClientRect().top : null,
      chartTop: chart ? chart.getBoundingClientRect().top : null,
      viewport: window.innerHeight,
      stats: document.querySelectorAll('.stats .stat').length,
      immediate: domains ? Array.prototype.filter.call(
        domains.querySelectorAll('button'),
        (b) => b.textContent.trim() === '立即检查' && b.getClientRects().length > 0
      ).length : 0,
      chartLabel: chart ? chart.getAttribute('aria-labelledby') : null,
    };
  });
  if (info.domainsTop !== null && info.domainsTop < info.viewport) {
    ok(`${label} 首屏可见监控项（top=${Math.round(info.domainsTop)}px）`);
  } else {
    fail(`${label} 监控项未进入首屏`);
  }
  if (info.stats === 4) ok(`${label} 首屏统计卡为 4 张`);
  else fail(`${label} 统计卡数量 ${info.stats}，应为 4`);
  if (info.immediate > 0) ok(`${label} 首屏可见「立即检查」（${info.immediate} 个）`);
  else fail(`${label} 首屏缺少「立即检查」`);
  if (info.chartTop === null || info.domainsTop === null || info.chartTop > info.domainsTop) {
    ok(`${label} 监控项在图表之前`);
  } else {
    fail(`${label} 图表位于监控项之前`);
  }
}

async function run() {
  process.env.LD_LIBRARY_PATH = [
    '/data/dsh/home/.local/playwright-deps/root/usr/lib/x86_64-linux-gnu',
    '/data/dsh/home/.local/playwright-deps/root/lib/x86_64-linux-gnu',
    process.env.LD_LIBRARY_PATH || '',
  ].filter(Boolean).join(':');

  const base = await ensureServer();
  const password = readPassword();
  fs.mkdirSync(OUT_DIR, { recursive: true });
  console.log(`截图目录: ${OUT_DIR}`);

  let browser;
  try {
    browser = await chromium.launch({
      executablePath: fs.existsSync(CHROME) ? CHROME : undefined,
      headless: true,
      args: ['--no-sandbox', '--disable-dev-shm-usage'],
    });
  } catch (err) {
    console.error(`\n无法启动 Chromium：${err.message}\n`);
    console.error('请转人工检查清单：');
    console.error('- 320/390/768/1024/1440 × 明/暗主题截图：首页、域名详情、设置、添加域名');
    console.error('- 每档 document.scrollWidth 必须等于 innerWidth，无横向滚动条');
    console.error('- 图表轴标签实际字号 ≥11px；图表可横向滚动查看');
    console.error('- Tab 到图表/时间线有 3px 焦点环，方向键更新提示');
    return 2;
  }

  try {
    for (const theme of THEMES) {
      for (const width of WIDTHS) {
        const label = `${theme}/${width}`;
        console.log(`\n== ${label} ==`);
        const context = await browser.newContext({
          viewport: { width, height: 900 },
          colorScheme: theme === 'dark' ? 'dark' : 'light',
          reducedMotion: 'reduce',
        });
        const page = await context.newPage();
        await login(page, base, password);

        await page.goto(`${base}/`, { waitUntil: 'networkidle' });
        await checkOverflow(page, `${label} 首页`);
        await checkFirstScreen(page, label);
        await checkChart(page, width, theme);

        await page.goto(`${base}/domain/1?days=90`, { waitUntil: 'networkidle' });
        await checkOverflow(page, `${label} 域名详情`);
        await checkTimeline(page, theme, width);
        if (screenshots) {
          await page.screenshot({ path: path.join(OUT_DIR, `${theme}-${width}-detail.png`), fullPage: true });
        }

        await page.goto(`${base}/settings`, { waitUntil: 'networkidle' });
        await checkOverflow(page, `${label} 设置`);

        await page.goto(`${base}/domain/new`, { waitUntil: 'networkidle' });
        await checkOverflow(page, `${label} 添加域名`);

        await page.goto(`${base}/`, { waitUntil: 'networkidle' });
        if (screenshots) {
          await page.screenshot({ path: path.join(OUT_DIR, `${theme}-${width}-index.png`), fullPage: true });
        }
        await context.close();
      }
    }
  } finally {
    await browser.close();
  }

  console.log(`\n截图: ${OUT_DIR}`);
  if (failures.length) {
    console.error(`\n视觉/响应式检查失败 ${failures.length} 项：`);
    for (const f of failures) console.error(' - ' + f);
    return 1;
  }
  console.log(`\n视觉/响应式检查通过：${THEMES.length} 主题 × ${WIDTHS.length} 宽度。`);
  return 0;
}

run().then((code) => {
  if (server && !keep) server.kill('SIGTERM');
  process.exit(code);
}).catch((err) => {
  console.error(err.stack || err.message);
  if (server && !keep) server.kill('SIGTERM');
  process.exit(1);
});
