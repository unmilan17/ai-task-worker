// Dashboard client: start runs, stream events over a WebSocket, render the
// timeline / live view / plan / memory, and answer approvals & questions.
"use strict";

const $ = (id) => document.getElementById(id);
const esc = (s) => String(s ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));

let current = null;   // { runId, ws, pendingCalls: Map(call_id -> element), answered: Set, started }

// ------------------------------------------------------------------ boot
async function boot() {
  const cfg = await (await fetch("/api/config")).json();
  $("model").textContent = `${cfg.provider} · ${cfg.model}`;
  $("systems").innerHTML = Object.entries(cfg.systems).map(([k, v]) => `<a href="${v}" target="_blank" rel="noopener">${k} ↗</a>`).join("");
  $("keywarn").classList.toggle("hidden", cfg.api_key_present);
  if (cfg.api_key_message) $("keywarn").textContent = `${cfg.api_key_message} Then restart the server.`;
  if (cfg.chaos) $("chaos").value = cfg.chaos;
  $("fault").insertAdjacentHTML("beforeend", (cfg.all_switches || []).map((s) => `<option value="${s}">${s}</option>`).join(""));
  if (cfg.switches && cfg.switches.length) $("fault").value = cfg.switches[0];
  $("model").textContent = `${cfg.provider} · ${cfg.model} · ${cfg.profile || "northwind"}`;
  $("fault").onchange = async () => {
    for (const s of cfg.all_switches || []) await fetch(`/api/sandbox/switch/${s}?on=false`, { method: "POST" });
    if ($("fault").value) await fetch(`/api/sandbox/switch/${$("fault").value}?on=true`, { method: "POST" });
  };
  $("samples").innerHTML = cfg.samples.map((s, i) => `<button data-i="${i}" title="${esc(s.task)}">${esc(s.label)}</button>`).join("");
  $("samples").onclick = (e) => { const b = e.target.closest("button"); if (b) $("task").value = cfg.samples[b.dataset.i].task; };
  $("run").onclick = startRun;
  $("chaos").onchange = () => fetch(`/api/sandbox/chaos/${$("chaos").value}`, { method: "POST" });
  $("reset").onclick = async () => { await fetch("/api/sandbox/reset", { method: "POST" }); $("fault").value = ""; $("reset").textContent = "Data reset ✓"; setTimeout(() => ($("reset").textContent = "Reset data"), 1500); };
  $("cancel").onclick = () => current && fetch(`/api/runs/${current.runId}/cancel`, { method: "POST" });
  $("history").onclick = (e) => { const li = e.target.closest("li"); if (li) openRun(li.dataset.id); };
  loadHistory();
}

async function loadHistory() {
  const runs = await (await fetch("/api/runs")).json();
  $("history").innerHTML = runs.map((r) => `<li data-id="${r.run_id}"><span class="pill ${r.status}">${esc(r.status)}</span><span class="t" title="${esc(r.task)}">${esc(r.task)}</span></li>`).join("") || `<li class="muted">No runs yet.</li>`;
}

// ------------------------------------------------------------------ runs
async function startRun() {
  const task = $("task").value.trim();
  if (!task) return;
  $("run").disabled = true;
  const res = await fetch("/api/runs", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ task }) });
  $("run").disabled = false;
  if (!res.ok) { alert((await res.json()).detail || "Could not start run"); return; }
  const { run_id } = await res.json();
  resetView(run_id, task);
  const ws = new WebSocket(`${location.protocol === "https:" ? "wss" : "ws"}://${location.host}/ws/runs/${run_id}`);
  current.ws = ws;
  ws.onmessage = (m) => handle(JSON.parse(m.data), true);
  ws.onclose = () => loadHistory();
  loadHistory();
}

async function openRun(runId) {
  if (current?.ws) current.ws.close();
  const data = await (await fetch(`/api/runs/${runId}`)).json();
  const started = data.events.find((e) => e.type === "run_started");
  resetView(runId, started?.data.task || "");
  data.events.forEach((e) => handle(e, false));
  current.sessionOpen = !!data.session_open;
  if (data.status === "running" || data.session_open) {
    const ws = new WebSocket(`${location.protocol === "https:" ? "wss" : "ws"}://${location.host}/ws/runs/${runId}`);
    current.ws = ws;
    let skip = data.events.length;   // the socket replays history first
    ws.onmessage = (m) => { if (skip-- > 0) return; handle(JSON.parse(m.data), true); };
  }
}

