const fs = require("fs");
const { chromium } = require("playwright");

async function main() {
  const state = JSON.parse(
    fs.readFileSync(process.env.MW_QA_STATE_PATH, "utf8").replace(/^\uFEFF/, "")
  );
  const browser = await chromium.launch({
    headless: true,
    executablePath: process.env.MW_QA_BROWSER_PATH,
  });
  const page = await browser.newPage({ viewport: { width: 1440, height: 1000 } });
  const errors = [];
  page.on("console", message => {
    if (message.type() === "error") errors.push(message.text());
  });
  page.on("pageerror", error => errors.push(error.message));
  const url = `${process.env.MW_QA_URL || state.url}/knowledge#token=${process.env.MW_QA_TOKEN || state.token}&project=claude-codex-mvp`;
  await page.goto(url, { waitUntil: "networkidle" });
  await page.waitForFunction(() => document.querySelector("#count").textContent !== "0 条");
  const projectChoices = await page.locator("#project option").evaluateAll(options => options.map(option => option.value));
  let projectSwitchWorked = true;
  if (projectChoices.length > 1) {
    const current = await page.locator("#project").inputValue();
    const other = projectChoices.find(project => project !== current);
    await Promise.all([
      page.waitForResponse(response => response.url().endsWith("/v1/knowledge/overview")),
      page.locator("#project").selectOption(other),
    ]);
    projectSwitchWorked = await page.locator("#project").inputValue() === other;
    await Promise.all([
      page.waitForResponse(response => response.url().endsWith("/v1/knowledge/overview")),
      page.locator("#project").selectOption(current),
    ]);
    await page.waitForFunction(() => document.querySelector("#count").textContent !== "0 条");
  }
  const filterOverflow = async () => page.locator(".knowledge-filters").evaluate(
    element => element.scrollWidth > element.clientWidth + 1
  );
  const wideFilterOverflow = await filterOverflow();
  await page.locator("#agentCandidate").click();
  const agentStates = await page.locator("#agentOptions .agent-option").evaluateAll(options => ({
    joined: options.filter(option => option.classList.contains("joined")).length,
    available: options.filter(option => option.dataset.agentId && !option.classList.contains("joined")).length,
    joinedColor: getComputedStyle(options.find(option => option.classList.contains("joined"))).color,
    availableColor: getComputedStyle(options.find(option => option.dataset.agentId && !option.classList.contains("joined"))).color,
  }));
  await page.evaluate(() => window.scrollTo(0, 0));
  await page.screenshot({
    path: process.env.MW_QA_SCREENSHOT_PATH.replace(/\.png$/, "-light-menu.png"),
    fullPage: false,
    animations: "disabled",
  });
  await page.locator('#agentOptions [data-agent-id="cursor"]').click();
  const availableSelected = await page.locator("#agentId").inputValue() === "cursor" &&
    !(await page.locator("#agentCandidate").evaluate(element => element.classList.contains("joined")));
  await page.locator("#agentCandidate").click();
  await page.locator('#agentOptions [data-agent-id="claude-code"]').click();
  const joinedSelected = await page.locator("#agentId").inputValue() === "claude-code" &&
    await page.locator("#agentCandidate").evaluate(element => element.classList.contains("joined"));
  await page.locator("#agentCandidate").focus();
  await page.keyboard.press("ArrowDown");
  const keyboardOpened = await page.locator("#agentCandidate").getAttribute("aria-expanded") === "true";
  await page.keyboard.press("Escape");
  const keyboardClosed = await page.locator("#agentCandidate").getAttribute("aria-expanded") === "false";
  await page.locator("#agentCandidate").click();
  await page.locator('#agentOptions [data-agent-id=""]').click();
  const customCleared = await page.locator("#agentId").inputValue() === "";
  await page.locator("#updatedFrom").fill("2999-01-01");
  const startDateWorked = await page.locator("#count").textContent() === "0 条";
  await page.locator("#updatedFrom").fill("");
  await page.locator("#updatedTo").fill("2000-01-01");
  const endDateWorked = await page.locator("#count").textContent() === "0 条";
  await page.locator("#updatedTo").fill("");
  await page.locator("#query").fill("refresh-check");
  await page.locator("#status").selectOption("candidate");
  await page.locator("#updatedFrom").fill("2020-01-01");
  await Promise.all([
    page.waitForResponse(response => response.url().endsWith("/v1/knowledge/overview")),
    page.locator("#refresh").click(),
  ]);
  const refreshResetWorked = await page.evaluate(() => ({
    query: document.querySelector("#query").value,
    status: document.querySelector("#status").value,
    sharing: document.querySelector("#sharing").value,
    knowledgeType: document.querySelector("#knowledgeType").value,
    scope: document.querySelector("#scope").value,
    sourceAgent: document.querySelector("#sourceAgent").value,
    updatedFrom: document.querySelector("#updatedFrom").value,
    updatedTo: document.querySelector("#updatedTo").value,
  }));
  await page.setViewportSize({ width: 1100, height: 850 });
  const mediumFilterOverflow = await filterOverflow();
  await page.screenshot({ path: process.env.MW_QA_SCREENSHOT_PATH.replace(/\.png$/, "-medium.png"), fullPage: false });
  await page.setViewportSize({ width: 1440, height: 1000 });
  const pending = page.locator("#pendingModule");
  const adopted = page.locator("#adoptedModule");
  const initiallyPendingOpen = await pending.evaluate(element => element.open);
  const initiallyAdoptedOpen = await adopted.evaluate(element => element.open);
  await pending.locator("summary").click();
  const pendingCollapsed = !(await pending.evaluate(element => element.open));
  await pending.locator("summary").click();
  await adopted.locator("summary").click();
  const adoptedExpanded = await adopted.evaluate(element => element.open);
  const pendingChecks = page.locator("#pendingRows input.knowledge-check");
  const pendingCheckCount = await pendingChecks.count();
  await pendingChecks.first().check();
  const bulkSingleWorked = await page.locator("#selectedPendingCount").textContent() === "已选 1 条" &&
    !(await page.locator("#batchApprove").isDisabled()) && !(await page.locator("#selectAllPending").isChecked());
  await page.locator("#selectAllPending").check();
  const bulkSelectAllWorked = await page.locator("#pendingRows input.knowledge-check:checked").count() === pendingCheckCount &&
    await page.locator("#selectAllPending").isChecked();
  await page.locator("#selectAllPending").uncheck();
  const bulkClearWorked = await page.locator("#pendingRows input.knowledge-check:checked").count() === 0 &&
    await page.locator("#batchApprove").isDisabled();
  const firstRow = page.locator("#pendingRows tr, #adoptedRows tr").first();
  if (!(await page.locator("#pendingRows tr").count())) await adopted.locator("summary").click();
  await firstRow.click();
  await page.waitForFunction(() => document.querySelector("#detail").open);
  const dialogVisible = await page.locator("#detail").isVisible();
  await page.locator("#close").click();
  await page.locator("#status").selectOption("candidate");
  const statusFilterWorked = await page.locator("#adoptedRows tr").count() === 0 &&
    await page.locator("#pendingRows tr").count() > 0;
  await page.locator("#status").selectOption("all");
  const title = await page.locator("#pendingRows .knowledge-title").first().textContent();
  await page.locator("#query").fill(title);
  const searchWorked = await page.locator("#pendingRows tr").count() === 1;
  await page.locator("#query").fill("");
  const result = await page.evaluate(() => ({
    title: document.title,
    rowCount: document.querySelectorAll("#pendingRows tr, #adoptedRows tr").length,
    pendingCount: document.querySelector("#pendingCount").textContent,
    adoptedCount: document.querySelector("#adoptedCount").textContent,
    filterRowCount: document.querySelectorAll(".knowledge-filters").length,
    bodyOverflow: document.documentElement.scrollWidth > document.documentElement.clientWidth,
    pendingEmptyVisible: !document.querySelector("#pendingEmpty").hidden,
    adoptedEmptyVisible: !document.querySelector("#adoptedEmpty").hidden,
    errorVisible: !document.querySelector("#error").hidden,
    errorText: document.querySelector("#error").textContent,
  }));
  if (await adopted.evaluate(element => element.open)) await adopted.locator("summary").click();
  await page.evaluate(() => window.scrollTo(0, 0));
  await page.screenshot({ path: process.env.MW_QA_SCREENSHOT_PATH, fullPage: true });
  await page.locator("#themeToggle").click();
  await page.waitForTimeout(450);
  await page.screenshot({ path: process.env.MW_QA_SCREENSHOT_PATH.replace(/\.png$/, "-dark.png"), fullPage: false });
  await page.locator("#themeToggle").click();
  await page.waitForTimeout(450);
  await page.setViewportSize({ width: 390, height: 844 });
  await page.evaluate(() => window.scrollTo(0, 0));
  await page.screenshot({
    path: process.env.MW_QA_MOBILE_SCREENSHOT_PATH,
    fullPage: true,
  });
  const mobileOverflow = await page.evaluate(
    () => document.documentElement.scrollWidth > document.documentElement.clientWidth
  );
  await browser.close();
  console.log(JSON.stringify({ ...result, projectSwitchWorked, agentStates, availableSelected, joinedSelected,
    customCleared, keyboardOpened, keyboardClosed, startDateWorked, endDateWorked,
    wideFilterOverflow, mediumFilterOverflow, initiallyPendingOpen, initiallyAdoptedOpen,
    pendingCollapsed, adoptedExpanded, pendingCheckCount, bulkSingleWorked,
    bulkSelectAllWorked, bulkClearWorked, refreshResetWorked, statusFilterWorked, searchWorked,
    dialogVisible, mobileOverflow, errors }, null, 2));
  if (result.bodyOverflow || mobileOverflow || result.errorVisible || !dialogVisible ||
      !initiallyPendingOpen || initiallyAdoptedOpen || !pendingCollapsed || !adoptedExpanded ||
      !bulkSingleWorked || !bulkSelectAllWorked || !bulkClearWorked ||
      refreshResetWorked.query !== "" || refreshResetWorked.status !== "all" ||
      refreshResetWorked.sharing !== "all" || refreshResetWorked.knowledgeType !== "all" ||
      refreshResetWorked.scope !== "all" || refreshResetWorked.sourceAgent !== "all" ||
      refreshResetWorked.updatedFrom !== "" || refreshResetWorked.updatedTo !== "" ||
      !statusFilterWorked || !searchWorked || !availableSelected || !joinedSelected || !customCleared ||
      !keyboardOpened || !keyboardClosed || !startDateWorked || !endDateWorked || !projectSwitchWorked ||
      wideFilterOverflow || mediumFilterOverflow || agentStates.joined < 1 ||
      agentStates.available < 1 || agentStates.joinedColor === agentStates.availableColor || errors.length) process.exit(1);
}

main().catch(error => {
  console.error(error);
  process.exit(1);
});
