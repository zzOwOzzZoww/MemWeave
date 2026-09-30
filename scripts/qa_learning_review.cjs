const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const {execFileSync} = require('node:child_process');
const {chromium} = require('playwright');

async function main() {
  const fixture = JSON.parse(process.env.MW_QA_FIXTURE);
  const browser = await chromium.launch({headless:true, executablePath:process.env.MW_QA_BROWSER_PATH});
  const context = await browser.newContext({viewport:{width:1440,height:1000}});
  const page = await context.newPage();
  await page.route('**/v1/agents/discover', route => route.fulfill({status:200, contentType:'application/json', body:JSON.stringify({results:[{
    agent_id:'workbuddy', display_name:'WorkBuddy', adapter_type:'runtime-api', installed:true,
    detected_by:['config'], capabilities:['shared-knowledge']
  }]})}));
  const errors = [];
  page.on('pageerror', error => errors.push(error.message));
  page.on('dialog', dialog => dialog.accept());
  const url = `${process.env.MW_QA_URL}/knowledge#token=${process.env.MW_QA_TOKEN}&project=ui-home`;
  const output = file => path.join(process.env.MW_QA_OUTPUT, file);
  const dialog = page.locator('#learningDialog');
  const row = agent => page.locator('#agentRows tr').filter({has:page.locator(`[data-learning-agent="${agent}"]`)});
  const entry = key => dialog.locator(`[data-learning-id="${fixture.keys[key]}"]`);
  const ready = () => page.waitForFunction(() => document.querySelector('#learningRecords').getAttribute('aria-busy') === 'false' && !!state.learningReview?.data);
  const pending = async count => {
    await page.waitForFunction(expected => state.learningReview?.data?.pending_count === expected && !state.learningReview.loading && !state.learningReview.busy, count);
  };
  const feedback = async (key, outcome) => {
    const response = await page.request.post(`${process.env.MW_QA_URL}/v1/knowledge/feedback`, {
      headers:{Authorization:`Bearer ${process.env.MW_QA_TOKEN}`},
      data:{agent_id:'human-review', project_key:'ui-batch', knowledge_id:fixture.keys[key], outcome,
        evidence_summary:'Another client reviewed the synthetic fixture.', evidence_kind:'user_approval', evidence_ref:'qa://other-client'}
    });
    assert.equal(response.status(),200);
  };
  const checkLayout = async () => {
    const bounds = await dialog.evaluate(element => {
      const rect = element.getBoundingClientRect();
      const footer = element.querySelector('.learning-footer').getBoundingClientRect();
      return {width:rect.width, right:rect.right, bottom:rect.bottom, viewportWidth:innerWidth, viewportHeight:innerHeight,
        overflow:element.scrollWidth > element.clientWidth + 1,
        bodyOverflow:element.querySelector('.dialog-body').scrollWidth > element.querySelector('.dialog-body').clientWidth + 1,
        footerBottom:footer.bottom};
    });
    assert.equal(bounds.overflow,false,JSON.stringify(bounds));
    assert.equal(bounds.bodyOverflow,false,JSON.stringify(bounds));
    assert.ok(bounds.right <= bounds.viewportWidth && bounds.bottom <= bounds.viewportHeight && bounds.footerBottom <= bounds.viewportHeight);
  };
  const checkToast = async theme => {
    await page.evaluate(theme => {
      document.documentElement.setAttribute('data-theme',theme);
      showToast('已配置 WorkBuddy 的接入，等待客户端触发执行');
    },theme);
    await page.waitForFunction(() => {
      const style = getComputedStyle(document.querySelector('#toast'));
      return Number(style.opacity) > .99 && Math.abs(new DOMMatrixReadOnly(style.transform).m42) < .1;
    });
    const style = await page.locator('#toast').evaluate(element => {
      const text = element.querySelector('#toastMsg');
      const rect = element.getBoundingClientRect();
      const textRect = text.getBoundingClientRect();
      return {foreground:getComputedStyle(text).color,background:getComputedStyle(element).backgroundColor,
        left:rect.left,right:rect.right,top:rect.top,bottom:rect.bottom,viewport:innerWidth,viewportHeight:innerHeight,
        overflow:element.scrollWidth>element.clientWidth+1,
        textLeft:textRect.left,textRight:textRect.right,textTop:textRect.top,textBottom:textRect.bottom};
    });
    const luminance = rgb => {
      const values=rgb.match(/\d+/g).slice(0,3).map(value=>{
        const x=Number(value)/255;return x<=.04045?x/12.92:Math.pow((x+.055)/1.055,2.4);
      });
      return values[0]*.2126+values[1]*.7152+values[2]*.0722;
    };
    const light=luminance(style.foreground),dark=luminance(style.background);
    assert.ok((Math.max(light,dark)+.05)/(Math.min(light,dark)+.05)>=4.5,JSON.stringify(style));
    assert.ok(style.left>=0&&style.right<=style.viewport&&style.textLeft>=style.left&&style.textRight<=style.right);
    assert.ok(style.top>=0&&style.bottom<=style.viewportHeight&&style.textTop>=style.top&&style.textBottom<=style.bottom);
    assert.equal(style.overflow,false);
    await page.locator('#toast').screenshot({path:output(`toast-${theme}.png`),animations:'disabled'});
  };
  try {
    await page.goto(url,{waitUntil:'domcontentloaded'});
    const workbuddy = page.locator('#agentRows tr').filter({hasText:'workbuddy'});
    await workbuddy.waitFor();
    await page.waitForFunction(() => !!state.overview && !agentLoadPromise && !syncRunning);
    assert.ok((await workbuddy.innerText()).includes('接入待修复'),'old runtime-only WorkBuddy must offer a real repair');
    assert.equal(await workbuddy.getByRole('button',{name:'修复接入',exact:true}).count(),1);
    await checkToast('light');
    await page.setViewportSize({width:390,height:844});
    await checkToast('dark');
    await page.setViewportSize({width:1440,height:1000});
    await page.evaluate(() => document.documentElement.setAttribute('data-theme','light'));
    await page.locator('#agentId').fill('unknown-fixture');
    await page.locator('#agentName').fill('Unknown fixture');
    await page.locator('#registerAgent').click();
    const unknown=page.locator('#agentRows tr').filter({hasText:'unknown-fixture'});
    await unknown.waitFor();
    assert.ok((await unknown.innerText()).includes('已登记，未接入'));
    await page.waitForFunction(() => document.querySelector('#toastMsg').textContent==='已登记 Unknown fixture，尚未接入自动召回和学习');
    assert.equal(await page.locator('#toast').getAttribute('data-kind'),'warning');
    assert.equal(await unknown.getByRole('button',{name:'修复接入',exact:true}).count(),0);
    const beforeDisable = Number(await page.locator('#connectedAgents').innerText());
    // Hold an old registry snapshot while the disable request commits.
    let captured, releaseRegistry;
    const registryCaptured = new Promise(resolve => {captured=resolve;});
    const registryGate = new Promise(resolve => {releaseRegistry=resolve;});
    let delayedRegistry = false;
    await page.route('**/v1/agents?include_disabled=true', async route => {
      if (!delayedRegistry) {
        delayedRegistry=true;
        const response=await route.fetch();
        captured();
        await registryGate;
        await route.fulfill({response});
      } else await route.continue();
    });
    await page.evaluate(() => {window.qaRegistryRefresh=loadAgents({force:true});});
    await registryCaptured;
    await workbuddy.getByRole('button',{name:'停用',exact:true}).click();
    await workbuddy.waitFor({state:'hidden',timeout:3000});
    assert.equal(Number(await page.locator('#connectedAgents').innerText()),beforeDisable-1);
    releaseRegistry();
    await page.evaluate(() => window.qaRegistryRefresh);
    await page.unroute('**/v1/agents?include_disabled=true');
    await page.evaluate(() => loadAgents({force:true}));
    assert.equal(await workbuddy.count(),0,'old snapshots must not resurrect a disabled row');
    assert.equal(await page.locator('#agentRows [data-disable-agent="disabled-agent"]').count(),0);
    await page.locator('#agentCandidate').click();
    const workbuddyOption=page.locator('#agentOptions [data-agent-id="workbuddy"]');
    assert.equal(await workbuddyOption.evaluate(element => element.classList.contains('joined')),false);
    await workbuddyOption.click();
    await page.locator('#registerAgent').click();
    await workbuddy.waitFor();
    await page.waitForFunction(() => state.registeredAgents.find(item=>item.agent_id==='workbuddy')?.hook?.configured===true);
    assert.ok((await workbuddy.innerText()).includes('通用事件 Hook'));
    assert.ok((await workbuddy.innerText()).includes('已配置'));
    assert.ok(!(await workbuddy.innerText()).includes('已记录执行'),'configuration cannot claim live agent execution');
    assert.equal(await workbuddy.getByRole('button',{name:'修复接入',exact:true}).count(),1);
    await page.waitForFunction(() => document.querySelector('#toastMsg').textContent==='已配置 WorkBuddy 的接入，等待客户端触发执行');
    assert.equal(Number(await page.locator('#connectedAgents').innerText()),beforeDisable);
    // A rejected mutation must leave the enabled row intact and retryable.
    await page.route('**/v1/agents/disable', route => route.fulfill({status:503,contentType:'application/json',body:JSON.stringify({detail:'QA disable failure'})}));
    await workbuddy.getByRole('button',{name:'停用',exact:true}).click();
    await page.locator('#error').getByText('QA disable failure').waitFor();
    assert.equal(await workbuddy.count(),1);
    assert.equal(await workbuddy.getByRole('button',{name:'停用',exact:true}).isEnabled(),true);
    await page.unroute('**/v1/agents/disable');
    await workbuddy.getByRole('button',{name:'停用',exact:true}).click();
    await workbuddy.waitFor({state:'hidden'});
    await page.locator('#agentRows').screenshot({path:output('agents-after-disable.png')});
    await row('codex').getByRole('button',{name:'生成 8 条候选',exact:true}).click();
    await ready();
    assert.equal(await dialog.locator('.learning-entry').count(),8);
    assert.ok(!(await dialog.innerText()).includes('上一轮候选'));
    assert.ok(!(await dialog.innerText()).includes('另一个 Agent 的候选'));
    assert.ok((await dialog.innerText()).includes('Synthetic fixture evidence'));
    assert.equal(await page.evaluate(() => window.qaInjected),undefined);
    await checkLayout();
    await page.screenshot({path:output('desktop-all.png')});
    await page.locator('#learningPending').click();
    await ready();
    assert.equal(await dialog.locator('.learning-entry').count(),7);
    await entry('a').getByRole('checkbox').check();
    await entry('e').getByRole('checkbox').check();
    await page.locator('#learningReason').fill('QA: 核对正文与来源证据。');
    await page.locator('#learningApprove').click();
    await pending(5);
    await row('codex').getByRole('button',{name:'待审核 5 条',exact:true}).waitFor();
    assert.equal(await row('codex').getByRole('button',{name:'生成 8 条候选',exact:true}).count(),1);
    await entry('b').getByRole('button',{name:'隔离',exact:true}).click();
    await pending(4);
    // No page interaction after this independent client's write.
    await feedback('c','verified');
    await pending(3);
    await row('codex').getByRole('button',{name:'待审核 3 条',exact:true}).waitFor();
    // Force the other client to win between the click and the HTTP transaction.
    await page.route('**/v1/knowledge/feedback', async route => {
      if (route.request().postDataJSON().knowledge_id === fixture.keys.d) await feedback('d','verified');
      await route.continue();
    });
    await entry('d').getByRole('button',{name:'隔离',exact:true}).click();
    await pending(2);
    await page.unroute('**/v1/knowledge/feedback');
    assert.equal(await entry('d').count(),0);
    assert.ok((await page.locator('#toastMsg').innerText()).includes('已同步其他位置'));
    await page.locator('#learningSelectAll').check();
    await page.locator('#learningApprove').click();
    await pending(1);
    assert.ok((await page.locator('#learningError').innerText()).includes('1 条未通过校验'));
    assert.equal(await entry('f').getByRole('checkbox').isChecked(),true);
    await page.screenshot({path:output('desktop-partial-review.png')});
    await entry('f').getByRole('button',{name:'隔离',exact:true}).click();
    await pending(0);
    assert.ok((await dialog.innerText()).includes('本次学习暂无待审核候选'));
    await page.locator('#learningAll').click();
    await ready();
    assert.equal(await dialog.locator('.learning-entry').count(),8);
    assert.equal(await dialog.locator('.badge.active').count(),6);
    assert.equal(await dialog.locator('.badge.quarantined').count(),2);
    assert.equal(await page.evaluate(() => window.qaInjected),undefined);
    await page.setViewportSize({width:390,height:844});
    await entry('e').locator('summary').click();
    await checkLayout();
    await page.screenshot({path:output('mobile-long-content.png')});
    await page.setViewportSize({width:1440,height:1000});
    // Independent process uses the same write path as a local learning hook.
    execFileSync(process.env.MW_QA_PYTHON,[path.join(__dirname,'qa_learning_review.py'),'--mutate',process.env.MW_QA_DATABASE],{env:process.env,windowsHide:true});
    await row('codex').getByRole('button',{name:'生成 1 条候选',exact:true}).waitFor();
    assert.equal(await dialog.locator('.learning-entry').count(),8,'open review must stay on its original batch');
    await page.locator('#learningClose').click();
    await row('codex').getByRole('button',{name:'待审核 1 条',exact:true}).click();
    await ready();
    assert.ok((await dialog.innerText()).includes('后台新增学习候选'));
    await page.keyboard.press('Escape');
    // A failed query has an honest error state and can recover independently.
    let failedRead = false;
    await page.route('**/v1/learning/run-records', async route => {
      if (!failedRead && route.request().postDataJSON().source_agent === 'empty-agent') {
        failedRead=true;
        await route.fulfill({status:503, contentType:'application/json', body:JSON.stringify({detail:'QA temporary failure'})});
      } else await route.continue();
    });
    await row('empty-agent').getByRole('button',{name:'生成 0 条候选',exact:true}).click();
    await page.locator('#learningRetry').waitFor();
    assert.ok((await page.locator('#learningError').innerText()).includes('QA temporary failure'));
    // The transient read failure recovers without clicking Retry or Refresh.
    await ready();
    assert.ok((await dialog.innerText()).includes('暂无可追溯'));
    await page.unrouteAll({behavior:'wait'});
    await page.locator('#learningClose').click();
    // A late response from a closed modal must not overwrite a new batch.
    let release;
    const gate = new Promise(resolve => {release=resolve;});
    await page.route('**/v1/learning/run-records', async route => {
      if (route.request().postDataJSON().source_agent === 'empty-agent') await gate;
      await route.continue();
    });
    await row('empty-agent').getByRole('button',{name:'生成 0 条候选',exact:true}).click();
    await page.locator('#learningMessage').getByText('正在读取本次学习…').waitFor();
    await page.locator('#learningClose').click();
    await row('codex').getByRole('button',{name:'生成 1 条候选',exact:true}).click();
    await ready();
    release();
    await page.unrouteAll({behavior:'wait'});
    assert.ok((await dialog.innerText()).includes('后台新增学习候选'));
    await page.locator('#learningClose').click();
    // Small screen, long text, dark theme, keyboard tabs, and paginated batch review.
    await page.setViewportSize({width:390,height:844});
    await row('page-agent').getByRole('button',{name:'待审核 23 条',exact:true}).click();
    await ready();
    await checkLayout();
    await page.screenshot({path:output('mobile-pending.png')});
    await page.locator('#learningNext').click();
    await ready();
    assert.equal(await dialog.locator('.learning-entry').count(),3);
    await page.locator('#learningSelectAll').check();
    await page.locator('#learningReject').click();
    await pending(20);
    assert.equal(await dialog.locator('.learning-entry').count(),20,'empty last page must return to the previous page');
    await page.locator('#learningPending').focus();
    await page.keyboard.press('ArrowLeft');
    await ready();
    assert.equal(await page.locator('#learningAll').getAttribute('aria-selected'),'true');
    await page.evaluate(() => document.documentElement.setAttribute('data-theme','dark'));
    await checkLayout();
    await page.screenshot({path:output('mobile-dark.png')});
    await page.locator('#learningClose').click();
    await page.setViewportSize({width:1440,height:1000});
    await page.evaluate(() => document.documentElement.setAttribute('data-theme','light'));
    // Automatic reconnect retries a failed stream without any refresh click.
    let eventRequests=0;
    await page.route('**/v1/dashboard/events', async route => {
      eventRequests++;
      if (eventRequests === 1) await route.abort(); else await route.continue();
    });
    await page.reload({waitUntil:'domcontentloaded'});
    await page.waitForFunction(() => eventsConnected);
    assert.ok(eventRequests >= 2);
    // External disables must also remove rows and show the empty state automatically.
    const registered=await page.request.get(`${process.env.MW_QA_URL}/v1/agents`,{headers:{Authorization:`Bearer ${process.env.MW_QA_TOKEN}`}});
    for (const agent of (await registered.json()).results) {
      const response=await page.request.post(`${process.env.MW_QA_URL}/v1/agents/disable`,{
        headers:{Authorization:`Bearer ${process.env.MW_QA_TOKEN}`},data:{agent_id:agent.agent_id}
      });
      assert.equal(response.status(),200);
    }
    await page.locator('#agentEmpty').waitFor();
    assert.equal(await page.locator('#agentRows tr').count(),0);
    assert.equal(Number(await page.locator('#connectedAgents').innerText()),0);
    await page.setViewportSize({width:390,height:844});
    await page.locator('#agentsModule').screenshot({path:output('mobile-agents-empty.png')});
    assert.deepEqual(errors,[]);
    const report = {batchIsolation:true, generatedCountStable:true, bulkReview:true, partialFailure:true,
      staleRequestAutoSync:true, externalReviewAutoSync:true, independentProcessAutoSync:true,
      originalBatchPreserved:true, emptyState:true, readFailureAutoRetry:true, lateResponseGuard:true, pagination:true,
      mobileLayout:true, desktopLayout:true, lightAndDark:true, escapedContent:true, streamReconnect:true,
      disabledAgentHidden:true, staleRegistryGuard:true, agentRejoin:true, failedDisablePreservesRow:true,
      externalDisableAutoSync:true, emptyAgentList:true, toastContrast:true, mobileToastLayout:true,
      workbuddyUpgrade:true, honestRegistrationStatus:true, pageErrors:errors};
    fs.writeFileSync(output('report.json'),JSON.stringify(report,null,2));
    console.log(JSON.stringify(report,null,2));
  } finally { await browser.close(); }
}

main().catch(error => {console.error(error); process.exitCode=1;});