function resetView(runId, task) {
  current = { runId, ws: null, calls: new Map(), answered: new Set(), requests: new Map(), started: Date.now(), steps: 0, cost: 0 };
  $("empty").classList.add("hidden");
  $("runview").classList.remove("hidden");
  $("runtask").textContent = task;
  setStatus("running");
  for (const id of ["timeline", "plan", "facts", "actions"]) $(id).innerHTML = "";
  $("human").classList.add("hidden"); $("result").classList.add("hidden");
  $("shot").removeAttribute("src"); $("noshot").classList.remove("hidden"); $("liveurl").textContent = "";
  document.querySelectorAll("#phases li").forEach((li) => (li.className = ""));
  $("cancel").classList.remove("hidden");
  $("metrics").textContent = "";
}

function setStatus(s) { const p = $("status"); p.className = `pill ${s}`; p.textContent = s.replace("_", " "); }

function setPhase(phase) {
  const order = ["understand", "execute", "verify", "report"];
  const idx = order.indexOf(phase);
  document.querySelectorAll("#phases li").forEach((li) => {
    const i = order.indexOf(li.dataset.p);
    li.className = i < idx ? "done" : i === idx ? "active" : "";
  });
}

function shotUrl(path) {
  if (!path) return null;
  const parts = path.replace(/\\/g, "/").split("/");
  const i = parts.lastIndexOf("screens");
  return i > 0 ? `/runs/${parts[i - 1]}/screens/${parts.slice(i + 1).join("/")}` : null;
}

function add(html, cls) {
  const div = document.createElement("div");
  div.className = `ev ${cls || ""}`;
  div.innerHTML = html;
  const tl = $("timeline");
  const stick = tl.scrollTop + tl.clientHeight >= tl.scrollHeight - 40;
  tl.appendChild(div);
  if (stick) tl.scrollTop = tl.scrollHeight;
  return div;
}

