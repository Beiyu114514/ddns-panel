#!/usr/bin/env node
/**
 * UI 交互回归：自行创建临时数据库与本机服务，DNS/Telegram 全部模拟。
 * 与 visual.mjs 使用相同的 PYTHON、PLAYWRIGHT_MODULE、CHROME_BIN 环境变量。
 * 结果及截图写入 artifacts/ui-interactions/；不操作部署数据或外部服务。
 */
import assert from 'node:assert/strict';
import { createRequire } from 'node:module';
import { spawn } from 'node:child_process';
import fs from 'node:fs';
import os from 'node:os';
import path from 'node:path';
import { fileURLToPath } from 'node:url';

const require = createRequire(import.meta.url);
const root = path.dirname(path.dirname(fileURLToPath(import.meta.url)));
const python = process.env.PYTHON || path.join(root, '.venv/bin/python');
const candidates = [process.env.PLAYWRIGHT_MODULE,
  ...(process.env.NODE_PATH || '').split(path.delimiter).filter(Boolean).map(p => path.join(p, 'playwright')),
  'playwright'];
let chromium;
for (const candidate of candidates.filter(Boolean)) {
  try { ({ chromium } = require(candidate)); break; } catch { /* 试下一个安装位置 */ }
}
if (!chromium) { console.error('找不到 Playwright，请设置 PLAYWRIGHT_MODULE。'); process.exit(2); }
const dataDir = fs.mkdtempSync(path.join(os.tmpdir(), 'ddns-interactions-'));
const output = path.join(root, 'artifacts/ui-interactions');
fs.mkdirSync(output, { recursive: true });
const port = 18000 + Math.floor(Math.random() * 10000);
const base = `http://127.0.0.1:${port}`;
const fixture = `
import asyncio
import uvicorn
from app import db, main
from app import monitor as monitor_module
from scripts.seed_demo import seed
db.init()
seed(db, 400)
db.create_domain('home.demo.example', 'AAAA', '1.1.1.1', 300, '类型冲突')
async def resolve(name, rrtype, resolver):
    return {'status': 'ok', 'ip': '203.0.113.9', 'rtt': 12.0, 'error': None}
async def send(token, chat, text):
    await asyncio.sleep(0.5)
    return True, '本地模拟'
monitor_module.resolve_once = resolve
main.send_tg = send
async def fixture_app(scope, receive, send):
    if scope['type'] == 'http' and scope.get('query_string') == b'slow=1':
        await asyncio.sleep(45)
        await send({'type': 'http.response.start', 'status': 204, 'headers': []})
        await send({'type': 'http.response.body', 'body': b''})
        return
    await main.app(scope, receive, send)
uvicorn.run(fixture_app, host='127.0.0.1', port=${port}, lifespan='off', access_log=False, log_level='warning', timeout_graceful_shutdown=1)
`;
const server = spawn(python, ['-c', fixture], { cwd: root,
  env: { ...process.env, DDNS_DATA_DIR: dataDir, COOKIE_SECURE: 'false', PYTHONPATH: root },
  stdio: ['ignore', 'pipe', 'pipe'] });
