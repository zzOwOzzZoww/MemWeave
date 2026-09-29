// Read-only UI QA, with browser-local metric fixtures for zero/nonzero states.
const fs = require('node:fs');
const path = require('node:path');
const assert = require('node:assert/strict');
const {chromium} = require('playwright');

async function main() {
  const state = JSON.parse(fs.readFileSync(process.env.MW_QA_STATE_PATH, 'utf8').replace(/^\uFEFF/, ''));
  const out = path.resolve(__dirname, '../outputs/experience-loop-20260924/ui');
  fs.mkdirSync(out, {recursive:true});
  const browser = await chromium.launch({headless:true, executablePath:process.env.MW_QA_BROWSER_PATH});
  const page = await browser.newPage({viewport:{width:1440,height:1100}});
  const errors = [];
  page.on('pageerror', error => errors.push(error.message));
  try {
    await page.goto(state.url+'/knowledge#'+new URLSearchParams({token:state.token,project:state.project_key}),
                    {waitUntil:'networkidle'});
    await page.waitForFunction(() => document.querySelectorAll('#experienceMetrics .evidence-card').length === 4);
    assert.equal(await page.locator('.kbd-shortcut').count(), 0);
    assert.equal(await page.locator('#query').getAttribute('placeholder'), '搜索知识或来源');
    assert.equal(await page.locator('#reuseMetrics .evidence-card').count(), 4);
    const layouts = [];
    for(const width of [1920,1440,1280,1100,1000]) {
      await page.setViewportSize({width,height:1100});
      const layout = await page.evaluate(() => {
        const input=document.getElementById('query'), css=getComputedStyle(input);
        const canvas=document.createElement('canvas'), ctx=canvas.getContext('2d');ctx.font=css.font;
        const available=input.clientWidth-parseFloat(css.paddingLeft)-parseFloat(css.paddingRight);
        const toolbar=document.querySelector('.knowledge-filters');
        const cards=[...document.querySelectorAll('#reuseMetrics .evidence-card')].map(x=>x.getBoundingClientRect());
        const row=cards.filter(r=>Math.abs(r.top-cards[0].top)<1);
        const label=getComputedStyle(document.querySelector('.date-control label'));
        const select=getComputedStyle(document.querySelector('.knowledge-filters .styled-select-trigger'));
        return {
          placeholderFits:ctx.measureText(input.placeholder).width<=available,
          filterOverflow:toolbar.scrollWidth>toolbar.clientWidth+1,
          pageOverflow:document.documentElement.scrollWidth>document.documentElement.clientWidth+1,
          cardsPerRow:row.length,
          uniformCardWidths:Math.max(...cards.map(x=>x.width))-Math.min(...cards.map(x=>x.width))<2,
          fonts:[css.fontFamily,label.fontFamily,select.fontFamily],
          sizes:[css.fontSize,label.fontSize,select.fontSize],
          weights:[css.fontWeight,label.fontWeight,select.fontWeight],
          colors:[getComputedStyle(input,'::placeholder').color,label.color],
          panelOverflow:[...document.querySelectorAll('.reuse-metrics-box')].some(x=>x.scrollWidth>x.clientWidth+1)
        };
      });
      assert.equal(layout.placeholderFits,true,JSON.stringify({width,...layout}));
      assert.equal(layout.filterOverflow,false,JSON.stringify({width,...layout}));
      assert.equal(layout.pageOverflow,false);
      assert.equal(layout.panelOverflow,false);
      assert.equal(layout.uniformCardWidths,true);
      for(const values of [layout.fonts,layout.sizes,layout.weights,layout.colors]) assert.equal(new Set(values).size,1,JSON.stringify(values));
      layouts.push({width,...layout});
      if(width===1440 || width===1000) {
        await page.locator('.reuse-panel').screenshot({path:path.join(out,'evidence-'+width+'.png')});
        await page.locator('.knowledge-filters').screenshot({path:path.join(out,'filters-'+width+'.png')});
      }
    }
    await page.setViewportSize({width:1440,height:1100});
    await page.route('**/v1/metrics',async route=>{
      const response=await route.fetch();const value=await response.json();
      await route.fulfill({json:{...value,experience:{total:10,candidates:2,ready:7,suspended:1,
        cross_agent_passed:12,failure_events:3,recovered:2,passed:14,failed:1,unverified:4}}});
    });
    await page.locator('#refresh').click();
    await page.waitForFunction(()=>document.getElementById('experienceMetrics').textContent.includes('12 条次'));
    assert.equal(await page.locator('#experienceMetrics .attention').count(),1);
    await page.locator('.reuse-panel').screenshot({path:path.join(out,'evidence-fixture.png')});
    await page.locator('#query').fill('widget');
    await page.locator('#refresh').click();
    assert.equal(await page.locator('#query').inputValue(),'');
    await page.locator('#sharing-trigger').click();
    assert.equal(await page.locator('#sharing-menu').isVisible(),true);
    await page.keyboard.press('Escape');
    await page.evaluate(()=>document.documentElement.setAttribute('data-theme','dark'));
    await page.locator('.reuse-panel').screenshot({path:path.join(out,'evidence-dark.png')});
    assert.deepEqual(errors,[]);
    const report={layouts,metricsFixture:true,refreshReset:true,dropdownKeyboard:true,errors};
    fs.writeFileSync(path.join(out,'report.json'),JSON.stringify(report,null,2));
    console.log(JSON.stringify(report,null,2));
  } finally {await browser.close();}
}
main().catch(e=>{console.error(e);process.exitCode=1;});

