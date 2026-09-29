// API fixtures are browser-local; no knowledge records are written.
const fs = require('node:fs');
const path = require('node:path');
const assert = require('node:assert/strict');
const { chromium } = require('playwright');

async function main() {
  const state = JSON.parse(fs.readFileSync(process.env.MW_QA_STATE_PATH, 'utf8').replace(/^\uFEFF/, ''));
  const out = path.resolve(__dirname, '../outputs/time-filters-20260924');
  fs.mkdirSync(out, {recursive: true});
  const browser = await chromium.launch({headless: true, executablePath: process.env.MW_QA_BROWSER_PATH});
  const page = await browser.newPage({viewport: {width: 1440, height: 1000}, timezoneId: 'Asia/Shanghai'});
  const errors = [];
  page.on('pageerror', e => errors.push(e.message));
  try {
    await page.goto(`${state.url}/knowledge#${new URLSearchParams({token: state.token, project: 'claude-codex-mvp'})}`, {waitUntil: 'networkidle'});
    await page.waitForFunction(() => document.getElementById('codexSource').textContent !== '—');
    assert.equal(await page.locator('#sortDirection').count(), 0);
    assert.equal(await page.locator('[data-time-sort]').count(), 2);
    assert.equal(await page.locator('.date-control[data-empty="true"]').count(), 2);
    const inputs = await page.locator('.date-control').evaluateAll(es => es.map(e => {
      const label = e.querySelector('label').getBoundingClientRect(), input = e.querySelector('input').getBoundingClientRect();
      return {inside: label.top >= input.top && label.bottom <= input.bottom && label.left >= input.left};
    }));
    assert.ok(inputs.every(i => i.inside), JSON.stringify(inputs));
    const layouts = [];
    for (const width of [1440, 1280, 1100, 1000]) {
      await page.setViewportSize({width, height: 1000});
      await page.locator('.knowledge-filters').scrollIntoViewIfNeeded();
      const overflow = await page.locator('.knowledge-filters').evaluate(e => e.scrollWidth > e.clientWidth + 1);
      assert.equal(overflow, false, `filter overflow ${width}`);
      layouts.push({width, overflow});
      await page.locator('.knowledge-filters').screenshot({path: path.join(out, `filters-${width}.png`)});
    }
    await page.setViewportSize({width: 1440, height: 1000});
    await page.locator('#pendingModule [data-time-sort]').evaluate(e => e.scrollIntoView({block:'center'}));
    await page.locator('#pendingModule thead').screenshot({path: path.join(out, 'table-sort.png')});

    const rows = [];
    for (const status of ['candidate', 'active']) for (let i = 0; i < 25; i++) rows.push({
      id: `${status}-${i}`, title: `排序测试 ${String(i).padStart(2, '0')}`, content: '', knowledge_type: 'fact',
      status, scope: 'project', project_key: 'claude-codex-mvp', source_agent: 'codex',
      updated_at: new Date(Date.UTC(2026, 8, i + 1, 16, 30)).toISOString(),
      recall_access: {}, evidence_agents: [], sharing_state: status === 'active' ? 'available_shared' : 'candidate'
    });
    await page.route('**/v1/knowledge/overview', async route => {
      const response = await route.fetch();
      const body = await response.json();
      await route.fulfill({json: {...body, results: rows}});
    });
    await page.locator('#refresh').click();
    await page.waitForFunction(() => document.getElementById('count').textContent === '50 条');
    assert.match(await page.locator('#pendingRows .knowledge-title').first().textContent(), /24$/);
    await page.locator('#pendingNext').click();
    assert.match(await page.locator('#pendingPageSummary').textContent(), /第 2/);
    await page.locator('#pendingModule [data-sort="asc"]').click();
    assert.match(await page.locator('#pendingPageSummary').textContent(), /第 1/);
    assert.match(await page.locator('#pendingRows .knowledge-title').first().textContent(), /00$/);
    assert.equal(await page.locator('[aria-sort="ascending"]').count(), 2);
    await page.locator('#adoptedModule summary').click();
    await page.locator('#adoptedModule [data-sort="desc"]').click();
    assert.equal(await page.locator('[aria-sort="descending"]').count(), 2);
    assert.match(await page.locator('#adoptedRows .knowledge-title').first().textContent(), /24$/);
    await page.locator('#adoptedModule .time-sort-label').focus();
    await page.keyboard.press('Enter');
    assert.equal(await page.locator('[aria-sort="ascending"]').count(), 2);
    await page.locator('#updatedFrom').fill('2026-09-02');
    await page.locator('#updatedTo').fill('2026-09-02');
    // The Sep 1 UTC timestamp is Sep 2 in the browser, matching the displayed date.
    assert.equal(await page.locator('#count').textContent(), '2 条');
    assert.match(await page.locator('#pendingRows .knowledge-title').first().textContent(), /00$/);
    assert.equal(await page.locator('.date-control[data-empty="false"]').count(), 2);
    assert.equal(await page.locator('.date-control label').first().isVisible(), false);
    await page.locator('.knowledge-filters').screenshot({path: path.join(out, 'dates-selected.png')});
    await page.locator('#refresh').click();
    await page.waitForFunction(() => document.getElementById('count').textContent === '50 条');
    assert.equal(await page.locator('[aria-sort="descending"]').count(), 2);
    assert.equal(await page.locator('.date-control[data-empty="true"]').count(), 2);
    assert.deepEqual(errors, []);
    console.log(JSON.stringify({layouts, datePlaceholdersInside: inputs, sortingAndPagination: true,
      localDateInclusiveFilter: true, keyboard: true, reset: true, errors, output: out}, null, 2));
  } finally { await browser.close(); }
}
main().catch(e => {console.error(e); process.exitCode = 1;});
