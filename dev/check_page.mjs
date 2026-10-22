// Dev-only: loads a review page in a browser and checks the page frame.
//   NODE_PATH=/tmp/rg-video/node_modules node dev/check_page.mjs <review page url> [--shots <dir>] [--stub]
// The fixture home comes from `test_scripts.py --keep <home>`; serve it with serve.py and pass a /r/<id> URL.
// --stub also checks the questions (asking from a finding, the thread, a path:line link and Back, 390 px wide). Serve with
//   REVIEW_GUIDE_CLAUDE=dev/stub_claude.py serve.py --home <home> --port 0
// so answers come from the stub; it asks on the first finding that has a suggested card, so it needs a guide with findings.
import { createRequire } from 'node:module';
import { mkdirSync } from 'node:fs';
const { chromium } = createRequire(process.cwd() + '/')('playwright');

const url = process.argv[2];
const stub = process.argv.includes('--stub');
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

// Questions, against the stub agent: ask from a finding, read the thread, follow a path:line link, come back, check 390 px.
async function questions() {
  const page = await open(1440);
  const pick = await page.evaluate(() => {
    const c = ST().comments.find((x) => x.axis && !cmState(x).gone && !cmState(x).on);
    if (!c) return null;
    state.open[`axis:${ST().id}:${c.axis}`] = true; render();
    document.getElementById(`axis-${c.axis}`).scrollIntoView({ block: 'start' });
    return { id: c.id, axis: c.axis };
  });
  if (!pick) { console.log('skip questions: no suggested finding in this guide'); await page.close(); return; }
  const card = page.locator(`[data-cid="${pick.id}"]`);
  await card.getByRole('button', { name: 'Ask', exact: true }).click();
  check(await page.locator('.askbox .tchip').innerText().then((t) => t.startsWith('Finding: ')), 'the ask box names its target');
  await page.locator('.askbox .fchip', { hasText: 'Is this real?' }).click();
  check(await page.locator('[data-asktext]').inputValue() === 'Is this real?', 'a starter fills the box');
  await page.keyboard.press('Control+Enter');
  await page.waitForSelector('.thr .tlive');
  await shot(page, '1440-thread-live');
  await page.waitForSelector('.thr .tfu', { timeout: 20000 });  // the follow-up box shows once the answer is complete
  check(await page.locator('.thr .tmsg.ag .av').first().innerText() === '✦', 'the answer carries the ✦ mark');
  check(await page.locator('.thr .tmsg.me .tq').first().innerText() === 'Is this real?', 'the thread shows the question');
  check(await page.locator('#qgrp .oi.qi').count() === 1, 'the rail lists the thread under Questions');
  const link = page.locator('.thr a.tref').first();
  check(await link.count() > 0, 'the answer has a path:line link');
  await page.evaluate((id) => { document.querySelector(`[data-cid="${id}"]`).scrollIntoView({ block: 'start' }); $main().scrollBy(0, 120); }, pick.id);
  await page.waitForTimeout(300);
  await shot(page, '1440-thread-finding');
  const before = await page.evaluate(() => $main().scrollTop);
  await link.click();
  await page.waitForSelector('.crumbs');
  const trail = await page.locator('.crumbs').innerText().then((t) => t.replace(/\s+/g, ' '));
  check(await page.evaluate(() => state.view === 'file') && /Question: Finding: /.test(trail), `a link in the answer opens the file, with the thread in the breadcrumb: ${trail}`);
  check(await page.locator('.ln.anchor').count() > 0, 'the file view outlines the cited line');
  await page.getByRole('button', { name: '← Back' }).click();
  await page.waitForSelector('.thr');
  await page.waitForTimeout(300);
  const after = await page.evaluate(() => $main().scrollTop);
  check(Math.abs(after - before) <= 5, `Back returns to the thread (${before} to ${after})`);
  await page.close();

  // The same thread on a phone: nothing sticks out sideways.
  const m = await open(390, 844);
  await m.evaluate((id) => { const c = ST().comments.find((x) => x.id === id); state.open[`axis:${ST().id}:${c.axis}`] = true; render(); document.getElementById('th-' + [...TH.values()][0].id).scrollIntoView({ block: 'center' }); }, pick.id);
  await m.waitForTimeout(300);
  await shot(m, '390-thread');
  const over = await m.evaluate(() => ({ doc: document.documentElement.scrollWidth - innerWidth, main: $main().scrollWidth - $main().clientWidth, thr: document.querySelector('.thr').getBoundingClientRect().right - innerWidth }));
  check(over.doc <= 0 && over.main <= 0 && over.thr <= 0, `no horizontal scroll at 390px with a thread open ${JSON.stringify(over)}`);
  await m.keyboard.press('?');
  check(await m.locator('.askbox').count() === 1, '? opens a question box');
  await m.close();
}
if (stub) await questions();

check(!errors.length, `no page or console errors${errors.length ? ': ' + errors.slice(0, 5).join(' | ') : ''}`);
await browser.close();
process.exit(fails.length ? 1 : 0);
