// Dev-only: opens an exported (shared) guide from disk and checks it works without a server.
//   NODE_PATH=/tmp/rg-video/node_modules node dev/check_shared.mjs <exported.html> [--server <base of a fixture server>] [--shots <dir>]
// Export the file from a fixture home (`test_scripts.py --keep <home>`, then serve it) with
//   POST /api/r/<id>/<n>/export  (header X-Review-Token from <home>/server.json, body {"mine": true})
// Pass 1 blocks every network request except file:, data: and blob:, and checks that none is attempted but the loopback probe.
// Pass 2 (with --server) lets the probe through to the fixture server (the page asks 127.0.0.1:7357; the check answers it from --server), then
// checks the band offers the import and that it ends on the new review.
import { createRequire } from 'node:module';
import { mkdirSync, readFileSync } from 'node:fs';
const { chromium } = createRequire(process.cwd() + '/')('playwright');

const file = process.argv[2];
const arg = (k) => (process.argv.includes(k) ? process.argv[process.argv.indexOf(k) + 1] : null);
const server = arg('--server'), shots = arg('--shots');
if (!file) { console.error('usage: check_shared.mjs <exported.html> [--server <base>] [--shots <dir>]'); process.exit(2); }
if (shots) mkdirSync(shots, { recursive: true });

const fails = [];
const check = (ok, what) => { console.log(`${ok ? 'ok  ' : 'FAIL'} ${what}`); if (!ok) fails.push(what); };
const PING = /^http:\/\/127\.0\.0\.1:\d+\/api\/ping$/;
const browser = await chromium.launch();

