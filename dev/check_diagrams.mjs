// Layout check for the guide's diagrams (dev only). For every diagram SVG on a page: no two boxes overlap, every edge
// starts and ends on (within 2 px of) its boxes, and no edge label covers a box. Optionally saves a screenshot of each.
//
//   NODE_PATH=/path/to/node_modules node dev/check_diagrams.mjs URL [--shots DIR] [--width 1440] [--dark] [--wrap 1600]
//
// URL is a review page served by serve.py. Exits 1 and prints each problem when a check fails.
import { createRequire } from 'node:module';
import { mkdirSync } from 'node:fs';

const require = createRequire(import.meta.url);
const { chromium } = require('playwright');
const args = process.argv.slice(2);
const url = args[0]?.startsWith('http') ? args[0] : null;
const opt = (k, d) => (args.includes(k) ? args[args.indexOf(k) + 1] : d);
const shots = opt('--shots'), width = +opt('--width', 1440), dark = args.includes('--dark');
if (!url) { console.error('usage: check_diagrams.mjs URL [--shots DIR] [--width N] [--dark]'); process.exit(2); }

const browser = await chromium.launch();
const page = await browser.newPage({ viewport: { width, height: 2400 }, colorScheme: dark ? 'dark' : 'light', deviceScaleFactor: 2 });
const errors = [];
page.on('pageerror', (e) => errors.push(`page error: ${e.message}`));
await page.goto(url);
// The saved page state may open a file; the diagrams live on the guide. --wrap N widens the content column (the page
// frame caps it today; the planned frame uses up to 1600 px).
const wrap = opt('--wrap');
await page.waitForTimeout(500); // the first state poll renders once more
await page.evaluate((wrap) => {
  if (wrap) document.head.append(Object.assign(document.createElement('style'), { textContent: `.wrap{max-width:${wrap}px}` }));
  state.view = 'guide'; render();
}, wrap);
await page.waitForSelector('svg.dg', { timeout: 10000 });
await page.evaluate(() => document.fonts.ready);
await page.waitForTimeout(300);

const problems = await page.evaluate(() => {
  const out = [];
  const inter = (a, b, m = 0) => a.x + m < b.x + b.width && b.x + m < a.x + a.width && a.y + m < b.y + b.height && b.y + m < a.y + a.height;
  const near = (p, b, tol) => p.x >= b.x - tol && p.x <= b.x + b.width + tol && p.y >= b.y - tol && p.y <= b.y + b.height + tol;
  document.querySelectorAll('svg.dg').forEach((sv, k) => {
    const name = `${sv.dataset.kind} "${sv.getAttribute('aria-label')}"`;
    const boxes = new Map([...sv.querySelectorAll('.dn[data-id]')].map((g) => [g.dataset.id, g.querySelector('.box').getBBox()]));
    const ids = [...boxes.keys()];
    for (let i = 0; i < ids.length; i++) for (let j = i + 1; j < ids.length; j++)
      if (inter(boxes.get(ids[i]), boxes.get(ids[j]), 0.5)) out.push(`${name}: boxes ${ids[i]} and ${ids[j]} overlap`);
    const notes = [...sv.querySelectorAll('.dnote .box')].map((r) => r.getBBox());
    sv.querySelectorAll('.de').forEach((g) => {
      if (g.classList.contains('dlg')) return;
      const line = g.querySelector('.line'), [a, b] = g.dataset.n.split(' ');
      const len = line.getTotalLength(), p0 = line.getPointAtLength(0), p1 = line.getPointAtLength(len);
      if (g.classList.contains('seq')) {
        // A message runs between lifelines: its ends sit on the two participants' lifeline x.
        const lx = (id) => +sv.querySelector(`.life[data-life="${CSS.escape(id)}"]`).getAttribute('x1');
        if (Math.abs(p0.x - lx(a)) > 2 || Math.abs(p1.x - lx(b)) > 2) out.push(`${name}: message ${a}->${b} does not join its lifelines`);
        return;
      }
      const ends = [[p0, p1], [p1, p0]].some(([s, t]) => near(s, boxes.get(a), 2) && near(t, boxes.get(b), 2));
      if (!ends) out.push(`${name}: edge ${a}->${b} does not start and end on its boxes`);
      // No edge runs through a box it does not join.
      for (const [id, bx] of boxes) {
        if (id === a || id === b) continue;
        const inner = { x: bx.x + 3, y: bx.y + 3, width: bx.width - 6, height: bx.height - 6 };
        for (let s = 0; s <= len; s += 4) if (near(line.getPointAtLength(s), inner, 0)) { out.push(`${name}: edge ${a}->${b} crosses box ${id}`); break; }
      }
    });
    sv.querySelectorAll('.dlg .lbg').forEach((r) => {
      const lb = r.getBBox(), g = r.closest('.dlg');
      for (const [id, b] of boxes) if (inter(lb, b, 0.5)) out.push(`${name}: label "${g.textContent.trim()}" covers box ${id}`);
      for (const b of notes) if (inter(lb, b, 0.5)) out.push(`${name}: label "${g.textContent.trim()}" covers a note`);
    });
    sv.querySelectorAll('.dlg .lbg').forEach((r, i, all) => {
      for (let j = i + 1; j < all.length; j++) if (inter(r.getBBox(), all[j].getBBox(), 0.5)) out.push(`${name}: two labels overlap`);
    });
    const scale = sv.getBoundingClientRect().width / +sv.dataset.w;
    if (scale < 0.79) out.push(`${name}: drawn at ${scale.toFixed(2)}, under the readable minimum`);
  });
  return out;
});

if (shots) {
  mkdirSync(shots, { recursive: true });
  const n = await page.locator('.dgm').count();
  for (let i = 0; i < n; i++) {
    const frame = page.locator('.dgm').nth(i);
    const kind = await frame.locator('svg.dg').getAttribute('data-kind');
    await frame.scrollIntoViewIfNeeded();
    await frame.screenshot({ path: `${shots}/${String(i + 1).padStart(2, '0')}-${kind}-${width}-${dark ? 'dark' : 'light'}.png` });
  }
}
await browser.close();
const all = [...errors, ...problems];
console.log(all.length ? all.join('\n') : `ok: every diagram at ${width}px ${dark ? 'dark' : 'light'}`);
process.exit(all.length ? 1 : 0);
