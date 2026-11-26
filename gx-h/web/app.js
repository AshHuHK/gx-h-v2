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
  if (listing.backend === "p0") {
    await openResearch(path, listing.run_id);
    return;
  }
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

function element(tag, text) {
  const node = document.createElement(tag);
  if (text !== undefined) node.textContent = text;
  return node;
}

function choices(list, rows, id, describe) {
  const options = rows.map(row => {
    const option = element("option", describe(row) || row[id]);
    option.value = row[id];
    return option;
  });
  list.replaceChildren(...options);
}

async function openResearch(path, initial) {
  byId("discussion").hidden = byId("run-controls").hidden = byId("target-controls").hidden = false;
  byId("text-label").textContent = "Message the research team";
  byId("send").textContent = "Send message";
  let selected = new URL(location.href).searchParams.get("run_id") || initial;
  let shown = null;
  let events = null;
  let serial = 0;
  let refreshTimer;
  let lastView;
  const storageKey = (kind, rid) => "gx:" + path + ":" + rid + ":" + kind;

  function clearComposer() {
    for (const id of ["text", "reply-to", "branch-id", "question-id", "material-ref", "attachment"]) byId(id).value = "";
    byId("follow").checked = false;
    byId("parent-materials").hidden = true;
  }

  function restoreComposer(rid) {
    clearComposer();
    const raw = sessionStorage.getItem(storageKey("pending", rid));
    if (!raw) return;
    const draft = JSON.parse(raw);
    byId("text").value = draft.text;
    byId("reply-to").value = draft.reply_to || "";
    byId("branch-id").value = draft.branch_id || "";
    byId("question-id").value = draft.question_id || "";
    byId("material-ref").value = draft.attachments?.find(item => item.artifact_ref)?.artifact_ref || "";
    byId("follow").checked = Boolean(draft.successor);
    byId("parent-materials").hidden = !draft.successor;
    byId("receipt").textContent = "A previous send has no receipt in this tab. Resend to check it.";
  }

  function render(view) {
    const snapshot = JSON.stringify([view.runs, view.goal, view.status, view.goal_status, view.goal_version,
      view.branches, view.material_options, view.messages, view.questions]);
    if (snapshot === lastView) return;
    lastView = snapshot;
    const basis = (options = {}) => path + "/basis?" + new URLSearchParams({run_id: selected, ...options});
    byId("research-basis").href = basis({goal_version: view.goal_version});
    choices(byId("run-select"), view.runs, "run_id", r => [r.run_key, r.run_id, r.status].filter(Boolean).join(" · "));
    byId("run-select").value = selected;
    byId("goal").textContent = view.goal;
    byId("status").textContent = [view.status, view.goal_status, "Goal v" + view.goal_version].join(" · ");
    byId("follow-controls").hidden = view.goal_status !== "CLOSED";
    const parentBox = byId("parent-materials");
    const checked = new Set([...parentBox.querySelectorAll("input:checked")].map(n => n.value));
    const pending = sessionStorage.getItem(storageKey("pending", selected));
    if (pending) for (const ref of JSON.parse(pending).successor?.parent_material_refs || []) checked.add(ref);
    parentBox.replaceChildren(element("legend", "Originals to carry into the follow-up"));
    for (const mat of view.material_options) {
      const label = element("label");
      const checkbox = element("input"); checkbox.type = "checkbox"; checkbox.value = mat.artifact_ref;
      checkbox.checked = checked.has(mat.artifact_ref);
      label.append(checkbox, " " + (mat.name || mat.kind) + " · " + mat.artifact_ref);
      parentBox.append(label);
    }
    choices(byId("material-options"), view.material_options, "artifact_ref", m => m.name);
    choices(byId("reply-options"), view.messages, "message_id", m => m.text);
    choices(byId("branch-options"), view.branches, "branch_id", b => [b.question, b.status].filter(Boolean).join(" · "));
    const questions = view.questions || [];
    choices(byId("question-options"), questions, "question_id", q => q.question || q.text);
    const expanded = new Set([...byId("messages").querySelectorAll("li[data-message-id] details[open]")]
      .map(n => n.closest("li").dataset.messageId));
    const posts = [];
    for (const msg of view.messages) {
      const li = element("li");
      li.dataset.messageId = msg.message_id;
      const heading = element("strong", "PI");
      const reply = element("button", "Reply"); reply.type = "button";
      reply.onclick = () => { byId("target-controls").open = true; byId("reply-to").value = msg.message_id; byId("text").focus(); };
      li.append(heading, reply, element("p", msg.text));
      li.append(element("small", [msg.created_at, msg.status, msg.branch_id, msg.question_id].filter(Boolean).join(" · ")));
      if (msg.reply && msg.reply.text) li.append(element("p", "Main"), element("p", msg.reply.text));
      for (const feedback of msg.feedback || []) {
        const disposition = feedback.disposition;
        li.append(element("p", [feedback.branch_id || "Scientific Lead", disposition?.outcome || feedback.status,
                                disposition?.reason].filter(Boolean).join(" · ")));
        const source = element("a", "Open feedback evidence");
        source.href = basis({message_id: msg.message_id, feedback_id: feedback.feedback_id});
        source.target = "_blank"; source.rel = "noopener";
        li.append(source);
      }
      if (msg.result?.status === "successor_started") {
        const child = msg.result.run_id;
        if (view.runs.some(r => r.run_id === child)) {
          const link = element("a", "Open follow-up study");
          const url = new URL(location.href); url.searchParams.set("run_id", child);
          link.href = url.href; li.append(link);
        }
      }
      if (msg.rejected) li.append(element("p", String(msg.rejected)));
      const details = element("details"); details.open = expanded.has(msg.message_id); details.append(element("summary", "Message, references and outcomes"));
      const facts = {message_id: msg.message_id, goal_version: msg.goal_version};
      for (const k of ["reply_to", "clauses", "feedback", "materials", "result", "citations", "answer", "reply"])
        if (msg[k] != null) facts[k] = msg[k];
      details.append(element("pre", JSON.stringify(facts, null, 2)));
      li.append(details);
      posts.push({at: msg.created_at, li});
    }
    for (const q of questions) {
      const li = element("li"); li.append(element("strong", "Researcher"), element("p", q.question || q.text || q.question_id));
      li.append(element("small", [q.question_id, q.branch_id, q.status].filter(Boolean).join(" · ")));
      if (q.choices?.length) li.append(element("p", q.choices.join(" · ")));
      if ((q.status || "OPEN").toUpperCase() === "OPEN") {
        const answer = element("button", "Answer"); answer.type = "button";
        answer.onclick = () => { byId("target-controls").open = true; byId("question-id").value = q.question_id; byId("text").focus(); };
        li.append(answer);
      }
      posts.push({at: q.asked_at, li});
    }
    posts.sort((a, b) => (a.at || "").localeCompare(b.at || ""));
    byId("messages").replaceChildren(...posts.map(post => post.li));
  }

  async function refresh() {
    const id = selected, turn = ++serial;
    const view = await request(path + "?run_id=" + encodeURIComponent(id));
    if (id === selected && turn === serial) { shown = view; render(view); }
    return view;
  }

  function listen(id, cursor) {
    if (events) events.close();
    const key = storageKey("cursor", id);
    const after = sessionStorage.getItem(key) || String(cursor || 0);
    events = new EventSource(path + "/events?run_id=" + encodeURIComponent(id) + "&after=" + encodeURIComponent(after));
    events.onopen = () => { if (id === selected) byId("connection").textContent = "Connected"; };
    events.onerror = () => { if (id === selected) byId("connection").textContent = "Reconnecting…"; };
    events.onmessage = e => {
      if (id !== selected) return;
      const row = JSON.parse(e.data);
      sessionStorage.setItem(key, e.lastEventId);
      const list = byId("timeline");
      const line = element("li", [row.at, row.entity, row.subject, row.status || row.kind].filter(Boolean).join(" · "));
      line.dataset.eventId = e.lastEventId;
      list.prepend(line);
      if (list.children.length > 50) list.lastChild.remove();
      if (!refreshTimer) refreshTimer = setTimeout(() => {
        refreshTimer = null;
        refresh().catch(error => { byId("status").textContent = error.message; });
      }, 200);
    };
  }

  async function switchRun(id) {
    selected = id; shown = null; lastView = null; ++serial;
    if (events) { events.close(); events = null; }
    clearTimeout(refreshTimer); refreshTimer = null;
    byId("send").disabled = true;
    byId("messages").replaceChildren(); byId("timeline").replaceChildren();
    byId("parent-materials").replaceChildren();
    byId("receipt").textContent = "";
    restoreComposer(id);
    const url = new URL(location.href); url.searchParams.set("run_id", id); history.replaceState({}, "", url);
    const view = await refresh();
    if (selected === id) { listen(id, view.cursor); byId("send").disabled = false; }
  }
  byId("run-select").onchange = () => switchRun(byId("run-select").value)
    .catch(error => { byId("status").textContent = error.message; });
  byId("follow").onchange = () => { byId("parent-materials").hidden = !byId("follow").checked; };
  byId("form").onsubmit = async e => {
    e.preventDefault();
    if (!shown || !byId("text").value.trim()) return;
    const rid = selected, key = storageKey("pending", rid);
    const body = {kind: "feedback", run_id: rid, text: byId("text").value,
                  conversation_id: shown.conversation_id, goal_version: shown.goal_version};
    const previous = JSON.parse(sessionStorage.getItem(key) || "null");
    for (const [field, id] of [["reply_to","reply-to"], ["branch_id","branch-id"], ["question_id","question-id"]])
      if (byId(id).value.trim()) body[field] = byId(id).value.trim();
    const upload = byId("attachment").files[0];
    const original = byId("material-ref").value.trim();
    const follow = !byId("follow-controls").hidden && byId("follow").checked;
    if (follow) body.successor = {parent_material_refs: [...byId("parent-materials").querySelectorAll("input:checked")].map(el => el.value)};
    byId("send").disabled = true;
    try {
      const attached = original ? [{artifact_ref: original}] : [];
      if (upload) {
        if (upload.size > 128 * 1024) throw new Error("Choose a UTF-8 text file up to 128 KiB.");
        const bytes = await upload.arrayBuffer();
        attached.push({name: upload.name, text: new TextDecoder("utf-8", {fatal: true}).decode(bytes)});
      } else if (previous && previous.text === body.text && previous.attachments) {
        attached.push(...previous.attachments.filter(item => item.name));
      }
      if (attached.length) body.attachments = attached;
      if (follow && attached.length) body.successor.include_message_attachments = true;
      let draft = {...body, client_message_id: crypto.randomUUID()};
      if (previous) {
        const {client_message_id, ...saved} = previous;
        // Retrying a lost POST retains its original Goal version.
        if (JSON.stringify({...body, goal_version: saved.goal_version}) === JSON.stringify(saved)) draft = previous;
      }
      sessionStorage.setItem(key, JSON.stringify(draft));
      const result = await request(path + "/messages", draft);
      sessionStorage.removeItem(key);
      if (selected === rid) {
        if (byId("text").value === body.text) clearComposer();
        byId("receipt").textContent = [result.message_id, result.status].filter(Boolean).join(" · ");
        await refresh();
      }
    } catch (err) { if (selected === rid) byId("receipt").textContent = err.message; }
    finally { if (selected === rid) byId("send").disabled = false; }
  };
  await switchRun(selected);
}
openProject().catch((error) => { byId("status").textContent = error.message; });
