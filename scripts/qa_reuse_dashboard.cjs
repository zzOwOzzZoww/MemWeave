const { chromium } = require('playwright');

(async () => {
  const browser = await chromium.launch({headless: true, executablePath: process.env.MW_QA_BROWSER_PATH});
  const page = await browser.newPage({viewport: {width: 1440, height: 1080}});
  const errors = [];
  page.on('pageerror', e => errors.push(e.message));
  page.on('console', e => { if (e.type() === 'error') errors.push(e.text()); });
  try {
    await page.goto(`${process.env.MW_QA_URL}/knowledge#token=${process.env.MW_QA_TOKEN}&project=memweave-evaluation`, {waitUntil:'networkidle'});
    await page.waitForFunction(() => document.querySelector('#reuseMetrics').textContent.includes('跨 Agent 约束通过 2'));
    await page.locator('details').evaluate(e => e.open = true);
    const traceText = await page.locator('#reuseTraces').innerText();
    if (!traceText.includes('约束验证通过')) throw new Error('Missing constraint evidence');
    await page.screenshot({path: process.env.MW_QA_SCREENSHOT_PATH, fullPage:true});
    await page.locator('#query').fill('demoalpha');
    await page.locator('#rows tr').first().click();
    await page.waitForFunction(() => document.querySelector('#detail').open);
    const evidence = await page.locator('#detailReuse').innerText();
    if (!evidence.includes('约束验证通过') || !evidence.includes('SHA256')) throw new Error('Missing per-item proof');
    await page.screenshot({path: process.env.MW_QA_DETAIL_PATH});
    const overflow = await page.evaluate(() => document.documentElement.scrollWidth > document.documentElement.clientWidth);
    if (overflow || errors.length) throw new Error(JSON.stringify({overflow, errors}));
    console.log(JSON.stringify({desktop:true, overview:true, perKnowledgeTrace:true, errors}));
  } finally {
    await browser.close();
  }
})().catch(e => { console.error(e.message); process.exit(1); });
