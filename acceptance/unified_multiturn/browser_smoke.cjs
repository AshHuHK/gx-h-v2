"use strict";

// Use an installed browser and Playwright. Web and CLI share the scientific state.
const assert = require("node:assert/strict");
const fs = require("node:fs/promises");
const path = require("node:path");
const { execFile, spawn } = require("node:child_process");
const { promisify } = require("node:util");
const exec = promisify(execFile);
const repo = path.resolve(__dirname, "../..");

async function openPage(url, settings = {}) {
  const origin = new URL(url);
  assert.ok(["localhost", "127.0.0.1", "[::1]"].includes(origin.hostname), "Use the local GX Web address");
  const { chromium } = require(settings.playwright || process.env.GX_PLAYWRIGHT || "playwright");
  const browser = await chromium.launch({
    executablePath: settings.chromium || process.env.GX_CHROMIUM || undefined,
    headless: true,
    args: ["--disable-gpu", "--disable-dev-shm-usage", "--disable-background-networking",
      "--disable-sync", "--disable-component-update", "--proxy-server=http://127.0.0.1:7890",
      "--proxy-bypass-list=localhost;127.0.0.1;[::1]"],
  });
  try {
    const context = await browser.newContext({locale: "zh-CN", viewport: {width: 1280, height: 900}});
    await context.route(url => url.origin !== origin.origin, route => route.abort());
    const page = await context.newPage();
    await page.goto(origin.href, {waitUntil: "domcontentloaded"});
    await page.locator("#text").waitFor();
    await page.locator("#status").filter({hasText: "Connecting…"}).waitFor({state: "hidden"});
    return {browser, page};
  } catch (error) {
    await browser.close();
    throw error;
  }
}

async function sendMessage(page, text, {branchId, questionId, replyTo, attachment, parentMaterialRefs} = {}) {
  if ([branchId, questionId, replyTo, attachment, parentMaterialRefs].some(value => value !== undefined)) {
    const details = page.locator("#target-controls");
    if (!await details.evaluate(el => el.open)) await details.locator("summary").click();
  }
  if (attachment) await page.locator("#attachment").setInputFiles(attachment);
  if (parentMaterialRefs) {
    await page.locator("#follow").check();
    for (const ref of parentMaterialRefs) {
      const checkbox = page.locator("#parent-materials input:visible");
      const matches = await checkbox.all();
      const match = await Promise.all(matches.map(async field => (await field.inputValue()) === ref));
      assert.ok(match.includes(true), "Selected parent material is listed on the form");
      await matches[match.indexOf(true)].check();
    }
  }
  for (const [id, value] of [["#branch-id", branchId], ["#question-id", questionId], ["#reply-to", replyTo]]) {
    if (value !== undefined) {
      const locator = page.locator(id);
      if (await locator.evaluate(el => el.tagName === "SELECT")) await locator.selectOption(value);
      else await locator.fill(value);
    }
  }
  await page.locator("#text").fill(text);
  const [response] = await Promise.all([
    page.waitForResponse(r => r.request().method() === "POST" && new URL(r.url()).pathname.endsWith("/messages")),
    page.locator("#send").click()
  ]);
  const receipt = await response.json();
  assert.equal(response.status(), 202, JSON.stringify(receipt));
  return {payload: response.request().postDataJSON(), receipt};
}

module.exports = {openPage, sendMessage};

const { once } = require("node:events");
const { createInterface } = require("node:readline");