async function open(width, { probe } = {}) {
  const ctx = await browser.newContext({ viewport: { width, height: 900 }, acceptDownloads: true });
  const seen = { requests: [], errors: [] };
  await ctx.route('**/*', async (route) => {
    const req = route.request(), u = req.url();
    if (/^(file|data|blob):/.test(u)) return route.continue();
    seen.requests.push(u);
    if (probe && server && /^http:\/\/127\.0\.0\.1:7357\//.test(u)) return route.fulfill({ response: await route.fetch({ url: u.replace('http://127.0.0.1:7357', server), method: req.method(), headers: req.headers(), postData: req.postDataBuffer() }) });
    if (probe && server && u.startsWith(server)) return route.continue();
    return route.abort();
  });
  const page = await ctx.newPage();
  page.on('pageerror', (e) => seen.errors.push(`pageerror: ${e.message}`));
  page.on('console', (m) => m.type() === 'error' && !PING.test(m.location().url) && !/ERR_FAILED/.test(m.text()) && seen.errors.push(`console: ${m.text()}`));
  await page.goto('file://' + file, { waitUntil: 'load' });
  await page.waitForSelector('.topbar');
  await page.evaluate(() => { state.rail = null; state.origin = state.nav = null; state.view = 'guide'; render(); $main().scrollTo(0, 0); });
  return { page, ctx, seen };
}
const shot = (page, name) => shots && page.screenshot({ path: `${shots}/shared-${name}.png` });

// Pass 1: no network.
{
  const { page, ctx, seen } = await open(1440);
  await page.waitForTimeout(500);
  check(seen.requests.every((u) => PING.test(u)), `no request but the loopback probe ${JSON.stringify(seen.requests)}`);
  check(await page.locator('.sharedband').count() === 1 && (await page.locator('.sharedband').innerText()).includes('Shared review guide'), 'the band says it is a shared guide');
  check(await page.locator('.top a[href="/"]').count() === 0, 'no link back to an inbox');
  check(await page.locator('.askrail, .askbtn').count() === 0, 'no Ask');
  check(await page.locator('text=Open in my review-guide').count() === 0, 'Open in my review-guide is hidden while no server answers');
  await shot(page, 'top');

  // Diagrams render, a box opens its file, Back returns to the diagram.
  const dg = page.locator('.dgm').first();
  if (await dg.count()) {
    check(await dg.locator('svg').count() > 0, 'a diagram renders');
    await dg.scrollIntoViewIfNeeded();
    await page.waitForTimeout(300);
    const link = dg.locator('.dn.link').first();
    if (await link.count()) {
      await link.click();
      await page.waitForSelector('.crumbs');
      const top = await page.evaluate(() => state.origin.top);
      check(await page.evaluate(() => state.view === 'file'), 'a diagram box opens its file');
      await page.getByRole('button', { name: '← Back' }).click();
      await page.waitForTimeout(1200);
      const after = await page.evaluate(() => $main().scrollTop);
      check(Math.abs(after - top) <= 5, `Back returns to the diagram (${top} to ${after})`);
    }
  } else console.log('skip no diagram in this guide');

  // Draft a comment on a finding and a verdict: it survives a reload, with one place that holds it.
  const add = page.locator('button:has-text("Add to my review")').first();
  if (await add.count()) {
    await add.click();
    await page.evaluate(() => { const p = ST().prs[0]; verdictState(p).body = 'a draft summary'; save(); });
    await page.waitForTimeout(600);
    await page.reload({ waitUntil: 'load' });
    await page.waitForSelector('.topbar');
    check(await page.evaluate(() => Object.values(state.cm).some((c) => c.on) && Object.values(state.verdict).some((v) => v.body === 'a draft summary')), 'a draft survives a reload');
    check(await page.evaluate(() => Object.keys(localStorage).every((k) => k.startsWith('rg-draft:'))), 'localStorage holds only the draft key');

    // Your review: the command, in full, with a quoted heredoc; no Post.
    await page.evaluate(() => go('send'));
    if (!(await page.evaluate(() => SELF))) {
      await page.evaluate(() => { for (const p of ST().prs) verdictState(p).event = 'COMMENT'; render(); });
      const copy = page.locator('.bulk .btn.primary');
      check((await copy.innerText()).startsWith('Copy the post command'), 'Post is "Copy the post command"');
      await copy.click();
      const cmd = await page.locator('.dlg pre.cmd').first().innerText();
      check(/^gh api --method POST repos\/[\w.-]+\/[\w.-]+\/pulls\/\d+\/reviews --input - <<'REVIEW_GUIDE_JSON'\n/.test(cmd) && cmd.trimEnd().endsWith('\nREVIEW_GUIDE_JSON'), 'the command is a gh api with a quoted heredoc');
      const json = cmd.split('\n').slice(1, -1).join('\n');
      let parsed = null;
      try { parsed = JSON.parse(json); } catch (e) {}
      check(!!parsed && /^[0-9a-f]{40}$/.test(parsed.commit_id) && Array.isArray(parsed.comments), 'the heredoc holds the review JSON, on the reviewed commit');
      await shot(page, 'command');
      await page.keyboard.press('Escape');
    }
  }

  // Download with my comments: a copy that reopens with the draft.
  await page.evaluate(() => go('guide'));
  const [dl] = await Promise.all([page.waitForEvent('download'), page.locator('.sharedband button:has-text("Download with my comments")').click()]);
  const copyPath = `/tmp/shared-check-${Date.now()}.html`;
  await dl.saveAs(copyPath);
  const html = readFileSync(copyPath, 'utf8');
  check(/-with-my-comments\.html$/.test(dl.suggestedFilename()) && html.includes('id="rg-bundle"') && html.includes('id="rg-seed"'), `Download with my comments saves a copy of the file (${dl.suggestedFilename()})`);
  const again = await ctx.newPage();
  const errs = [];
  again.on('pageerror', (e) => errs.push(e.message));
  await again.goto('file://' + copyPath);
  await again.waitForSelector('.topbar');
  check(await again.evaluate(() => Object.values(state.cm).some((c) => c.on)) === await page.evaluate(() => Object.values(state.cm).some((c) => c.on)) && !errs.length, 'the copy reopens with the draft');
  check(seen.errors.length === 0, `no page error ${JSON.stringify(seen.errors.slice(0, 3))}`);
  await ctx.close();
}

// Narrow screen: the band and the page do not scroll sideways.
{
  const { page, ctx } = await open(390);
  await shot(page, '390-top');
  check(await page.evaluate(() => document.documentElement.scrollWidth - innerWidth) <= 0, 'no horizontal page scroll at 390px');
  await ctx.close();
}

// Pass 2: a server answers the probe.
if (server) {
  const { page, ctx, seen } = await open(1440, { probe: true });
  await page.waitForSelector('text=Open in my review-guide', { timeout: 5000 }).catch(() => {});
  check(await page.locator('text=Open in my review-guide').count() === 1, 'the band offers the import once a server answers the probe');
  const popup = page.waitForEvent('popup');
  await page.locator('text=Open in my review-guide').click();
  const pop = await popup;
  await pop.waitForSelector('#go', { timeout: 20000 });
  check((await pop.locator('h1').innerText()).startsWith('Import the shared review'), 'the server answers with its own confirmation page');
  await shot(pop, 'import-confirm');
  await pop.locator('#go').click();
  await pop.waitForURL(/\/r\//, { timeout: 60000 });
  await pop.waitForSelector('.topbar');
  check((await pop.locator('.sharedband.imported').innerText()).startsWith('Shared by'), 'the import ends on the new review, marked as shared');
  check(seen.errors.length === 0, `no page error ${JSON.stringify(seen.errors.slice(0, 3))}`);
  await ctx.close();
}

await browser.close();
process.exit(fails.length ? 1 : 0);
