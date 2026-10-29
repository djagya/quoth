/* Accessibility audit of the preview page (synthetic data): text contrast in light and dark, keyboard order,
   the accessibility tree and reduced motion. Same setup as shoot.cjs:
   NODE_PATH=<dir with playwright> node tests/webui-preview/audit.cjs [base-url] */
const {chromium} = require('playwright');

const base = (process.argv[2] || 'http://127.0.0.1:8765') + '/tests/webui-preview/index.html';
const VIEWS = ['Overview', 'Review queue', 'Memories', 'Core', 'Refresh history'];

async function settle(page) {
  await page.waitForSelector('.quoth-content .quoth-view > *');
  await page.waitForFunction(() => !document.querySelector('.quoth-content[aria-busy]'));
}
// Every visible text run against the first opaque background behind it; WCAG AA thresholds.
function contrast() {
  const ctx = document.createElement('canvas').getContext('2d', {willReadFrequently: true});
  const rgba = color => { ctx.clearRect(0, 0, 1, 1); ctx.fillStyle = '#000'; ctx.fillStyle = color; ctx.fillRect(0, 0, 1, 1); return [...ctx.getImageData(0, 0, 1, 1).data]; };
  const lum = ([r, g, b]) => [r, g, b].map(v => { v /= 255; return v <= .03928 ? v / 12.92 : ((v + .055) / 1.055) ** 2.4; })
    .reduce((a, v, i) => a + v * [.2126, .7152, .0722][i], 0);
  const background = el => {
    const layers = [];
    for (let e = el; e; e = e.parentElement) {
      const c = rgba(getComputedStyle(e).backgroundColor);
      if (c[3] > 0) { layers.push(c); if (c[3] === 255) break; }
    }
    let out = [255, 255, 255];
    for (const c of layers.reverse()) out = out.map((v, i) => v * (1 - c[3] / 255) + c[i] * c[3] / 255);
    return out;
  };
  const failures = [];
  const walker = document.createTreeWalker(document.querySelector('.quoth-panel'), NodeFilter.SHOW_TEXT);
  for (let t = walker.nextNode(); t; t = walker.nextNode()) {
    const el = t.parentElement;
    if (!t.textContent.trim() || !el.getClientRects().length || el.closest('.quoth-sr,[aria-hidden="true"]')) continue;
    const style = getComputedStyle(el);
    if (style.visibility === 'hidden' || Number(style.opacity) === 0) continue;
    const fg = rgba(style.color); const bg = background(el);
    const mixed = fg.slice(0, 3).map((v, i) => v * fg[3] / 255 + bg[i] * (1 - fg[3] / 255));
    const [a, b] = [lum(mixed), lum(bg)].sort((x, y) => y - x);
    const ratio = (a + .05) / (b + .05);
    const size = parseFloat(style.fontSize); const bold = Number(style.fontWeight) >= 700;
    const need = el.closest('button:disabled') ? 0 : size >= 24 || (bold && size >= 18.66) ? 3 : 4.5;
    if (ratio < need) failures.push(`${ratio.toFixed(2)} < ${need} "${t.textContent.trim().slice(0, 40)}" (${el.tagName.toLowerCase()}.${el.className})`);
  }
  return failures;
}

