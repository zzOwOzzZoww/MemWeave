const fs = require('fs');
const path = require('path');
const {chromium} = require('playwright');

(async () => {
  const state = JSON.parse(fs.readFileSync(path.join(process.env.MEMWEAVE_HOME, 'runtime-state.json'), 'utf8'));
  const browser = await chromium.launch({headless: true, executablePath: process.env.MEMWEAVE_TEST_CHROME});
  const page = await browser.newPage({viewport: {width: 1440, height: 1000}});
  const errors = [];
  page.on('pageerror', error => errors.push(error.message));
  const assert = (condition, message) => {if (!condition) throw new Error(message)};
  try {
    await page.goto(`${state.url}/knowledge#token=${state.token}&project=shared-fixture`);
    await page.locator('#agentRows tr').nth(2).waitFor();
    const claude = page.locator('#agentRows tr').filter({has: page.locator('[data-repair-agent="claude-code"]')});
    assert((await claude.innerText()).includes('已配置'), 'native configuration label missing');
    assert((await claude.innerText()).includes('执行证据待确认'), 'configuration must not imply observed execution');
    assert((await claude.innerText()).includes('shared-fixture'), 'global shared pool missing');
    const custom = page.locator('#agentRows tr').filter({hasText: 'custom-agent'});
    assert((await custom.innerText()).includes('已登记'), 'API adapter must remain registered only');
    assert(await custom.locator('[data-repair-agent]').count() === 0, 'API adapter must not offer native repair');

    const settingsPath = path.join(process.env.CLAUDE_CONFIG_DIR, 'settings.json');
    const settings = JSON.parse(fs.readFileSync(settingsPath, 'utf8'));
    settings.hooks.UserPromptSubmit[0].matcher = 'only-old-directory';
    fs.writeFileSync(settingsPath, JSON.stringify(settings));
    await page.locator('#refresh').click();
    await page.waitForFunction(() => document.getElementById('agentRows').textContent.includes('接入待修复'));
    await claude.locator('[data-repair-agent]').click();
    await page.waitForFunction(() => !document.getElementById('agentRows').textContent.includes('接入待修复'));
    const repaired = JSON.parse(fs.readFileSync(settingsPath, 'utf8'));
    assert(repaired.hooks.UserPromptSubmit.some(group => group.matcher === ''), 'repair did not remove conditional scope');

    const widths = [1440, 1000, 390];
    for (const width of widths) {
      await page.setViewportSize({width, height: 1000});
      await page.locator('#agentsModule').scrollIntoViewIfNeeded();
      assert(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth), `page overflow at ${width}`);
      const bounds = await page.locator('#agentsModule').boundingBox();
      assert(bounds.x >= 0 && bounds.x + bounds.width <= width + 1, `agent section clipped at ${width}`);
      const buttonsFit = await page.locator('[data-repair-agent]').evaluateAll(buttons => buttons.every(button => {
        const box = button.getBoundingClientRect();
        const cell = button.parentElement.getBoundingClientRect();
        return button.scrollWidth <= button.clientWidth && box.x >= cell.x && box.right <= cell.right;
      }));
      assert(buttonsFit, `repair button overflow at ${width}`);
      await page.screenshot({path: path.join(process.env.MEMWEAVE_TEST_OUTPUT, `agents-${width}.png`), fullPage: false});
    }
    assert(errors.length === 0, 'browser errors: ' + errors.join(';'));
    const report = {globalScopeVisible: true,apiAdapterNotMisreported: true,legacyRepairWorks: true,
      checkedWidths: widths,pageErrors: errors};
    fs.writeFileSync(path.join(process.env.MEMWEAVE_TEST_OUTPUT, 'report.json'), JSON.stringify(report, null, 2));
    console.log(JSON.stringify(report));
  } finally {await browser.close()}
})().catch(error => {console.error(error.message);process.exit(1)});