let serverLog = '';
server.stdout.on('data', chunk => { serverLog += chunk; });
server.stderr.on('data', chunk => { serverLog += chunk; });
const results = [];
let browser;
async function check(name, run) {
  try { await run(); results.push({ name, passed: true }); console.log(`PASS ${name}`); }
  catch (error) { results.push({ name, passed: false, error: error.message }); console.error(`FAIL ${name}: ${error.stack || error}`); }
}
async function submit(page, selector, url) {
  const [, response] = await Promise.all([
    page.waitForNavigation({ waitUntil: 'load' }),
    page.waitForResponse(r => r.url() === base + url && r.request().method() === 'POST'),
    page.locator(selector).click(),
  ]);
  return response;
}
async function checkError(page, id) {
  const info = await page.locator(id).evaluate(element => {
    const r = element.getBoundingClientRect();
    return { focused: document.activeElement === element,
      visible: r.top >= 56 && r.bottom <= innerHeight };
  });
  assert.deepEqual(info, { focused: true, visible: true });
}
async function run() {
  let ready = false;
  for (let i = 0; i < 100; i++) {
    if (server.exitCode !== null) throw new Error(serverLog);
    try { ready = (await fetch(base + '/login')).ok; } catch { /* 服务尚未就绪 */ }
    if (ready) break;
    await new Promise(resolve => setTimeout(resolve, 100));
  }
  if (!ready) throw new Error(`临时服务启动失败：${serverLog}`);
  browser = await chromium.launch({ headless: true, executablePath: process.env.CHROME_BIN || undefined,
    args: ['--no-sandbox', '--disable-dev-shm-usage'] });
  const context = await browser.newContext({ viewport: { width: 1440, height: 900 }, reducedMotion: 'reduce' });
  const page = await context.newPage();
  page.setDefaultTimeout(10000);
  await page.goto(base + '/login');
  await check('登录必填校验不锁定表单', async () => {
    await page.locator('button[type=submit]').click();
    assert.equal(await page.locator('button[type=submit]').isDisabled(), false);
    assert.equal(page.url(), base + '/login');
  });
  await page.fill('#password', fs.readFileSync(path.join(dataDir, 'initial_password.txt'), 'utf8').trim());
  await Promise.all([page.waitForURL(base + '/'), page.locator('button[type=submit]').click()]);

  await check('DNS 无效时保留输入并定位字段错误', async () => {
    await page.goto(base + '/domain/new');
    await page.fill('#name', 'resolver-audit.example');
    await page.fill('#resolver', '1.1.1.999');
    await page.fill('#note', '保留备注');
    assert.equal((await submit(page, 'form[action="/domain/new"] button', '/domain/new')).status(), 400);
    assert.equal(await page.inputValue('#resolver'), '1.1.1.999');
    assert.equal(await page.inputValue('#note'), '保留备注');
    assert.equal(await page.getAttribute('#resolver', 'aria-invalid'), 'true');
    await checkError(page, '#new-errors');
  });
  await check('类型冲突保留草稿和实际运行状态', async () => {
    await page.goto(base + '/domain/1?days=90#settings');
    await page.selectOption('#rrtype', 'AAAA');
    await page.fill('#note', '冲突后保留备注');
    await page.selectOption('#interval_sec', '600');
    await page.uncheck('#enabled');
    assert.equal((await submit(page, 'form[action="/domain/1/update"] button', '/domain/1/update')).status(), 400);
    assert.equal(await page.inputValue('#rrtype'), 'AAAA');
    assert.equal(await page.inputValue('#note'), '冲突后保留备注');
    assert.equal(await page.inputValue('#interval_sec'), '600');
    assert.equal(await page.isChecked('#enabled'), false);
    assert.equal(await page.locator('.badge-row .type').innerText(), 'A');
    await checkError(page, '#domain-errors');
  });
  await check('通知凭据缺失不显示为已启用', async () => {
    await page.goto(base + '/settings');
    await page.check('#tg_enabled');
    assert.equal((await submit(page, '#notify-form button:first-of-type', '/settings/notify')).status(), 400);
    assert.equal(await page.getAttribute('#tg_token', 'aria-invalid'), 'true');
    assert.equal(await page.getAttribute('#tg_chat', 'aria-invalid'), 'true');
    assert.equal(await page.locator('section[aria-labelledby=notify-h] .badge').innerText(), '未启用');
    await checkError(page, '#notify-errors');
  });
  await check('测试保留 formaction 且全部提交入口锁定', async () => {
    await page.goto(base + '/settings');
    await page.fill('#tg_token', 'local-mock-token');
    await page.fill('#tg_chat', '12345');
    await page.check('#tg_enabled');
    const posts = [];
    const listener = request => { if (request.method() === 'POST') posts.push(new URL(request.url()).pathname); };
    page.on('request', listener);
    try {
      await Promise.all([
        page.waitForNavigation({ waitUntil: 'load' }),
        page.waitForResponse(r => r.url() === base + '/settings/test_tg' && r.request().method() === 'POST'),
        page.evaluate(() => {
        const form = document.getElementById('notify-form');
        const buttons = form.querySelectorAll('button');
        const events = [];
        form.addEventListener('submit', event => {
          events.push({ prevented: event.defaultPrevented, disabled: [...buttons].map(b => b.disabled) });
          sessionStorage.setItem('pending-events', JSON.stringify(events));
        });
        buttons[1].click();
        buttons[0].click();
        form.requestSubmit(buttons[0]);
        }),
      ]);
      assert.deepEqual(posts, ['/settings/test_tg']);
      assert.deepEqual(await page.evaluate(() => JSON.parse(sessionStorage.getItem('pending-events'))), [
        { prevented: false, disabled: [true, true] }, { prevented: true, disabled: [true, true] },
      ]);
      assert.match(await page.locator('.flash-stack').innerText(), /测试消息已发送/);
    } finally { page.off('request', listener); }
  });
  await check('清除 Token 时测试拒绝且保留原配置', async () => {
    await page.goto(base + '/settings');
    await page.check('#clear_tg_token');
    await page.fill('#tg_token', 'unsaved-replacement');
    assert.equal((await submit(page, '#notify-form button[formaction]', '/settings/test_tg')).status(), 400);
    assert.equal(await page.getAttribute('#tg_token', 'aria-invalid'), 'true');
    assert.equal(await page.inputValue('#tg_token'), '');
    await checkError(page, '#notify-errors');
    await page.goto(base + '/settings');
    assert.equal(await page.isChecked('#clear_tg_token'), false);
    assert.match(await page.getAttribute('#tg_token', 'placeholder'), /已配置/);
  });
  await check('慢请求持续锁定，取消导航后恢复按钮', async () => {
    await page.goto(base + '/settings');
    // 保持真实导航等待 31 秒，使用 CDP 读取旧文档，避免自动等待导航造成测试死锁。
    const cdp = await context.newCDPSession(page);
    try {
      console.log('  慢请求：发起原生导航');
      await page.locator('#notify-form').evaluate(form => { form.action += '?slow=1'; });
      await Promise.all([
        page.waitForRequest(r => r.url() === base + '/settings/notify?slow=1' && r.method() === 'POST'),
        cdp.send('Runtime.evaluate', { expression: "document.querySelector('#notify-form button').click()" }),
      ]);
      console.log('  慢请求：等待 31 秒');
      await new Promise(resolve => setTimeout(resolve, 31000));
      const state = await cdp.send('Runtime.evaluate', {
        expression: "[...document.querySelectorAll('#notify-form button')].map(e => e.disabled)", returnByValue: true,
      });
      assert.deepEqual(state.result.value, [true, true]);
      console.log('  慢请求：按钮保持锁定，取消导航');
      await cdp.send('Runtime.evaluate', { expression: 'window.stop()' });
      await page.waitForFunction(() => !document.querySelector('#notify-form button').disabled);
      assert.equal(await page.getAttribute('#notify-form', 'aria-busy'), null);
    } finally { await cdp.send('Page.stopLoading').catch(() => {}); await cdp.detach(); }
  });
  await check('其他监听器取消提交后恢复按钮', async () => {
    await page.goto(base + '/settings');
    await page.locator('#notify-form').evaluate(async form => {
      form.addEventListener('submit', event => event.preventDefault(), { once: true });
      form.requestSubmit(form.querySelector('button'));
      await Promise.resolve();
    });
    assert.equal(await page.locator('#notify-form button').first().isDisabled(), false);
    assert.equal(await page.getAttribute('#notify-form', 'aria-busy'), null);
  });
  await check('提交成功后通过浏览器历史返回可再次保存', async () => {
    await page.goto(base + '/settings');
    assert.equal((await submit(page, '#notify-form button:first-of-type', '/settings/notify')).status(), 303);
    await page.goBack({ waitUntil: 'load' });
    assert.equal(page.url(), base + '/settings');
    assert.deepEqual(await page.locator('#notify-form button').evaluateAll(es => es.map(e => e.disabled)), [false, false]);
    assert.equal(await page.getAttribute('#notify-form', 'aria-busy'), null);
    assert.match(await page.locator('#notify-form button').last().innerText(), /发送测试消息/);
    assert.equal((await submit(page, '#notify-form button:first-of-type', '/settings/notify')).status(), 303);
  });
  await check('密码错误自动定位且设置导航保持选中', async () => {
    await page.goto(base + '/settings');
    await page.fill('#current_password', 'wrong-audit-password');
    await page.fill('#password', 'new-password-a');
    await page.fill('#password2', 'new-password-b');
    assert.equal((await submit(page, 'form[action="/settings/password"] button', '/settings/password')).status(), 400);
    await checkError(page, '#password-errors');
    assert.equal(await page.locator('.sidebar a[href="/settings"][aria-current="page"]').count(), 1);
    await page.screenshot({ path: path.join(output, 'password-error-focus.png') });
  });
  await check('记录分页保留范围、锚点与阅读位置', async () => {
    await page.goto(base + '/domain/1?days=90#changes');
    await Promise.all([
      page.waitForNavigation({ waitUntil: 'load' }),
      page.locator('.pager a').last().click(),
    ]);
    assert.equal(new URL(page.url()).hash, '#changes');
    assert.equal(new URL(page.url()).searchParams.get('days'), '90');
    assert.ok(await page.locator('#changes').evaluate(e => Math.abs(e.getBoundingClientRect().top) < 100));
  });
  const mobileContext = await browser.newContext({ viewport: { width: 390, height: 844 }, isMobile: true, hasTouch: true });
  await mobileContext.addCookies(await context.cookies());
  const mobile = await mobileContext.newPage();
  await check('触屏和鼠标选择实际时间段，键盘继续工作', async () => {
    for (const device of [page, mobile]) {
      await device.goto(base + '/domain/1?days=90');
      await device.locator('#tl-track').scrollIntoViewIfNeeded();
      const box = await device.locator('#tl-track').boundingBox();
      const point = { x: box.x + box.width * 0.9, y: box.y + box.height * 0.5 };
      const expected = await device.evaluate(p => document.elementFromPoint(p.x, p.y).getAttribute('data-label'), point);
      assert.ok(expected);
      if (device === mobile) await device.touchscreen.tap(point.x, point.y);
      else await device.mouse.click(point.x, point.y);
      assert.equal(await device.locator('#tl-tip').textContent(), expected);
      await device.locator('#tl-track').focus();
      await device.keyboard.press('ArrowRight');
      assert.notEqual(await device.locator('#tl-tip').textContent(), expected);
    }
    await mobile.screenshot({ path: path.join(output, 'mobile-timeline-selection.png') });
  });
  await mobileContext.close();
  await context.close();
}
try { await run(); }
catch (error) { console.error(error.stack || error); results.push({ name: '浏览器运行', passed: false, error: error.message }); }
finally {
  if (browser) await browser.close();
  server.kill('SIGTERM');
  await new Promise(resolve => { if (server.exitCode !== null) resolve(); else server.once('exit', resolve); });
  fs.rmSync(dataDir, { recursive: true, force: true });
  fs.writeFileSync(path.join(output, 'results.json'), JSON.stringify(results, null, 2));
}
const failed = results.filter(row => !row.passed).length;
console.log(`交互回归：${results.length - failed} 通过，${failed} 失败。`);
process.exitCode = failed ? 1 : 0;
