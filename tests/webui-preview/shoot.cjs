/* Screenshots of the preview page (synthetic data) at three widths, light and dark.
   Needs Playwright with a Chromium build:  NODE_PATH=<dir with playwright> node tests/webui-preview/shoot.cjs <out-dir> [base-url]
   Serve the repo root first, e.g. python3 -m http.server 8765. */
const {chromium} = require('playwright');
const fs = require('node:fs');
const path = require('node:path');

const out = process.argv[2] || '.scratch/webui-shots/latest';
const base = (process.argv[3] || 'http://127.0.0.1:8765') + '/tests/webui-preview/index.html';
const only = process.env.ONLY ? process.env.ONLY.split(',') : null;
const SIZES = [['desktop', 1440, 900], ['laptop', 1024, 768], ['phone', 390, 844]];

async function settle(page) {
  await page.waitForSelector('.quoth-content .quoth-view > *');
  await page.waitForFunction(() => !document.querySelector('.quoth-content[aria-busy]'));
  await page.waitForTimeout(150);
}
async function goView(page, view) {
  await page.click(`.quoth-nav button[data-view="${view}"]`);
  await settle(page);
}
const STEPS = {
  overview: async page => goView(page, 'Overview'),
  review: async page => goView(page, 'Review queue'),
  'review-topics': async page => {
    await goView(page, 'Review queue');
    await page.locator('.quoth-topic').first().scrollIntoViewIfNeeded();
    const boxes = page.locator('.quoth-topic').first().locator('.quoth-drop input');
    if (await boxes.count() > 3) { await boxes.nth(2).check(); await boxes.nth(4).check(); }
    await page.locator('.quoth-topic').first().evaluate(el => el.scrollIntoView({block: 'start'}));
  },
  'review-round-end': async page => {
    await goView(page, 'Review queue');
    const boxes = page.locator('.quoth-topic[data-next] .quoth-drop input');
    if (await boxes.count() > 4) { await boxes.nth(2).check(); await boxes.nth(4).check(); }
    await page.locator('.quoth-topic[data-next] .quoth-later, .quoth-topic[data-next]').first().evaluate(el => el.scrollIntoView({block: 'end'}));
  },
  'review-cleanup': async page => {
    await goView(page, 'Review queue');
    await page.locator('.quoth-bucket summary').first().click();
    await page.locator('.quoth-bucket').first().evaluate(el => el.scrollIntoView({block: 'start'}));
  },
  'review-suggestion': async page => {
    await goView(page, 'Review queue');
    await page.click('.quoth-segmented button:nth-child(2)'); await settle(page);
  },
  memories: async page => goView(page, 'Memories'),
  detail: async page => {
    await goView(page, 'Memories');
    await page.fill('.quoth-search input', 'blue shed'); await page.keyboard.press('Enter'); await settle(page);
    await page.locator('.quoth-catalogue tbody tr').last().locator('.quoth-link').click(); await settle(page);
  },
  'detail-pending': async page => {
    await goView(page, 'Review queue');
    await page.locator('.quoth-bucket summary').first().click();
    await page.locator('.quoth-bucket .quoth-slip-meta .quoth-link').first().click(); await settle(page);
  },
  core: async page => goView(page, 'Core'),
  recall: async page => {
    await goView(page, 'Recall');
    await page.fill('textarea[data-private]', 'Where did I leave the kayak paddles?');
    await page.click('text=Show what the assistant would recall');
    await page.waitForSelector('.quoth-recall'); await page.waitForTimeout(100);
  },
  said: async page => {
    await goView(page, 'What the assistant said');
    await page.fill('input[data-private]', 'paddles');
    await page.click('.quoth-ask .quoth-primary');
    await page.waitForSelector('.quoth-said'); await page.waitForTimeout(100);
  },
  night: async page => goView(page, 'Refresh history'),
};
const PLAN = [
  ...Object.keys(STEPS).map(step => ['rich', step]),
  ['empty', 'overview'], ['empty', 'review'], ['empty', 'memories'], ['empty', 'night'],
  ['error', 'overview'], ['error', 'review'], ['error', 'memories'], ['slow', 'loading'],
];

(async () => {
  const browser = await chromium.launch();
  fs.mkdirSync(out, {recursive: true});
  for (const scheme of ['light', 'dark']) {
    for (const [size, width, height] of SIZES) {
      const context = await browser.newContext({viewport: {width, height}, colorScheme: scheme, deviceScaleFactor: size === 'phone' ? 2 : 1,
        reducedMotion: 'reduce'});
      for (const [scenario, step] of PLAN) {
        const name = `${size}-${scheme}-${scenario === 'rich' ? '' : scenario + '-'}${step}`;
        if (only && !only.some(o => name.includes(o))) continue;
        if (scenario !== 'rich' && size === 'laptop') continue;
        const page = await context.newPage();
        const errors = [];
        page.on('pageerror', e => errors.push(String(e)));
        await page.goto(`${base}?scenario=${scenario}`);
        if (step === 'loading') { await page.waitForTimeout(600); }
        else { await settle(page); if (STEPS[step]) await STEPS[step](page); }
        await page.screenshot({path: path.join(out, name + '.png')});
        if (errors.length) console.log('ERRORS', name, errors);
        await page.close();
      }
      await context.close();
    }
  }
  // The WebUI's theme against the OS setting: the panel follows the WebUI.
  for (const [size, width, height] of SIZES.filter(([s]) => s !== 'laptop')) {
    for (const [os, theme] of [['dark', 'light'], ['light', 'dark']]) {
      const name = `${size}-webui-${theme}-os-${os}-overview`;
      if (only && !only.some(o => name.includes(o))) continue;
      const page = await browser.newPage({viewport: {width, height}, colorScheme: os, deviceScaleFactor: size === 'phone' ? 2 : 1, reducedMotion: 'reduce'});
      await page.goto(`${base}?scenario=rich&theme=${theme}`); await settle(page);
      await page.screenshot({path: path.join(out, name + '.png')});
      await page.close();
    }
  }
  await browser.close();
  console.log('shots in', out);
})().catch(error => { console.error(error); process.exitCode = 1; });
