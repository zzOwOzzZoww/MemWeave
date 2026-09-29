const {chromium} = require('playwright');
const fs = require('fs');
const path = require('path');

(async () => {
  const browser = await chromium.launch({headless:true,
    executablePath:'C:/Program Files/Google/Chrome/Application/chrome.exe'});
  const page = await browser.newPage({viewport:{width:1440,height:1080}});
  const errors=[];
  page.on('pageerror', e=>errors.push(e.message));
  const out=process.env.MW_QA_OUTPUT;
  const assert=(value,message)=>{if(!value)throw new Error(message)};
  try {
    await page.goto(process.env.MW_QA_URL+'/knowledge#token='+process.env.MW_QA_TOKEN+'&project=ui-test');
    await page.waitForFunction(()=>document.querySelectorAll('#adoptedRows tr').length===20);
    await page.locator('#adoptedModule summary').click();
    const box=await page.locator('#selectAllAdopted').boundingBox();
    assert(box.width<=20 && box.height<=20,'bulk checkbox must share compact styling');
    await page.locator('#adoptedRows tr').first().click();
    await page.waitForFunction(()=>document.querySelector('#detail').open);
    assert(await page.locator('#approve').isDisabled(),'Active approve must be disabled');
    assert(await page.locator('#approve').isVisible(),'Active status should remain visible');
    assert(await page.locator('#reviewReason').isDisabled(),'Active review reason must be disabled');
    assert(!await page.locator('#reject').isVisible(),'Active review action should be hidden');
    await page.screenshot({path:path.join(out,'active-disabled.png')});
    await page.locator('#close').click();
    assert((await page.locator('#turnTimingMetrics').innerText()).includes('暂无对照样本'),'no paired samples is not zero');
    await page.locator('#selectAllAdopted').check();
    assert((await page.locator('#selectedAdoptedCount').innerText()).includes('20'),'select page count');
    await page.locator('#adoptedNext').click();
    assert(await page.locator('#batchRemove').isDisabled(),'selection must not silently carry over pages');
    await page.locator('#selectAllAdopted').check();
    assert((await page.locator('#selectedAdoptedCount').innerText()).includes('5'),'last page count');
    page.once('dialog',d=>d.dismiss());
    await page.locator('#batchRemove').click();
    assert((await page.locator('#adoptedCount').innerText()).includes('25'),'cancel must not delete');
    // Failure leaves selected rows intact and makes retry available.
    await page.route('**/v1/knowledge/remove',r=>r.fulfill({status:503,contentType:'application/json',body:'{"detail":"QA unavailable"}'}));
    page.once('dialog',d=>d.accept());
    await page.locator('#batchRemove').click();
    await page.waitForFunction(()=>!document.querySelector('#batchRemove').disabled);
    assert((await page.locator('#adoptedCount').innerText()).includes('25'),'failed request must not remove rows');
    await page.unroute('**/v1/knowledge/remove');
    await page.screenshot({path:path.join(out,'bulk-remove.png')});
    page.once('dialog',d=>d.accept());
    await page.locator('#batchRemove').click();
    await page.waitForFunction(()=>document.querySelector('#adoptedCount').textContent.trim()==='20 条');
    assert(await page.locator('#batchRemove').isDisabled(),'successful removal clears selection');
    assert((await page.locator('#adoptedPageSummary').innerText()).includes('1 / 1'),'page clamp after delete');
    // Existing pending workflow still works.
    await page.locator('#pendingRows input.knowledge-check').first().check();
    page.once('dialog',d=>d.accept());
    await page.locator('#batchApprove').click();
    await page.waitForFunction(()=>document.querySelector('#adoptedCount').textContent.trim()==='21 条');
    const pair=JSON.parse(fs.readFileSync(process.env.MW_QA_PAIR,'utf8'));
    await page.evaluate(async payload=>{const response=await fetch('/v1/metrics/latency-pairs',{
      method:'POST',headers:{'Authorization':'Bearer '+new URLSearchParams(location.hash.slice(1)).get('token'),
      'Content-Type':'application/json'},body:JSON.stringify(payload)});if(!response.ok)throw new Error('pair import failed')},pair);
    await page.locator('#refresh').click();
    await page.waitForFunction(()=>document.querySelector('#turnTimingMetrics').textContent.includes('+500 ms'));
    assert((await page.locator('#timingImpactNote').innerText()).includes('样本偏少'),'small sample warning');
    await page.locator('#turnTimingMetrics').scrollIntoViewIfNeeded();
    await page.screenshot({path:path.join(out,'paired-impact.png')});
    for(const width of [1440,1100,1000]) {
      await page.setViewportSize({width,height:1080});
      assert(await page.evaluate(()=>document.documentElement.scrollWidth<=document.documentElement.clientWidth),`overflow at ${width}`);
      await page.locator('#adoptedModule').evaluate(e=>window.scrollTo(0,e.getBoundingClientRect().top+window.scrollY-95));
      await page.screenshot({path:path.join(out,`layout-${width}.png`)});
    }
    assert(errors.length===0,JSON.stringify(errors));
    const report={isolated:true,activeDisabled:true,reviewReadonly:true,
      selectPageOnly:true,cancelSafe:true,errorRetry:true,permanentRemoval:true,
      paginationClamp:true,pendingApproval:true,emptyImpact:true,pairedImpact:true,
      widths:[1440,1100,1000],errors};
    fs.writeFileSync(path.join(out,'ui-report.json'),JSON.stringify(report,null,2));
    console.log(JSON.stringify(report));
  } finally {await browser.close()}
})().catch(error=>{console.error(error.message);process.exit(1)});