async function smoke(directory) {
  await fs.mkdir(directory, {recursive: true});
  const state = await fs.mkdtemp(path.join(path.resolve(directory), "web-state-"));
  const python = process.env.GX_PYTHON || path.join(repo, ".venv/bin/python");
  const env = {...process.env, GX_CODEX_OFFLINE: "1", PYTHONDONTWRITEBYTECODE: "1"};
  const project = "multiturn-browser";
  const api = "/api/science/conversations/" + project;
  const goal = "Inspect the supplied study without starting new computations.";
  const budget = {tokens: 200000, api_calls: 6, job_submissions: 6, worker_tasks: 20,
    cpu_seconds: 90, gpu_jobs: 0, gpu_minutes: 0, wall_hours: 1};
  const grant = path.join(state, "grant.json");
  await fs.writeFile(grant, JSON.stringify({project, research_effort: "quick", research_modules: [],
    plugins: [], capabilities: [], run_budget: budget,
    workspace_budget: Object.fromEntries(Object.entries(budget).map(([k, n]) => [k, 4 * n]))}));
  async function cli(args, allowed = []) {
    try {
      const {stdout} = await exec(python, ["-B", "bin/gx-p0", ...args, "--state", state], {cwd: repo, env});
      return JSON.parse(stdout);
    } catch (err) {
      if (allowed.includes(err.code)) return JSON.parse(err.stdout);
      throw err;
    }
  }
  async function newRun(key) {
    const result = await cli(["research", "--goal", goal, "--goal-config", grant, "--run-key", key,
      "--timeout", "0.01"], [75]);
    assert.ok(result.run_id);
    return result.run_id;
  }
  async function serve(port, directory = state) {
    const child = spawn(python, ["-B", "gx-h/bin/gx-web", "--state-dir", directory,
      "--project", project, "--port", String(port)], {cwd: repo, env});
    const lines = createInterface({input: child.stdout});
    const errors = [];
    child.stderr.on("data", data => errors.push(data.toString()));
    const result = await Promise.race([once(lines, "line"), once(child, "exit").then(() => {
      throw Error("gx-web exited: "+errors.join(""));
    })]);
    lines.close();
    const addr = /http:\/\/127\.0\.0\.1:\d+\//.exec(result[0]);
    if (!addr) { child.kill(); throw Error("gx-web did not publish its local address"); }
    return {child, url: addr[0]};
  }
  async function stop(child) {
    if (child.exitCode !== null || child.signalCode) return;
    const done = once(child, "exit");
    child.kill();
    await done;
  }
  const runA = await newRun("browser-A");
  const runB = await newRun("browser-B");
  const timeline = run => cli(["conversation", "--run-id", run]);
  const queued = (run, id, text) => cli(["message", "--run-id", run,
    "--message-id", id, "--text", text, "--queue-only"]);
  const initial = await cli(["stop","--run-id",runA]);
  assert.equal(initial.status, "PAUSED");
  const driverPids = [];
  let server, browser, driver;
  async function startDriver() {
    // A real, offline science driver on B owns the shared native connection.
    // A's busy Main can still receive messages during both driver lifetimes.
    driver = spawn(python, ["-B", "bin/gx-p0", "run", "--state", state, "--run-id", runB,
      "--timeout", "90"], {cwd: repo, env, stdio: "ignore"});
    const check = "from pathlib import Path\nimport sys,time\n" +
      "from gx_core.hostobs.session_receipts import native_active\n" +
      "p=Path(sys.argv[1]); until=time.monotonic()+8\n" +
      "while time.monotonic()<until and not native_active(p): time.sleep(.05)\n" +
      "assert native_active(p), 'offline driver did not acquire workspace'";
    await exec(python, ["-B","-c",check,state], {cwd:repo,env:{...env,PYTHONPATH:path.join(repo,"gx-h")}});
    assert.equal(driver.exitCode, null);
    driverPids.push(driver.pid);
  }
  try {
    await startDriver();
    server = await serve(0);
    const pageResult = await openPage(server.url);
    browser = pageResult.browser;
    const page = pageResult.page;
    await page.locator("#run-select").selectOption(runA);
    const guiText = "只讨论：请说明这个研究的现有依据。";
    const {receipt, payload} = await sendMessage(page, guiText);
    const id = receipt.message_id;
    assert.ok(id);
    assert.equal(payload.run_id, runA);
    const first = await timeline(runA);
    assert.equal(first.messages.filter(m => m.message_id === id && m.text === guiText).length, 1);

    await queued(runA, id, guiText);
    assert.equal((await timeline(runA)).messages.filter(m=>m.message_id===id).length, 1);
    const post = body => page.evaluate(async ({api, body}) => {
      const r = await fetch(api+"/messages", {method:"POST",headers:{"Content-Type":"application/json"},body:JSON.stringify(body)});
      return {status:r.status,body:await r.json()};
    }, {api,body});
    assert.equal((await post({...payload,text:"不同内容"})).status, 409);
    await page.locator("#run-select").selectOption(runB);
    assert.equal((await post({...payload,run_id:runB})).status, 409);
    assert.ok(!(await timeline(runB)).messages.some(m=>m.message_id===id));
    await page.locator("#run-select").selectOption(runA);
    await page.locator("#messages").getByText(guiText, {exact: true}).waitFor();
    await page.reload({waitUntil:"domcontentloaded"});
    await page.locator("#run-select").selectOption(runA);
    await page.locator("#messages").getByText(guiText, {exact: true}).waitFor();

    const lostText = "请保留尚未解决的问题。";
    let committed;
    await page.route("**/messages", async route => {
      const response = await route.fetch();
      assert.equal(response.status(), 202);
      committed = await response.json();
      await response.dispose();
      await route.abort("failed");
    }, {times: 1});
    await page.locator("#text").fill(lostText);
    await Promise.all([
      page.waitForEvent("requestfailed", {predicate: req => new URL(req.url()).pathname === api + "/messages"}),
      page.locator("#send").click(),
    ]);
    await page.reload({waitUntil: "domcontentloaded"});
    await page.locator("#text").waitFor();
    await page.locator("#receipt").filter({hasText: "no receipt"}).waitFor();
    assert.equal(await page.locator("#text").inputValue(), lostText);
    const retry = await sendMessage(page, lostText);
    assert.equal(retry.receipt.message_id, committed.message_id);
    assert.equal((await timeline(runA)).messages.filter(m => m.message_id === committed.message_id).length, 1);

    const cliId = "from-cli";
    const cliText = "请用中文回答。";
    const second = await queued(runA, cliId, cliText);
    assert.equal(second.message_id, cliId);
    await page.locator("#messages").getByText(cliText, {exact: true}).waitFor();

    const tick = "before-restart";
    await queued(runA, tick, "这条记录用于标记已经收到的消息。");
    const waitForMilestone = id => page.locator("#timeline li[data-event-id]").filter({hasText: id}).first().waitFor();
    const cursors = () => page.locator("#timeline li[data-event-id]").evaluateAll(
      items => items.map(item => item.dataset.eventId).reverse());
    await waitForMilestone(tick);
    const last = (await cursors()).at(-1);
    const reconnected = page.waitForRequest(req => {
      const url = new URL(req.url());
      return url.pathname === api + "/events" && url.searchParams.get("run_id") === runA &&
        req.headers()["last-event-id"] === last;
    });
    await stop(server.child);
    await stop(driver);
    const missed = "while-disconnected";
    await queued(runA, missed, "断线期间的追问。");
    await startDriver();
    server = await serve(new URL(server.url).port);
    const resumedRequest = await reconnected;
    await waitForMilestone(missed);
    const deliveredCursors = await cursors();
    assert.equal(new Set(deliveredCursors).size, deliveredCursors.length);
    assert.ok(deliveredCursors.every((e,i) => i === 0 || Number(e) > Number(deliveredCursors[i - 1])));
    await page.locator("#messages").getByText("断线期间的追问。", {exact: true}).waitFor();
    await page.screenshot({path:path.join(state,"conversation.png"),fullPage:true});

    const reopened = await timeline(runA);
    const receiptIds = reopened.messages.map(m=>m.message_id);
    for(const expected of [id,cliId,tick,missed]) assert.equal(receiptIds.filter(mid=>mid===expected).length,1);
    const after = await cli(["status","--run-id",runA]);
    assert.equal(after.status, initial.status);
    assert.deepEqual(after.jobs, initial.jobs);
    assert.deepEqual(after.decisions, initial.decisions);

    // A saved legacy session descriptor still selects the original Native inbox.
    await browser.close();
    browser = null;
    await stop(server.child);
    const legacyDirectory = await fs.mkdtemp(path.join(path.resolve(directory), "native-control-"));
    await fs.writeFile(path.join(legacyDirectory, "native-session.json"), "{}");
    server = await serve(0, legacyDirectory);
    const legacy = await openPage(server.url);
    browser = legacy.browser;
    const legacyReply = await sendMessage(legacy.page, "旧版项目应继续从原收件箱接收反馈。");
    assert.equal(legacyReply.receipt.delivery, "queued");
    await legacy.page.locator("#receipt").getByText("Feedback · queued", {exact:true}).waitFor();
    await fs.access(path.join(legacyDirectory, "web-messages.sqlite3"));
    await assert.rejects(fs.access(path.join(legacyDirectory, "gx.db")), {code:"ENOENT"});

    const summary = {state, runs:[runA,runB],browser_version:browser.version(),web_message_id:id,
      lost_http_response_replay:committed.message_id,driver_processes:driverPids,paused_status:after.status,new_science_jobs:after.jobs.length - initial.jobs.length,
      legacy_native_inbox:{directory:legacyDirectory,...legacyReply.receipt},
      cli_message_id:cliId, across_run_conflict:409, changed_body_conflict:409,
      server_restart_last_event_id:resumedRequest.headers()["last-event-id"],delivered_cursors:deliveredCursors,
      status:reopened.messages.filter(m=>[id,cliId,tick,missed].includes(m.message_id)).map(m=>({id:m.message_id,status:m.status}))};
    await fs.writeFile(path.join(state,"browser.json"),JSON.stringify(summary,null,2)+"\n");
    console.log(JSON.stringify(summary,null,2));
  } finally {
    if(browser) await browser.close();
    if(server) await stop(server.child);
    if(driver) await stop(driver);
  }
}

if(require.main===module) {
  if(!process.argv[2]) throw Error("Usage: node browser_smoke.cjs OUTPUT_DIRECTORY");
  smoke(process.argv[2]).catch(error=>{console.error(error);process.exitCode=1;});
}
