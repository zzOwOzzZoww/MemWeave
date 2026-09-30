const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const {execFileSync} = require('node:child_process');
const {chromium} = require('playwright');

const captions = {
  zh: [
    ['Codex 留下项目约定', '会话结束事件进入本地 Runtime，提炼一条候选。'],
    ['先进入候选，不直接生效', '真实管理页中查看正文、来源 Agent 和提炼证据。'],
    ['未经确认，Claude Code 不会拿到它', '相同问题调用真实召回 Hook，候选不进入上下文。'],
    ['人工核对后，批准候选', '审核写入证据，candidate 晋升为 active。'],
    ['换到 Claude Code，复用 Codex 的约定', '真实 Hook 上下文节选：保留约定与来源，不展示模型回答。'],
    ['无关问题，仍然返回空', '共享记忆不是每次都注入，天气问题不携带测试约定。'],
  ],
  en: [
    ['Codex leaves a project rule', 'The end-of-turn hook sends a synthetic session to the local Runtime.'],
    ['Candidate first, not permanent truth', 'Inspect the real review UI: content, source agent and evidence.'],
    ['Unapproved knowledge stays out', 'The real Claude Code recall hook returns no candidate context.'],
    ['Review and approve the candidate', 'Human approval supplies evidence: candidate becomes active.'],
    ['Claude Code reuses the Codex rule', 'Excerpt from actual additionalContext: the rule and its source, not a model answer.'],
    ['Unrelated question? No injection.', 'A weather query receives no project testing knowledge.'],
  ],
};

