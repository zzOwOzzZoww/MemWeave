// Read-only browser QA for the Agent overview. Synthetic cases intercept GET/POST
// responses in this browser only; they never alter the local knowledge database.
const fs = require('node:fs');
const path = require('node:path');
const assert = require('node:assert/strict');
const { chromium } = require('playwright');

async function main() {
  const runtime = JSON.parse(fs.readFileSync(process.env.MW_QA_STATE_PATH, 'utf8').replace(/^\uFEFF/, ''));
  const output = path.resolve(__dirname, '../outputs/agent-overview');
  fs.mkdirSync(output, { recursive: true });
  const browser = await chromium.launch({ headless: true, executablePath: process.env.MW_QA_BROWSER_PATH });
  const page = await browser.newPage({ viewport: { width: 1440, height: 1080 } });
  await page.clock.install();
  const errors = [];
  page.on('pageerror', error => errors.push(error.message));
  let overview, metrics, agents;
  page.on('response', async response => {
    if (response.url().endsWith('/v1/knowledge/overview')) overview = await response.json();
    if (response.url().endsWith('/v1/metrics')) metrics = await response.json();
    if (response.url().endsWith('/v1/agents?include_disabled=true')) agents = await response.json();
  });
  const url = `${runtime.url}/knowledge#${new URLSearchParams({ token: runtime.token, project: 'claude-codex-mvp' })}`;
  const waitLoaded = () => page.waitForFunction(() => document.getElementById('codexSource').textContent !== '—');
  const values = () => page.locator('.relation-value').evaluateAll(items => Object.fromEntries(items.map(item => [item.id, item.textContent])));
  const formatted = value => value.toLocaleString('zh-CN');
  try {
    await page.goto(url, { waitUntil: 'networkidle' });
    await waitLoaded();
    const actual = await values();
    const summary = overview.summary;
    assert.deepEqual(actual, {
      codexSource: formatted(summary.source_counts.codex || 0),
      codexAvailable: formatted(summary.retrievable_counts.codex),
      claudeSource: formatted(summary.source_counts['claude-code'] || 0),
      claudeAvailable: formatted(summary.retrievable_counts['claude-code']),
      connectedAgents: formatted(new Set(agents.results.filter(a => a.enabled).map(a => a.agent_id)).size),
      recallRequests: formatted(metrics.reuse.traces),
      crossAgentEmittedTurns: formatted(metrics.reuse.cross_agent_emitted_turns),
      crossAgentEvidenceTurns: formatted(metrics.reuse.cross_agent_evidence_turns),
    });
    assert.equal(await page.locator('.relation-group').count(), 2);
    for (const group of await page.locator('.relation-group').all()) assert.equal(await group.locator('.relation-item').count(), 4);
    await page.evaluate(() => document.documentElement.setAttribute('data-theme', 'light'));
    const colors = await page.locator('.relation-group').evaluateAll(groups => groups.map(g => getComputedStyle(g).backgroundColor));
    assert.notEqual(colors[0], colors[1]);
    const layouts = [];
    for (const width of [1440, 1280, 1100, 1000]) {
      await page.setViewportSize({ width, height: 1080 });
      await page.locator('.relation-strip').scrollIntoViewIfNeeded();
      const layout = await page.locator('.relation-strip').evaluate(section => {
        const groups = [...section.querySelectorAll('.relation-group')].map(g => g.getBoundingClientRect());
        const overflow = [...section.querySelectorAll('*')].filter(e => e.scrollWidth > e.clientWidth + 1);
        const titleWrapped = [...section.querySelectorAll('.relation-title')].some(e => e.clientHeight > parseFloat(getComputedStyle(e).lineHeight) + 1);
        return { sideBySide: Math.abs(groups[0].top - groups[1].top) < 1, overflow: overflow.length, titleWrapped };
      });
      assert.equal(layout.overflow, 0, JSON.stringify({ width, ...layout }));
      assert.equal(layout.sideBySide, true);
      assert.equal(layout.titleWrapped, false);
      layouts.push({ width, ...layout });
      if (width === 1440 || width === 1100) await page.locator('.relation-strip').screenshot({ path: path.join(output, `overview-${width}.png`) });
    }
    await page.setViewportSize({ width: 1440, height: 1080 });
    await page.evaluate(() => document.documentElement.setAttribute('data-theme', 'dark'));
    await page.locator('.relation-strip').screenshot({ path: path.join(output, 'overview-dark.png') });
    await page.evaluate(() => document.documentElement.setAttribute('data-theme', 'light'));
    await page.screenshot({ path: path.join(output, 'overview-in-page.png'), fullPage: true });
    assert.equal(await page.locator('#error').isVisible(), false);

    const baseOverview = overview;
    const baseMetrics = metrics;
    let mode = 'distinct';
    let learningStatus = 'running';
    let hookConfigured = true;
    let hookExecuted = true;
    let agentsFailed = false;
    let agentRequests = 0;
    await page.route('**/v1/agents?include_disabled=true', route => {
      agentRequests++;
      if (agentsFailed) return route.fulfill({status: 503, json: {detail: 'QA connection failure'}});
      return route.fulfill({ json: { results: [
      { agent_id: 'codex', display_name: 'Codex', enabled: true, installed: true,
        adapter_type: 'codex-hook', hook: {configured: hookConfigured, feature_enabled: true, execution: {observed: hookExecuted}},
        learning: {status: learningStatus, proposal_count: 3, promoted_count: 0} },
      { agent_id: 'claude-code', display_name: 'Claude Code', enabled: true, installed: true,
        adapter_type: 'claude-hook', hook: {configured: true},
        learning: {status: 'completed', proposal_count: 2, promoted_count: 0} },
      { agent_id: 'inactive', display_name: 'Inactive', enabled: false },
    ] } }); });
    await page.route('**/v1/knowledge/overview', route => route.fulfill({ json: {
      ...baseOverview, results: [], summary: {
        total: 0, status_counts: {},
        source_counts: mode === 'distinct' ? { codex: 12034, 'claude-code': 27 } : {},
        retrievable_counts: mode === 'distinct' ? { codex: 101, 'claude-code': 202 } : mode === 'missing' ? {} : { codex: 0, 'claude-code': 0 },
      },
    } }));
    await page.route('**/v1/metrics', route => route.fulfill({ json: { ...baseMetrics, reuse: {
      ...baseMetrics.reuse, traces: mode === 'distinct' ? 12 : 0,
      cited_items: mode === 'distinct' ? 3 : 0,
      source_attributed_turns: mode === 'distinct' ? 2 : 0,
      cross_agent_emitted_turns: mode === 'missing' ? null : mode === 'distinct' ? 9 : 0,
      cross_agent_evidence_turns: mode === 'missing' ? null : mode === 'distinct' ? 2 : 0,
      cross_agent_cited_turns: mode === 'distinct' ? 1 : 0,
      cross_agent_source_attributed_turns: mode === 'distinct' ? 2 : 0,
      checked_items: 0,
      cross_agent_checked_items: 0,
      constraint_pass_items: 0,
      cross_agent_constraint_pass_items: mode === 'missing' ? null : 0,
    } } }));
    await page.reload({ waitUntil: 'networkidle' });
    await waitLoaded();
    assert.deepEqual(await values(), { codexSource: '12,034', codexAvailable: '101', claudeSource: '27', claudeAvailable: '202', connectedAgents: '2', recallRequests: '12', crossAgentEmittedTurns: '9', crossAgentEvidenceTurns: '2' });
    const codex = page.locator('#agentRows tr').filter({has: page.locator('[data-disable-agent="codex"]')});
    const claude = page.locator('#agentRows tr').filter({has: page.locator('[data-disable-agent="claude-code"]')});
    assert.equal(await codex.locator('.badge.active').textContent(), '已接入');
    assert.equal(await claude.locator('.badge.active').textContent(), '已接入');
    assert.equal(await codex.locator('.badge').evaluate(e => getComputedStyle(e).color), await claude.locator('.badge').evaluate(e => getComputedStyle(e).color));
    assert.match(await codex.textContent(), /正在学习/);
    assert.match(await claude.textContent(), /生成 2 条候选 · 待审核/);
    assert.match(await page.locator('#reuseMetrics').textContent(), /暂无校验样本/);
    assert.equal(await page.locator('#agentRows .badge.none').textContent(), '已停用');

    learningStatus = 'completed';
    const beforeRefresh = agentRequests;
    await page.locator('#query').fill('temporary-filter');
    await page.locator('#refresh').click();
    await page.waitForFunction(() => document.querySelector('#agentRows').textContent.includes('生成 3 条候选'));
    assert.ok(agentRequests > beforeRefresh);
    assert.equal(await page.locator('#query').inputValue(), '');
    assert.equal(await codex.locator('.badge.active').textContent(), '已接入');

    // Polls update Agent state without clearing the user's knowledge filters.
    learningStatus = 'failed';
    await page.locator('#query').fill('keep-my-filter');
    await page.clock.fastForward(15000);
    await page.waitForFunction(() => document.querySelector('#agentRows').textContent.includes('最近学习失败'));
    assert.equal(await page.locator('#query').inputValue(), 'keep-my-filter');
    assert.equal(await codex.locator('.badge.active').textContent(), '已接入');
    hookConfigured = false;
    await page.clock.fastForward(15000);
    await page.waitForFunction(() => document.querySelector('#agentRows').textContent.includes('接入待修复'));
    assert.equal(await codex.locator('.badge.candidate').textContent(), '接入待修复');
    hookConfigured = true;
    hookExecuted = false;
    await page.clock.fastForward(15000);
    await page.waitForFunction(() => !document.querySelector('#agentRows').textContent.includes('接入待修复'));
    assert.equal(await codex.locator('.badge.active').textContent(), '已接入');
    assert.match(await codex.textContent(), /执行证据待确认/);
    const beforeHidden = agentRequests;
    await page.evaluate(() => Object.defineProperty(document, 'hidden', {configurable: true, get: () => true}));
    await page.clock.fastForward(30000);
    assert.equal(agentRequests, beforeHidden);
    await page.evaluate(() => delete document.hidden);
    agentsFailed = true;
    await page.locator('#refresh').click();
    await page.waitForFunction(() => document.getElementById('error').textContent.includes('部分数据刷新失败'));
    agentsFailed = false;
    for (const next of ['zero', 'missing']) {
      mode = next;
      await page.reload({ waitUntil: 'networkidle' });
      await waitLoaded();
      const v = await values();
      assert.equal(v.codexSource, '0');
      assert.equal(v.claudeSource, '0');
      assert.equal(v.recallRequests, '0');
      assert.equal(v.codexAvailable, mode === 'missing' ? '—' : '0');
      assert.equal(v.crossAgentEmittedTurns, mode === 'missing' ? '—' : '0');
      assert.equal(v.crossAgentEvidenceTurns, mode === 'missing' ? '—' : '0');
    }
    assert.deepEqual(errors, []);
    console.log(JSON.stringify({ liveValues: actual, layouts, dataCases: ['live API values', 'distinct per-agent counts', 'disabled agent excluded', 'zero preserved', 'missing shown as dash', 'consistent green badges while running or failed', 'configuration warnings retained', 'manual refresh updates agents and clears filters', 'polling preserves filters and stops on hidden pages', 'refresh failure reported'], errors, screenshots: output }, null, 2));
  } finally {
    await browser.close();
  }
}

main().catch(error => { console.error(error); process.exitCode = 1; });
