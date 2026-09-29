// Browser actions run only against the disposable runtime created by the runner.
const {chromium} = require('playwright');
const fs = require('fs');
const path = require('path');
(async () => {
  const browser = await chromium.launch({headless:true,executablePath:'C:/Program Files/Google/Chrome/Application/chrome.exe'});
  const page = await browser.newPage({viewport:{width:1440,height:1080}});
  const errors=[];
  page.on('pageerror',error=>errors.push(error.message));
  const assert=(condition,message)=>{if(!condition)throw new Error(message)};
  try {
    await page.goto(process.env.MW_QA_URL+'/knowledge#token='+process.env.MW_QA_TOKEN+'&project=ui-test');
    await page.waitForFunction(()=>document.querySelector('#pendingRows tr')?.innerText.includes('审核期限新版'));
    await page.locator('#pendingRows tr').filter({hasText:'审核期限新版'}).locator('input.knowledge-check').check();
    page.once('dialog',d=>d.accept());
    await page.locator('#batchApprove').click();
    await page.waitForFunction(()=>document.querySelector('#error').textContent.includes('失败 1 条'));
    assert(await page.locator('#pendingRows tr').filter({hasText:'审核期限新版'}).locator('input.knowledge-check').isChecked(),
      'failed bulk approval preserves selection');
    await page.locator('#pendingRows tr').filter({hasText:'审核期限新版'}).locator('input.knowledge-check').uncheck();
    await page.locator('#pendingRows tr').filter({hasText:'审核期限新版'}).click();
    await page.waitForFunction(()=>document.querySelector('#detail').open);
    assert(await page.locator('#approve').isDisabled(),'implicit approval must be disabled');
    assert(await page.locator('#replaceVersion').isVisible(),'replacement action must be visible');
    assert((await page.locator('#detailVersions').innerText()).includes('24小时'),'old value must be reviewable');
    assert((await page.locator('#detailVersions').innerText()).includes('Claude Code'),'source must be readable');
    await page.screenshot({path:path.join(process.env.MW_QA_OUTPUT,'replacement-review.png')});
    page.once('dialog',d=>d.dismiss());
    await page.locator('#replaceVersion').click();
    assert(await page.locator('#replaceVersion').isEnabled(),'cancel leaves action available');
    page.once('dialog',d=>d.accept());
    const responsePromise=page.waitForResponse(r=>r.url().endsWith('/v1/knowledge/feedback'));
    await page.locator('#replaceVersion').click();
    assert((await responsePromise).ok(),'replacement API must succeed');
    await page.waitForFunction(()=>!document.querySelector('#detail').open);
    if(!await page.locator('#adoptedModule').evaluate(e=>e.open)) await page.locator('#adoptedModule summary').click();
    await page.waitForFunction(()=>document.querySelector('#adoptedRows').textContent.includes('审核期限新版'));
    await page.locator('#adoptedRows tr').filter({hasText:'审核期限新版'}).click();
    await page.waitForFunction(()=>document.querySelector('#detail').open);
    assert(await page.locator('#detail .dialog-body').evaluate(e=>e.scrollTop===0),'new detail must start at top');
    assert((await page.locator('#detailVersions').innerText()).includes('已完成版本替代'),'success state must be visible');
    assert(await page.locator('#approve').isDisabled(),'accepted version remains read-only');
    assert(!await page.locator('#replaceVersion').isVisible(),'resolved version must not offer replacement');
    await page.screenshot({path:path.join(process.env.MW_QA_OUTPUT,'replacement-complete.png')});
    await page.locator('#close').click();
    await page.locator('#adoptedRows tr').filter({hasText:'审核期限旧版'}).click();
    await page.waitForFunction(()=>document.querySelector('#detail').open);
    assert((await page.locator('#detailVersions').innerText()).includes('历史版本'),'old record is labelled historical');
    assert(await page.locator('#approve').isDisabled(),'superseded version cannot be approved');
    assert(!await page.locator('#replaceVersion').isVisible(),'superseded version cannot be a replacement');
    assert(await page.locator('#reviewReason').isDisabled(),'superseded review is read-only');
    assert(errors.length===0,errors.join('\n'));
    fs.writeFileSync(path.join(process.env.MW_QA_OUTPUT,'browser-report.json'),JSON.stringify({passed:true,errors,checks:['bulk conflict retention','review conflict','cancel','explicit replacement','detail scroll reset','resolved read-only','superseded read-only']},null,2));
    console.log('Version review browser checks passed');
  } catch(error) {
    await page.screenshot({path:path.join(process.env.MW_QA_OUTPUT,'failure.png')});
    console.error(JSON.stringify(await page.evaluate(()=>({error:document.getElementById('error').textContent,adopted:document.getElementById('adoptedRows').textContent})),null,2));
    throw error;
  } finally { await browser.close(); }
})().catch(error=>{console.error(error);process.exitCode=1});