async function main() {
  const fixture = JSON.parse(process.env.MW_DEMO_FIXTURE);
  const output = file => path.join(process.env.MW_DEMO_OUTPUT, file);
  const hook = phase => JSON.parse(execFileSync(process.env.MW_DEMO_PYTHON,
    ['-X', 'utf8', path.join(__dirname, 'record_readme_demo.py'), '--invoke', phase],
    {encoding:'utf8',timeout:25000,windowsHide:true}));
  const browserPath = process.env.MW_DEMO_BROWSER || [
    'C:/Program Files/Google/Chrome/Application/chrome.exe',
    'C:/Program Files (x86)/Microsoft/Edge/Application/msedge.exe',
  ].find(file => fs.existsSync(file));
  const browser = await chromium.launch({headless:true,executablePath:browserPath});
  const context = await browser.newContext({viewport:{width:1120,height:900},reducedMotion:'reduce'});
  const dashboard = await context.newPage();
  const frame = await context.newPage();
  await frame.setViewportSize({width:1120,height:760});
  const errors = [];
  dashboard.on('dialog', dialog => dialog.accept());
  dashboard.on('pageerror', error => errors.push(error.message));
  frame.on('pageerror', error => errors.push(error.message));
  // Discovery is unrelated to this isolated lifecycle demo; never scan real Agent homes.
  await dashboard.route('**/v1/agents/discover', route => route.fulfill({status:200,
    contentType:'application/json',body:JSON.stringify({results:[]})}));
  const logo = 'data:image/png;base64,' + fs.readFileSync(path.join(__dirname,
    '..','src','agent_knowledge_bridge','assets','memweave-icon.png')).toString('base64');
  async function capture(index, proof) {
    for (const language of ['zh','en']) {
      const [title, subtitle] = captions[language][index];
      await frame.setContent(`<!doctype html><html lang="${language}"><meta charset="utf-8"><style>
        *{box-sizing:border-box}body{margin:0;background:#f5f7f8;color:#162b2a;font-family:"Segoe UI","Microsoft YaHei",sans-serif;letter-spacing:0}
        header{height:86px;background:white;border-bottom:1px solid #dfe7e7;display:flex;align-items:center;padding:0 40px;gap:14px}
        header img{width:44px;height:44px}header strong{font-size:25px}header span{margin-left:auto;font-size:16px;color:#506565}
        section{padding:26px 40px 16px}h1{margin:0;font-size:26px;line-height:1.35;font-weight:650}p{margin:9px 0 0;font-size:16px;color:#506565}
        .proof{height:510px;margin:0 40px;display:flex;align-items:center;justify-content:center;overflow:hidden}
        .proof img{display:block;max-width:100%;max-height:100%;object-fit:contain}
        .terminal{width:100%;height:100%;background:#172725;color:#f2f7f6;padding:28px 32px;border-radius:6px}
        .bar{font-size:14px;color:#a7c8c3;margin-bottom:20px;border-bottom:1px solid #36504b;padding-bottom:14px}
        label{display:block;font-size:13px;color:#81dbbc;font-weight:600;margin:17px 0 8px}
        pre{font-family:"Cascadia Mono",Consolas,"Microsoft YaHei",monospace;font-size:19px;line-height:1.6;white-space:pre-wrap;overflow-wrap:anywhere;margin:0}
        .result{color:#dcf6e7;font-size:20px;line-height:1.6}.compact{font-size:15px;line-height:1.45}
        footer{height:54px;display:flex;align-items:center;padding:0 40px;gap:20px;color:#506565;font-size:13px}
        .progress{display:flex;gap:5px;margin-left:auto}.progress i{display:block;width:30px;height:4px;background:#dce5e4}.progress i.done{background:#14866a}
      </style><header><img src="${logo}" alt=""><strong>MemWeave</strong><span>Codex → MemWeave → Claude Code</span></header>
      <section><h1></h1><p></p></section><main class="proof"></main><footer><span></span><div class="progress">
      ${Array.from({length:6},(_,step)=>`<i class="${step<=index?'done':''}"></i>`).join('')}</div><b>${index+1} / 6</b></footer></html>`);
      await frame.locator('h1').evaluate((element, text) => element.textContent = text, title);
      await frame.locator('section p').evaluate((element, text) => element.textContent = text, subtitle);
      await frame.locator('footer span').evaluate((element, text) => element.textContent = text,
        language==='zh'?'合成会话 · 本地模拟提炼 · 真实 Hook / Runtime / 审核':'Synthetic session / mock reviewer / real hooks, Runtime and review');
      await frame.locator('.proof').evaluate((element, proof) => {
        if (proof.image) {
          const image = document.createElement('img'); image.src=proof.image; image.alt='MemWeave review UI'; element.append(image);
        } else {
          element.innerHTML='<div class="terminal"><div class="bar"></div><label>INPUT</label><pre class="input"></pre><label class="output-label"></label><pre class="result"></pre></div>';
          element.querySelector('.bar').textContent=proof.client;
          element.querySelector('.input').textContent=proof.input;
          element.querySelector('.output-label').textContent=proof.label;
          element.querySelector('.result').textContent=proof.output;
          if (proof.output.length>600) element.querySelector('.result').classList.add('compact');
        }
      },proof);
      await frame.evaluate(async () => {
        await document.fonts.ready;
        await Promise.all([...document.images].map(image=>image.decode()));
      });
      assert.equal(await frame.evaluate(() => {
        const box=document.querySelector('.proof').getBoundingClientRect();
        const result=document.querySelector('.result');
        return document.documentElement.scrollWidth>innerWidth || document.documentElement.scrollHeight>innerHeight ||
          !!(result && result.getBoundingClientRect().bottom>box.bottom-15);
      }),false,'demo text must fit in its frame');
      await frame.screenshot({path:output(`${language}-${String(index+1).padStart(2,'0')}.png`),animations:'disabled'});
    }
  }
  try {
    assert.deepEqual(hook('capture'),{});
    await dashboard.goto(`${process.env.MW_DAEMON_URL}/knowledge#token=${process.env.MW_DAEMON_TOKEN}&project=${fixture.project}`,
      {waitUntil:'domcontentloaded'});
    await dashboard.evaluate(() => document.documentElement.setAttribute('data-theme','light'));
    const row=dashboard.locator('#agentRows tr').filter({has:dashboard.locator('[data-learning-agent="codex"]')});
    await row.getByRole('button',{name:'生成 1 条候选',exact:true}).waitFor();
    await capture(0,{client:'Codex / Stop hook · synthetic session',input:fixture.rule,
      label:'END-OF-TURN RESULT',output:'Stop → local Runtime → 1 candidate\nSource: codex / codex-demo-session\nNo automatic promotion.'});
    await row.getByRole('button',{name:'生成 1 条候选',exact:true}).click();
    const dialog=dashboard.locator('#learningDialog');
    await dashboard.waitForFunction(() => state.learningReview?.data?.pending_count===1 && !state.learningReview.loading);
    assert.ok((await dialog.innerText()).includes(fixture.rule));
    assert.ok((await dialog.innerText()).includes('待审核'));
    const screenshot=async()=>{
      const clip=await dialog.evaluate(element=>{
        const bounds=element.getBoundingClientRect();
        const footer=element.querySelector('.learning-footer').getBoundingClientRect();
        return {x:bounds.x,y:bounds.y,width:bounds.width,height:footer.top-bounds.top};
      });
      return {image:'data:image/png;base64,'+(await dashboard.screenshot({clip,animations:'disabled'})).toString('base64')};
    };
    await capture(1,await screenshot());
    const blocked=hook('candidate');
    assert.deepEqual(blocked,{});
    await capture(2,{client:'Claude Code / UserPromptSubmit hook',input:fixture.question,
      label:'ACTUAL HOOK OUTPUT · candidate',output:JSON.stringify(blocked,null,2)});
    await dashboard.locator('#learningReason').fill('已核对项目约定：使用 uv 管理依赖，并运行 uv run pytest tests -q。');
    await dialog.getByRole('button',{name:'批准',exact:true}).click();
    await dashboard.waitForFunction(() => state.learningReview?.data?.pending_count===0 && !state.learningReview.busy);
    assert.ok((await dialog.innerText()).includes('已批准'));
    await capture(3,await screenshot());
    const recalled=hook('active');
    const injected=recalled.hookSpecificOutput?.additionalContext;
    assert.ok(injected?.includes('uv run pytest tests -q'));
    assert.ok(injected.includes('codex'));
    const excerpt=injected.split('\n').filter(line=>line.includes('uv run pytest tests -q') ||
      line==='source_label=Codex' || line.startsWith('source=codex ')).join('\n');
    assert.ok(excerpt.includes('source_label=Codex') && excerpt.includes('uv run pytest tests -q'));
    await capture(4,{client:'Claude Code / UserPromptSubmit hook',input:fixture.question,
      label:'ACTUAL additionalContext · excerpt · active',output:excerpt});
    const empty=hook('unrelated');
    assert.deepEqual(empty,{});
    await capture(5,{client:'Claude Code / UserPromptSubmit hook',input:'明天北京会下雨吗？',
      label:'ACTUAL HOOK OUTPUT · unrelated question',output:JSON.stringify(empty,null,2)});
    assert.deepEqual(errors,[]);
    fs.writeFileSync(output('hook-outputs.json'),JSON.stringify({candidate:blocked,active:recalled,unrelated:empty},null,2));
    console.log(JSON.stringify({native_stop_capture:true,candidate_visible:true,candidate_not_injected:true,
      human_approval:true,cross_agent_context:true,unrelated_empty:true,frame_layout:true,page_errors:errors}));
  } finally {
    await browser.close();
  }
}

main().catch(error => {console.error(error);process.exitCode=1;});
