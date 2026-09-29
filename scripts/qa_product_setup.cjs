const fs = require('fs');
const {chromium} = require('playwright');
(async () => {
  const state = JSON.parse(fs.readFileSync(process.env.MEMWEAVE_HOME + '/runtime-state.json', 'utf8'));
  const browser = await chromium.launch({headless: true, executablePath: process.env.MEMWEAVE_TEST_CHROME});
  const page = await browser.newPage({viewport:{width:1440,height:1000}});
  const errors=[];
  page.on('pageerror', e=>errors.push(e.message));
  const assert=(x,message)=>{if(!x)throw new Error(message)};
  try {
    await page.goto(state.url+'/knowledge#token='+state.token+'&project=default');
    await page.waitForFunction(()=>document.getElementById('refresh')&&!document.getElementById('refresh').classList.contains('spinning'));
    await page.locator('#providerSettings').click();
    await page.locator('#providerDialog').waitFor({state:'visible'});
    assert(await page.locator('#providerModel').inputValue()==='fixture-model','settings load');
    assert(await page.locator('#providerKey').inputValue()==='','key must never return from API');
    assert((await page.locator('#settingsProject-trigger').innerText()).trim().length>0,'workspace label missing');
    await page.locator('#settingsProject-trigger').click();
    await page.locator('#settingsProject-menu [role=option]').first().click();
    await page.locator('#providerModel').fill('fixture-model-updated');
    await page.locator('#providerForm button[type=submit]').click();
    await page.waitForFunction(()=>document.getElementById('providerMessage').textContent.includes('保存成功'));
    await page.screenshot({path:process.env.MEMWEAVE_TEST_OUTPUT+'/settings-1440.png'});
    await page.locator('#providerClose').click();
    await page.locator('#providerSettings').click();
    await page.locator('#providerDialog').waitFor({state:'visible'});
    assert(await page.locator('#providerModel').inputValue()==='fixture-model-updated','settings persist');
    const sizes=[];
    for (const width of [1440,1100,1000]) {
      await page.setViewportSize({width,height:1000});
      assert(await page.evaluate(()=>document.documentElement.scrollWidth<=innerWidth),'page overflow');
      const bounds=await page.locator('#providerDialog').boundingBox();
      assert(bounds.x>=0&&bounds.x+bounds.width<=width&&bounds.y>=0&&bounds.y+bounds.height<=1000,'dialog clipping');
      sizes.push(width);
    }
    await page.locator('#providerClose').click();
    assert(await page.locator('main > details.dashboard-module').count()===5,'existing modules changed');
    assert(errors.length===0,'page errors: '+errors.join(';'));
    const report={modelLoaded:true,keyNeverPrefilled:true,configurationSaved:true,existingModules:5,
      checkedWidths:sizes,pageErrors:errors};
    fs.writeFileSync(process.env.MEMWEAVE_TEST_OUTPUT+'/settings-ui-report.json',JSON.stringify(report,null,2));
    console.log(JSON.stringify(report));
  } finally {await browser.close()}
})().catch(error=>{console.error(error.message);process.exit(1)});