// ------------------------------------------------------------------ event handling
function handle(ev, live) {
  const d = ev.data || {};
  switch (ev.type) {
    case "phase": setPhase(d.phase); add(`${esc(d.phase)}${d.reason ? " · " + esc(d.reason) : ""}`, "phase"); break;
    case "task_spec": {
      const s = d.spec;
      add(`<b>Goal:</b> ${esc(s.goal)}<br><b>Success criteria</b><ul>${s.success_criteria.map((c) => `<li>${esc(c)}</li>`).join("")}</ul>`, "card");
      break;
    }
    case "plan_updated":
      $("plan").innerHTML = d.plan.map((p) => `<li>${esc(p.title)}<span class="st ${p.status}">${esc(p.status.replace("_", " "))}</span>${p.note ? `<div class="muted small">${esc(p.note)}</div>` : ""}</li>`).join("");
      break;
    case "llm_turn":
      if (d.thoughts) add(`💭 ${esc(d.thoughts.length > 700 ? d.thoughts.slice(0, 700) + "…" : d.thoughts)}`, "thought");
      else if (d.text) add(`💬 ${esc(d.text)}`, "thought");
      current.cost = d.total_cost_usd ?? current.cost;
      updateMetrics(ev);
      break;
    case "tool_call": {
      if (d.attempt > 1) break;   // retries are shown as their own rows
      const el = add(`<details><summary><span class="name">${esc(d.name)}</span> <span class="args">${esc(JSON.stringify(d.args)).slice(0, 220)}</span> <span class="badge warn">running…</span></summary></details>`, "tool");
      current.calls.set(d.call_id, el);
      current.steps = Math.max(current.steps, ev.step || 0);
      updateMetrics(ev);
      break;
    }
    case "tool_result": {
      const el = current.calls.get(d.call_id) || add(`<details><summary><span class="name">${esc(d.name)}</span></summary></details>`, "tool");
      el.classList.add(d.ok ? "ok" : "fail");
      const badge = el.querySelector(".badge");
      const label = d.ok ? (d.attempts > 1 ? `ok after ${d.attempts} tries` : "ok") : esc(d.error_kind || "error");
      if (badge) { badge.className = `badge ${d.ok ? "ok" : "fail"}`; badge.textContent = label; }
      const det = el.querySelector("details");
      det.insertAdjacentHTML("beforeend", `<pre>${esc(d.output)}${d.hint && !d.ok ? "\n\nHint: " + esc(d.hint) : ""}</pre>`);
      break;
    }
    case "retry": add(`↻ ${esc(d.name)} hit a transient error - automatic retry ${d.attempt} in ${d.delay_s}s`, "retry"); break;
    case "reflection": add(`🧭 ${esc(d.note)}`, "reflect"); break;
    case "policy_block": add(`⛔ Policy blocked: ${esc(d.action)} - ${esc(d.reason)}`, "block"); break;
    case "screenshot": {
      const url = shotUrl(d.path);
      if (url) { $("shot").src = url; $("noshot").classList.add("hidden"); $("liveurl").textContent = d.url || ""; }
      break;
    }
    case "memory_fact": {
      const f = d.fact;
      $("facts").insertAdjacentHTML("beforeend", `<li><code>${esc(f.key)}</code> = <b>${esc(f.value)}</b><div class="muted small">${esc(f.source)}</div></li>`);
      add(`📌 Remembered <code>${esc(f.key)}</code> = <b>${esc(f.value)}</b>`, "fact");
      break;
    }
    case "side_effect":
      $("actions").insertAdjacentHTML("beforeend", `<li>✎ ${esc(d.description)}${d.fields ? `<div class="muted small">${esc(JSON.stringify(d.fields))}</div>` : ""}</li>`);
      add(`✎ <b>Changed data:</b> ${esc(d.description)}`, "effect");
      break;
    case "human_request": current.requests.set(d.request.id, d.request); add(`🙋 ${d.request.kind === "approval" ? "Approval requested" : "Question for you"}: ${esc(d.request.question)}`, "human"); if (live || !current.answered.has(d.request.id)) showHuman(d.request); break;
    case "human_response": {
      current.answered.add(d.request_id);
      const r = d.response;
      add(`🙋 Answer: ${r.timed_out ? "no answer (timed out)" : r.approved === true ? "approved" : r.approved === false ? "rejected" : ""} ${esc(r.answer || "")}`, "human");
      if ($("human").dataset.id === d.request_id) $("human").classList.add("hidden");
      break;
    }
    case "claim": add(`🏁 Worker reports <b>${esc(d.claim.status)}</b>: ${esc(d.claim.summary)}`, "card"); break;
    case "verification": {
      const v = d.verdict;
      add(`🔎 Verification (${esc(v.method)}): <span class="badge ${v.passed ? "ok" : "fail"}">${v.passed ? "PASSED" : "FAILED"}</span> ${esc(v.summary)}
        ${(v.criteria || []).length ? `<ul>${v.criteria.map((c) => `<li class="${c.met ? "yes" : "no"}">${c.met ? "✓" : "✗"} <span style="color:var(--ink);font-weight:400">${esc(c.criterion)} <span class="muted">- ${esc(c.evidence)}</span></span></li>`).join("")}</ul>` : ""}`, "card");
      break;
    }
    case "rate_limit_wait": {
      // Collapse consecutive waits into one line.
      const last = $("timeline").lastElementChild;
      const html = `⏳ Waiting ${esc(d.seconds)}s for the free-tier rate limit`;
      if (last && last.classList.contains("wait")) last.innerHTML = html; else add(html, "retry wait");
      break;
    }
    case "write_review":
      if (d.result !== "ok") add(`🛡 Pre-write review (${esc(d.result)}): ${esc(d.detail)}`, d.result === "unsourced" ? "block" : "reflect");
      break;
    case "circuit_open": add(`⚡ Circuit breaker opened for ${esc(d.host)}: the system looks down, no more calls to it`, "block"); break;
    case "turn_started":
      setStatus("running"); setPhase("understand");
      $("result").classList.add("hidden"); $("cancel").classList.remove("hidden");
      add(`Follow-up (turn ${esc(d.turn)}): ${esc(d.task)}`, "phase");
      break;
    case "session_closed": current.sessionOpen = false; $("followup")?.remove(); break;
    case "error": add(`⚠ ${esc(d.message)}`, "error"); break;
    case "run_finished": current.sessionOpen = !!d.can_follow_up; finish(d); break;
  }
}

function updateMetrics(ev) {
  const secs = ev.ts && current.t0 ? Math.round(ev.ts - current.t0) : null;
  if (!current.t0 && ev.ts) current.t0 = ev.ts;
  $("metrics").textContent = `${current.steps} steps · $${(current.cost || 0).toFixed(3)}${secs !== null ? ` · ${secs}s` : ""}`;
}