(async () => {
  const browser = await chromium.launch();
  let problems = 0;
  // The panel must follow the WebUI's `.dark`, even against the OS setting; with a bare host
  // (no WebUI variables) it follows the OS. Contrast is checked in every combination.
  const CASES = [
    ['webui dark, OS dark', 'dark', '&theme=dark', 'dark'], ['webui light, OS light', 'light', '&theme=light', 'light'],
    ['webui light, OS dark', 'dark', '&theme=light', 'light'], ['webui dark, OS light', 'light', '&theme=dark', 'dark'],
    ['bare host, OS dark', 'dark', '&host=bare', 'dark'], ['bare host, OS light', 'light', '&host=bare', 'light'],
  ];
  for (const [name, os, query, want] of CASES) {
    const page = await browser.newPage({viewport: {width: 1440, height: 900}, colorScheme: os});
    await page.goto(base + '?scenario=rich' + query); await settle(page);
    const panel = await page.evaluate(() => {
      const c = document.createElement('canvas').getContext('2d'); c.fillStyle = getComputedStyle(document.querySelector('.quoth-panel')).backgroundColor;
      c.fillRect(0, 0, 1, 1); const [r, g, b] = c.getImageData(0, 0, 1, 1).data; return {rgb: [r, g, b].join(','), dark: r + g + b < 384};
    });
    const ok = panel.dark === (want === 'dark');
    if (!ok) problems += 1;
    console.log(`scheme ${name}: panel rgb(${panel.rgb}) ${panel.dark ? 'dark' : 'light'} ${ok ? 'as expected' : 'WRONG, want ' + want}`);
    for (const view of VIEWS) {
      await page.click(`.quoth-nav button[data-view="${view}"]`); await settle(page);
      if (view === 'Review queue') await page.locator('.quoth-bucket summary').first().click();
      const failures = await page.evaluate(contrast);
      problems += failures.length;
      console.log(`contrast ${name} ${view}: ${failures.length ? failures.length + ' below AA' : 'AA'}`);
      for (const f of failures.slice(0, 8)) console.log('   ', f);
    }
    await page.close();
  }
  // Toggling the WebUI theme while the panel is open re-themes it.
  const toggle = await browser.newPage({viewport: {width: 1440, height: 900}, colorScheme: 'light'});
  await toggle.goto(base + '?scenario=rich&theme=light'); await settle(toggle);
  await toggle.evaluate(() => document.documentElement.classList.add('dark'));
  await toggle.waitForTimeout(50);
  const live = await toggle.evaluate(() => document.querySelector('.quoth-overlay').dataset.scheme + '/' + document.querySelector('.quoth-launcher').dataset.scheme);
  if (live !== 'dark/dark') problems += 1;
  console.log('theme toggled to dark while open: overlay/launcher scheme', live);
  await toggle.close();
  // Keyboard only: tab order from the open dialog, arrows in the nav, Enter opens a view and lands on its heading.
  const page = await browser.newPage({viewport: {width: 1440, height: 900}});
  await page.goto(base + '?scenario=rich'); await settle(page);
  const where = () => page.evaluate(() => { const e = document.activeElement; return (e.getAttribute('aria-label') || e.textContent || e.tagName).trim().slice(0, 40) + ' <' + e.tagName.toLowerCase() + '>'; });
  console.log('focus on open:', await where());
  const order = [];
  for (let i = 0; i < 16; i++) { await page.keyboard.press('Tab'); order.push(await where()); }
  console.log('tab order:', order.join(' | '));
  await page.focus('.quoth-nav button[data-view="Overview"]');
  await page.keyboard.press('ArrowDown'); await page.keyboard.press('ArrowDown');
  console.log('after ArrowDown x2:', await where());
  await page.keyboard.press('Enter'); await settle(page);
  console.log('after Enter:', await where());
  const outline = await page.evaluate(() => { const b = document.querySelector('.quoth-nav button'); b.focus(); return getComputedStyle(b).outlineStyle + ' ' + getComputedStyle(b).outlineWidth; });
  console.log('nav focus outline:', outline);
  for (let i = 0; i < 40; i++) await page.keyboard.press('Shift+Tab');
  console.log('focus stays in dialog after 40 Shift+Tab:', await page.evaluate(() => !!document.activeElement.closest('.quoth-overlay')));
  await page.keyboard.press('Escape');
  console.log('Escape closes:', await page.evaluate(() => !document.querySelector('.quoth-overlay')));
  // The accessibility tree of the overview and the review queue.
  await page.evaluate(() => window.__quothPreviewOpen()); await settle(page);
  console.log('--- aria tree: overview\n' + (await page.locator('.quoth-overlay').ariaSnapshot()).split('\n').slice(0, 60).join('\n'));
  await page.click('.quoth-nav button[data-view="Review queue"]'); await settle(page);
  console.log('--- aria tree: review (head)\n' + (await page.locator('.quoth-content').ariaSnapshot()).split('\n').slice(0, 45).join('\n'));
  await page.close();
  // Phone: no round item stays under the sticky reply bar once revealed; the tab strip shows its edges;
  // meta rows stay within two lines.
  const phone = await browser.newPage({viewport: {width: 390, height: 844}, deviceScaleFactor: 2});
  await phone.goto(base + '?scenario=rich'); await settle(phone);
  const edge = () => phone.evaluate(() => document.querySelector('.quoth-rail').dataset.edge);
  console.log('phone tabs on open: edge', await edge());
  await phone.click('.quoth-nav button[data-view="Refresh history"]'); await settle(phone);
  console.log('phone tabs on Refresh history: edge', await edge(), '· active tab fully visible:', await phone.evaluate(() => {
    const r = document.querySelector('.quoth-rail').getBoundingClientRect();
    const b = document.querySelector('.quoth-nav button[aria-current="page"]').getBoundingClientRect();
    return b.left >= r.left && b.right <= r.right;
  }));
  await phone.click('.quoth-nav button[data-view="Review queue"]'); await settle(phone);
  const covered = await phone.evaluate(() => {
    const bar = document.querySelector('.quoth-topic[data-next] .quoth-reply');
    const hits = [];
    for (const li of document.querySelectorAll('.quoth-topic[data-next] .quoth-slip-numbered')) {
      for (const part of [li.querySelector('.quoth-claim-text'), li.querySelector('.quoth-drop')]) {
        part.scrollIntoView({block: 'nearest'});
        const a = part.getBoundingClientRect(), b = bar.getBoundingClientRect();
        if (a.bottom > b.top + 1 && a.top < b.bottom - 1) hits.push(li.querySelector('.quoth-num').textContent + ' ' + part.className);
      }
    }
    return {hits, barHeight: Math.round(bar.getBoundingClientRect().height)};
  });
  console.log('phone reply bar height', covered.barHeight, 'px · items left under it after reveal:', covered.hits.length ? covered.hits.join(', ') : 'none');
  problems += covered.hits.length;
  const metaLines = await phone.evaluate(() => [...document.querySelectorAll('.quoth-topic[data-next] .quoth-slip-meta')].map(m => {
    const tops = new Set([...m.children].filter(c => c.getClientRects().length).map(c => Math.round(c.getBoundingClientRect().top / 8)));
    return tops.size;
  }));
  console.log('phone meta rows, lines per item:', metaLines.join(' '));
  problems += metaLines.filter(n => n > 2).length;
  await phone.close();
  // Reduced motion: the skeleton shimmer stops.
  for (const reducedMotion of ['no-preference', 'reduce']) {
    const p = await browser.newPage({reducedMotion});
    await p.goto(base + '?scenario=slow'); await p.waitForSelector('.quoth-loading span');
    console.log('motion', reducedMotion, await p.evaluate(() => { const s = getComputedStyle(document.querySelector('.quoth-loading span')); return s.animationDuration + ' x' + s.animationIterationCount; }));
    await p.close();
  }
  await browser.close();
  console.log(problems ? `contrast problems: ${problems}` : 'contrast: all AA');
  process.exitCode = problems ? 1 : 0;
})().catch(error => { console.error(error); process.exitCode = 2; });
