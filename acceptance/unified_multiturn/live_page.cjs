"use strict";

// Keep the actual PI page attached while the two native research turns start.
const {openPage, sendMessage} = require("./browser_smoke.cjs");
const readline = require("node:readline");

async function main() {
  const {browser, page} = await openPage(process.argv[2]);
  const respond = value => process.stdout.write(JSON.stringify(value) + "\n");
  respond({ready:true});
  try {
    for await (const line of readline.createInterface({input:process.stdin, terminal:false})) {
      const command = JSON.parse(line);
      if (command.action === "close") break;
      try {
        if (command.action === "send") {
          const visibleRoutes = [];
          for (const ref of command.routeRefs || []) {
            const matched = page.locator("#branch-options option[value=" + JSON.stringify(ref) + "]");
            await matched.first().waitFor({state:"attached"});
            visibleRoutes.push({branch_id:ref, label:await matched.first().textContent()});
          }
          respond({...await sendMessage(page, command.text, command.target || {}), visibleRoutes});
        } else if (command.action === "refresh") {
          await page.reload({waitUntil:"domcontentloaded"});
          await page.locator("#messages").getByText(command.expected, {exact:false}).first().waitFor();
          respond({visible:true, text:await page.locator("#messages").innerText()});
        } else if (command.action === "select") {
          await page.locator("#run-select").selectOption(command.runId);
          respond({runId:await page.locator("#run-select").inputValue()});
        } else throw Error("Unknown browser action");
      } catch(error) {respond({error:error.message});}
    }
  } finally {await browser.close();}
}
main().catch(error => {console.error(error.message); process.exitCode=1;});