function showHuman(req) {
  if (current.answered.has(req.id)) return;
  const box = $("human");
  box.dataset.id = req.id;
  const details = Object.entries(req.details || {}).filter(([, v]) => v !== "" && v != null)
    .map(([k, v]) => `<tr><td class="muted">${esc(k.replace("field:", ""))}</td><td>${esc(typeof v === "object" ? JSON.stringify(v) : v)}</td></tr>`).join("");
  if (req.kind === "approval") {
    box.innerHTML = `<h3>⚠ Approval needed</h3><div>${esc(req.question)}</div><table>${details}</table>
      <div class="row"><input type="text" id="hcomment" placeholder="Optional comment"><button class="ok" id="happrove">Approve</button><button class="bad" id="hreject">Reject</button></div>`;
    $("happrove").onclick = () => respond(req.id, { approved: true, answer: $("hcomment").value });
    $("hreject").onclick = () => respond(req.id, { approved: false, answer: $("hcomment").value });
  } else {
    box.innerHTML = `<h3>❓ The worker needs your input</h3><div>${esc(req.question)}</div>${details ? `<table>${details}</table>` : ""}
      <div class="row">${(req.options || []).map((o, i) => `<button data-o="${i}">${esc(o)}</button>`).join("")}</div>
      <div class="row"><input type="text" id="hanswer" placeholder="Type an answer"><button class="ok" id="hsend">Send</button></div>`;
    box.querySelectorAll("button[data-o]").forEach((b) => (b.onclick = () => respond(req.id, { answer: req.options[b.dataset.o] })));
    $("hsend").onclick = () => respond(req.id, { answer: $("hanswer").value });
  }
  box.classList.remove("hidden");
  box.scrollIntoView({ behavior: "smooth", block: "nearest" });
}

async function respond(id, body) {
  await fetch(`/api/runs/${current.runId}/respond`, { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ request_id: id, ...body }) });
  current.answered.add(id);
  $("human").classList.add("hidden");
}

async function finish(d) {
  setStatus(d.status);
  setPhase("report");
  document.querySelectorAll("#phases li").forEach((li) => (li.className = "done"));
  $("cancel").classList.add("hidden");
  $("human").classList.add("hidden");
  const data = await (await fetch(`/api/runs/${current.runId}`)).json();
  const r = data.report;
  const box = $("result");
  if (!r) { box.innerHTML = `<h3>${esc(d.status)}</h3><p>${esc(d.reason || "")}</p>`; box.classList.remove("hidden"); return; }
  const last = (r.verification || []).slice(-1)[0];
  box.innerHTML = `<h3>Result <span class="pill ${r.status}">${esc(r.status.replace("_", " "))}</span>${r.verified ? ' <span class="pill succeeded">independently verified</span>' : ""}</h3>
    <div class="summary">${esc(r.summary)}</div>
    ${r.notes_for_user?.length ? `<ul>${r.notes_for_user.map((n) => `<li>${esc(n)}</li>`).join("")}</ul>` : ""}
    ${last?.criteria?.length ? `<table><tr><th>Success criterion</th><th>Met</th><th>Evidence observed by verifier</th></tr>${last.criteria.map((c) => `<tr><td>${esc(c.criterion)}</td><td class="${c.met ? "yes" : "no"}">${c.met ? "yes" : "no"}</td><td>${esc(c.evidence)}</td></tr>`).join("")}</table>` : ""}
    <p class="muted small">${r.metrics.tool_steps} steps · ${r.metrics.llm_calls} LLM calls · ${r.metrics.duration_s}s · ~$${r.metrics.cost_usd} · retries ${r.reliability.auto_retries} ·
      <a href="/runs/${r.run_id}/report.html" target="_blank" rel="noopener">Open full evidence report ↗</a>${r.video ? ` · <a href="/runs/${r.run_id}/${r.video}" target="_blank" rel="noopener">session video ↗</a>` : ""}</p>`;
  if (current.sessionOpen) {
    box.insertAdjacentHTML("beforeend", `<div class="human" id="followup" style="margin-top:10px;padding:10px">
      <b>Follow up</b> <span class="muted small">same session: the worker keeps its memory and browser</span>
      <div class="row"><input type="text" id="followupText" placeholder="e.g. What was the due date again? / Do the same for another supplier">
      <button class="ok" id="followupSend">Send</button></div></div>`);
    $("followupSend").onclick = sendFollowUp;
    $("followupText").onkeydown = (e) => { if (e.key === "Enter") sendFollowUp(); };
  }
  box.classList.remove("hidden");
  $("metrics").textContent = `${r.metrics.tool_steps} steps · $${r.metrics.cost_usd} · ${r.metrics.duration_s}s`;
  loadHistory();
}

async function sendFollowUp() {
  const message = $("followupText").value.trim();
  if (!message) return;
  const res = await fetch(`/api/runs/${current.runId}/followup`, { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ message }) });
  if (!res.ok) { alert((await res.json()).detail || "Could not send"); return; }
  $("followup")?.remove();
}

boot();
