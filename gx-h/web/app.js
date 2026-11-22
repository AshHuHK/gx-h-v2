"use strict";
const byId = (id) => document.getElementById(id);

async function request(path, body) {
  const options = body === undefined ? {} : {
    method: "POST", headers: {"Content-Type": "application/json"}, body: JSON.stringify(body)
  };
  const response = await fetch(path, options);
  const data = await response.json();
  if (!response.ok) throw new Error(data.error || response.statusText);
  return data;
}

async function openProject() {
  const base = "/api/science/conversations";
  const listing = await request(base);
  const project = listing.items[0].id;
  const path = base + "/" + encodeURIComponent(project);
  byId("project").textContent = project;
  document.title = project + " · GX";
  let lastMessage = null;

  async function refresh() {
    try {
      const projectView = await request(path);
      byId("status").textContent = [projectView.status, projectView.reason].filter(Boolean).join(" · ");
      byId("reply").hidden = !projectView.latest_root_reply;
      byId("reply").textContent = projectView.latest_root_reply || "";
      if (lastMessage) {
        const found = (projectView.feedback || []).find((row) => row.id === lastMessage);
        if (found) byId("receipt").textContent = "Feedback · " + found.status;
      }
    } catch (error) { byId("status").textContent = error.message; }
  }

  const launch = byId("launch");
  launch.hidden = !listing.can_launch;
  launch.onclick = async () => {
    launch.disabled = true;
    try {
      const receipt = await request(path + "/messages", {kind: "launch"});
      byId("receipt").textContent = "Project · " + receipt.status;
      await refresh();
    } catch (error) { byId("receipt").textContent = error.message; }
    finally { launch.disabled = false; }
  };
  // Reuse a message ID after a lost HTTP reply. Editing the text creates a new request.
  let draft = null;
  byId("form").onsubmit = async (event) => {
    event.preventDefault();
    const field = byId("text");
    if (!field.value.trim()) return;
    if (!draft || draft.text !== field.value) draft = {
      kind: "feedback", text: field.value, client_message_id: crypto.randomUUID()
    };
    byId("send").disabled = true;
    try {
      const result = await request(path + "/messages", draft);
      lastMessage = result.message_id;
      byId("receipt").textContent = "Feedback · " + result.delivery;
      if (field.value === draft.text) field.value = "";
      draft = null;
    } catch (error) { byId("receipt").textContent = error.message; }
    finally { byId("send").disabled = false; }
  };

  const events = new EventSource(path + "/events");
  events.onopen = () => { byId("connection").textContent = "Connected"; };
  events.onerror = () => { byId("connection").textContent = "Reconnecting…"; };
  events.onmessage = (event) => {
    const row = JSON.parse(event.data);
    const line = document.createElement("li");
    line.textContent = [row.at, row.entity, row.subject, row.status].join(" · ");
    const list = byId("timeline");
    list.prepend(line);
    if (list.children.length > 100) list.lastChild.remove();
  };
  await refresh();
  setInterval(refresh, 5000);
}
openProject().catch((error) => { byId("status").textContent = error.message; });
