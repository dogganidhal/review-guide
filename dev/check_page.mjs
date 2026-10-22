// Dev-only: loads a review page in a browser and checks the page frame.
//   NODE_PATH=/tmp/rg-video/node_modules node dev/check_page.mjs <review page url> [--shots <dir>]
// The fixture home comes from `test_scripts.py --keep <home>`; serve it with serve.py and pass a /r/<id> URL.
import { createRequire } from 'node:module';
import { mkdirSync } from 'node:fs';
const { chromium } = createRequire(process.cwd() + '/')('playwright');

const url = process.argv[2];
const shots = process.argv.includes('--shots') ? process.argv[process.argv.indexOf('--shots') + 1] : null;
if (!url) { console.error('usage: check_page.mjs <url> [--shots <dir>]'); process.exit(2); }
if (shots) mkdirSync(shots, { recursive: true });

const fails = [], errors = [];
const check = (ok, what) => { console.log(`${ok ? 'ok  ' : 'FAIL'} ${what}`); if (!ok) fails.push(what); };
const browser = await chromium.launch();

async function open(width, height = 900) {
  const page = await browser.newPage({ viewport: { width, height } });
  page.on('pageerror', (e) => errors.push(`pageerror: ${e.message}`));
  page.on('console', (m) => m.type() === 'error' && !/fonts\.g|cdnjs|ERR_|Failed to load resource/.test(m.text() + m.location().url) && errors.push(`console: ${m.text()}`));
  await page.goto(url, { waitUntil: 'load' });
  await page.waitForSelector('.topbar');
  await page.evaluate(() => { state.rail = null; state.origin = state.nav = null; state.view = 'guide'; render(); $main().scrollTo(0, 0); });
  return page;
}
const shot = (page, name) => shots && page.screenshot({ path: `${shots}/${name}.png` });

for (const width of [1440, 1920]) {
  const page = await open(width);
  const h = await page.evaluate(() => document.getElementById('top').getBoundingClientRect().height);
  check(h < 64, `top bar is one line at ${width}px (${Math.round(h)}px)`);
  await shot(page, `${width}-guide-top`);
  const main = await page.evaluate(() => { const m = document.getElementById('main'); return { left: m.getBoundingClientRect().left, w: document.querySelector('.wrap').getBoundingClientRect().width }; });
  check(width < 1900 || main.w > 1300, `guide uses the width at ${width}px (${Math.round(main.w)}px)`);

  // Stage links scroll to their part and mark it.
  const stage = await page.evaluate(async () => {
    const out = {};
    for (const id of ['findings', 'read', 'understand']) {
      const b = document.querySelector(`.stage[data-s=${id}]`);
      if (!b) continue;
      b.click(); await new Promise((r) => setTimeout(r, 900));
      out[id] = document.querySelector('.stage[aria-current=true]')?.dataset.s;
    }
    return out;
  });
  check(Object.entries(stage).every(([k, v]) => k === v), `stage links scroll and mark the stage ${JSON.stringify(stage)}`);
  await page.evaluate(() => $main().scrollTo(0, $main().scrollHeight / 2));
  await page.waitForTimeout(300);
  await shot(page, `${width}-guide-mid`);

  // Open a file from a step, then Back must restore the scroll.
  const st = await page.evaluate(() => {
    const steps = allSteps(ST());
    if (!steps.length) return null;
    const e = steps[Math.min(1, steps.length - 1)];
    state.open[e.s.id] = true; render();
    document.getElementById(e.s.id).scrollIntoView({ block: 'start' });
    return e.s.id;
  });
  if (st) {
    await page.waitForTimeout(300);
    const before = await page.evaluate(() => $main().scrollTop);
    await page.locator(`[id="${st}"] a`, { hasText: 'Whole file' }).click();
    await page.waitForSelector('.crumbs');
    check(await page.evaluate(() => state.view === 'file' && !!document.querySelector('.crumbs .crumb.cur')), `file opens from a step with a breadcrumb: ${await page.locator('.crumbs').innerText().then((t) => t.replace(/\s+/g, ' '))}`);
    check(await page.locator('.walkers').count() > 0, 'step walkers are shown');
    await shot(page, `${width}-file-from-step`);
    await page.getByRole('button', { name: '← Back' }).click();
    await page.waitForTimeout(300);
    const after = await page.evaluate(() => $main().scrollTop);
    check(Math.abs(after - before) <= 5, `Back restores the scroll (${before} to ${after})`);
  } else console.log('skip no step in this guide');

  // Send view.
  await page.locator('.stage[data-s=send]').click();
  await page.waitForTimeout(300);
  await shot(page, `${width}-send`);
  const dims = await page.evaluate(() => document.documentElement.scrollWidth - innerWidth);
  check(dims <= 0, `no horizontal page scroll at ${width}px`);
  await page.close();
}

// Mobile.
{
  const page = await open(390, 844);
  const x = async () => page.evaluate(() => ({ doc: document.documentElement.scrollWidth - innerWidth, body: document.body.scrollWidth - innerWidth, main: document.getElementById('main').scrollWidth - document.getElementById('main').clientWidth }));
  const h = await page.evaluate(() => document.getElementById('top').getBoundingClientRect().height);
  await shot(page, '390-guide-top');
  check((await x()).doc <= 0 && (await x()).body <= 0, `no horizontal page scroll at 390px ${JSON.stringify(await x())}`);
  await page.evaluate(() => $main().scrollTo(0, 1200));
  await page.waitForTimeout(200);
  await shot(page, '390-guide-mid');
  await page.locator('.railbtn').click();
  await shot(page, '390-rail');
  await page.locator('.railbtn').click();
  const f = await page.evaluate(() => { const s = ST(); const f = s.files[0]; openFile(f.path); return f.path; });
  await page.waitForTimeout(300);
  await shot(page, '390-file');
  check((await x()).doc <= 0, `no horizontal page scroll in a file at 390px ${JSON.stringify(await x())}`);
  await page.evaluate(() => go('send'));
  await shot(page, '390-send');
  check((await x()).doc <= 0, 'no horizontal page scroll in Your review at 390px');
  await page.close();
}

check(!errors.length, `no page or console errors${errors.length ? ': ' + errors.slice(0, 5).join(' | ') : ''}`);
await browser.close();
process.exit(fails.length ? 1 : 0);
