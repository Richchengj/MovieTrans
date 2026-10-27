// WebUI 浏览器层冒烟（由 scripts/webui_smoke.py --ui 调用）
// 用法：node webui_smoke_ui.js http://127.0.0.1:8798 <project_id>
// 需要 playwright（NODE_PATH 指向已安装的 node_modules）；用系统 Edge，免下浏览器。
const { chromium } = require('playwright');

const [, , base, project] = process.argv;
if (!base || !project) {
  console.error('用法: node webui_smoke_ui.js <base_url> <project_id>');
  process.exit(2);
}

let fails = 0;
function check(name, ok, detail = '') {
  console.log(`[${ok ? 'PASS' : 'FAIL'}] ${name}${detail ? ': ' + detail : ''}`);
  if (!ok) fails++;
  return ok;
}

(async () => {
  const browser = await chromium.launch({ channel: 'msedge', headless: true });
  const page = await browser.newPage({ viewport: { width: 1600, height: 1000 } });
  const errors = [];
  page.on('pageerror', (e) => errors.push('pageerror: ' + e.message));
  page.on('console', (m) => {
    if (m.type() === 'error') errors.push('console: ' + m.text());
  });

  await page.goto(base, { waitUntil: 'networkidle' });

  // 1 项目下拉
  const opts = await page.$$eval('#project_select option', (els) =>
    els.map((e) => e.value).filter(Boolean));
  check('项目下拉有选项', opts.length > 0, `${opts.length} 个`);

  await page.selectOption('#project_select', project);
  await page.waitForTimeout(1500);

  // 2 翻译后端徽标（renderBackendBadge 有产出）
  const backend = ((await page.textContent('#cfg_backend')) || '').trim();
  check('翻译徽标有文案', backend.length > 0, backend);

  // 3 配置页：TTS 引擎三选一
  await page.click('nav button[data-tab="config"]');
  await page.waitForTimeout(600);
  const engines = await page.$$eval('#tts_engine option', (els) =>
    els.map((e) => e.value));
  check('TTS 引擎三选一',
    ['indextts2', 'gpt_sovits', 'siliconflow'].every((e) => engines.includes(e)),
    engines.join(','));

  const ttsBadge = ((await page.textContent('#cfg_tts_backend')) || '').trim();
  check('TTS 就绪徽标有文案', ttsBadge.length > 0, ttsBadge);

  // 4 云端引擎：显示 Key 行、隐藏本机设备行
  await page.selectOption('#tts_engine', 'siliconflow');
  await page.waitForTimeout(350);
  check('选云端：显示 API Key 行', await page.isVisible('#sf_key_row'));
  check('选云端：隐藏本机设备行', !(await page.isVisible('#tts_device_row')));

  // 5 GSV：显示部署路径、隐藏语气模式
  await page.selectOption('#tts_engine', 'gpt_sovits');
  await page.waitForTimeout(350);
  check('选 GSV：显示部署路径', await page.isVisible('#gsv_home_row'));
  check('选 GSV：隐藏语气模式', !(await page.isVisible('#emo_mode_row')));

  // 6 主力引擎：设备行恢复
  await page.selectOption('#tts_engine', 'indextts2');
  await page.waitForTimeout(350);
  check('选主力：设备行恢复', await page.isVisible('#tts_device_row'));

  // 7 复核页：R2 合并对照表有数据行
  await page.click('nav button[data-tab="review"]');
  await page.waitForTimeout(2600);
  const rows = await page.$$eval('#tr_body tr', (els) => els.length);
  check('对照表有数据行', rows > 0, `${rows} 行`);

  // 8 交付页：质检面板在位
  await page.click('nav button[data-tab="output"]');
  await page.waitForTimeout(1600);
  check('交付页有质检按钮', await page.isVisible('#out_qc_btn'));

  // 9 全程零 JS 错误
  check('零 JS 错误', errors.length === 0, errors.slice(0, 3).join(' | '));

  await browser.close();
  console.log(fails === 0 ? 'UI SMOKE OK' : `UI SMOKE FAILED (${fails} 项)`);
  process.exit(fails === 0 ? 0 : 1);
})().catch((e) => {
  console.error('FATAL ' + (e && e.message ? e.message : e));
  process.exit(2);
});
