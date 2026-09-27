/* Council - client */

const $ = (s) => document.querySelector(s);
const el = (tag, cls, txt) => {
  const n = document.createElement(tag);
  if (cls) n.className = cls;
  if (txt !== undefined) n.textContent = txt;
  return n;
};

const state = {
  conversationId: null,
  mode: "moa",
  running: false,
  stream: null,        // live EventSource
  lastSeq: 0,          // for reconnect
  turnId: null,
  agents: new Map(),   // agentKey -> {bodyEl, statusEl, buffer}
};

/* ---------------- tiny markdown renderer ----------------
   Escapes first, then formats. Deliberately minimal - no library, no innerHTML
   of untrusted text. Handles what the models actually emit: headings, lists,
   fences, bold, inline code. */
function md(src) {
  const esc = (s) => s.replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;");
  const fences = [];
  let t = esc(src).replace(/```(\w*)\n([\s\S]*?)```/g, (_, lang, code) => {
    fences.push(`<pre><code>${code.replace(/\n$/, "")}</code></pre>`);
    return `\u0000F${fences.length - 1}\u0000`;
  });

  const lines = t.split("\n");
  const out = [];
  let list = null;

  const closeList = () => { if (list) { out.push(`</${list}>`); list = null; } };

  for (let raw of lines) {
    const line = raw.trimEnd();
    let m;
    if ((m = line.match(/^(#{1,6})\s+(.*)$/))) {
      closeList();
      const lvl = Math.min(m[1].length, 3);
      out.push(`<h${lvl}>${inline(m[2])}</h${lvl}>`);
    } else if (/^\s*[-*]\s+/.test(line)) {
      if (list !== "ul") { closeList(); out.push("<ul>"); list = "ul"; }
      out.push(`<li>${inline(line.replace(/^\s*[-*]\s+/, ""))}</li>`);
    } else if (/^\s*\d+[.)]\s+/.test(line)) {
      if (list !== "ol") { closeList(); out.push("<ol>"); list = "ol"; }
      out.push(`<li>${inline(line.replace(/^\s*\d+[.)]\s+/, ""))}</li>`);
    } else if (/^\s*(---+|___+)\s*$/.test(line)) {
      closeList(); out.push("<hr>");
    } else if (/^&gt;\s?/.test(line)) {
      closeList(); out.push(`<blockquote>${inline(line.replace(/^&gt;\s?/, ""))}</blockquote>`);
    } else if (line.trim() === "") {
      closeList();
    } else {
      closeList();
      out.push(`<p>${inline(line)}</p>`);
    }
  }
  closeList();

  return out.join("\n").replace(/\u0000F(\d+)\u0000/g, (_, i) => fences[+i]);

  function inline(s) {
    return s
      .replace(/`([^`]+)`/g, "<code>$1</code>")
      .replace(/\*\*([^*]+)\*\*/g, "<strong>$1</strong>")
      .replace(/(^|[^*])\*([^*\n]+)\*/g, "$1<em>$2</em>");
  }
}

function toast(msg, ms = 3200) {
  const t = $("#toast");
  t.textContent = msg;
  t.classList.remove("hidden");
  clearTimeout(t._h);
  t._h = setTimeout(() => t.classList.add("hidden"), ms);
}

async function api(path, opts = {}) {
  const res = await fetch(path, {
    headers: { "Content-Type": "application/json" },
    credentials: "same-origin",
    ...opts,
  });
  if (res.status === 401) { showLogin(); throw new Error("not authenticated"); }
  if (!res.ok) {
    let msg = `HTTP ${res.status}`;
    try { msg = (await res.json()).error || msg; } catch {}
    throw new Error(msg);
  }
  return res.status === 204 ? null : res.json();
}

/* ---------------- auth ---------------- */
function showLogin() { $("#login").classList.remove("hidden"); $("#app").classList.add("hidden"); }
function showApp() { $("#login").classList.add("hidden"); $("#app").classList.remove("hidden"); }

$("#login-form").addEventListener("submit", async (e) => {
  e.preventDefault();
  $("#login-error").textContent = "";
  try {
    await api("/api/login", {
      method: "POST",
      body: JSON.stringify({ password: $("#login-password").value }),
    });
    showApp();
    await boot();
  } catch (err) {
    $("#login-error").textContent = "Wrong password.";
  }
});

/* ---------------- conversations ---------------- */
async function loadConversations() {
  const convs = await api("/api/conversations");
  const list = $("#conv-list");
  list.innerHTML = "";
  for (const c of convs) {
    const item = el("div", "conv-item" + (c.id === state.conversationId ? " active" : ""));
    item.appendChild(el("span", "", c.title));
    const del = el("span", "del", "×");
    del.title = "Delete";
    del.addEventListener("click", async (e) => {
      e.stopPropagation();
      if (!confirm("Delete this conversation?")) return;
      await api(`/api/conversations/${c.id}`, { method: "DELETE" });
      if (state.conversationId === c.id) { state.conversationId = null; await newConversation(); }
      await loadConversations();
    });
    item.appendChild(del);
    item.addEventListener("click", () => openConversation(c.id));
    list.appendChild(item);
  }
}

async function newConversation() {
  const { id } = await api("/api/conversations", { method: "POST" });
  await openConversation(id);
  await loadConversations();
}

async function openConversation(cid) {
  state.conversationId = cid;
  closeSidebar();
  const conv = await api(`/api/conversations/${cid}`);
  $("#conv-title").textContent = conv.title;
  const thread = $("#thread");
  thread.innerHTML = "";

  if (!conv.turns.length) {
    thread.appendChild(buildEmpty());
  } else {
    for (const t of conv.turns) await renderPastTurn(t);
  }
  await loadConversations();
  scrollDown();
}

function buildEmpty() {
  const d = el("div", "empty");
  d.innerHTML = `<h2>Ask the council</h2>
    <p>Several models answer in parallel, then argue about it. You get one answer plus the questions you hadn't thought to ask.</p>
    <div class="mode-explain">
      <div><b>Quick</b><span>4 models in parallel, merged. ~20s</span></div>
      <div><b>Debate</b><span>Drafter vs Critic, 3 rounds, then a verdict. ~90s</span></div>
      <div><b>Full</b><span>Quick, then debated. Best for planning a project. ~2min</span></div>
    </div>`;
  return d;
}

/* ---------------- rendering ---------------- */
function buildTurnShell(prompt, mode) {
  const turn = el("div", "turn");
  const user = el("div", "user-msg");
  user.appendChild(el("span", "tag", mode === "moa" ? "Quick" : mode === "debate" ? "Debate" : "Full"));
  user.appendChild(document.createTextNode(prompt));
  turn.appendChild(user);
  turn.appendChild(el("div", "agents"));
  return turn;
}

/* Find (or create) a seat's panel.
   `isStart` must be true ONLY for agent_start. A debate seat speaks several
   times across rounds and gets a fresh panel each round, but the chunk/done/
   error events that follow carry no round number -- so looking a panel up by
   round meant they never matched and silently rendered a second, unlabelled
   panel per seat. The live one is tracked per key instead. */
function agentPanel(container, key, info, isStart) {
  if (!container._current) container._current = {};
  if (!isStart) {
    const live = container._current[key];
    if (live) return live;
  }

  const panel = el("div", "agent");
  panel.dataset.agent = key;
  panel.dataset.round = info.round ?? "";

  const head = el("div", "agent-head");
  const sw = el("span", "swatch");
  sw.style.background = info.color || "#8b8b9e";
  head.appendChild(sw);
  head.appendChild(el("span", "chev", "▶"));
  head.appendChild(el("span", "agent-name", info.label || key));
  const modelBit = (info.model || "").split("/").pop();
  head.appendChild(el("span", "agent-model",
    info.provider ? `${info.provider} · ${modelBit}` : modelBit));
  if (info.round) head.appendChild(el("span", "agent-round", `round ${info.round}`));
  const status = el("span", "agent-status running", "thinking...");
  head.appendChild(status);

  const body = el("div", "agent-body md streaming");
  head.addEventListener("click", () => {
    panel.classList.toggle("open");
    body.classList.remove("streaming");
  });

  panel.appendChild(head);
  panel.appendChild(body);
  container.appendChild(panel);

  panel._parts = { panel, body, status, buffer: "" };
  container._current[key] = panel._parts;
  return panel._parts;
}

async function renderPastTurn(t) {
  const thread = $("#thread");
  const turn = buildTurnShell(t.user_prompt, t.mode);
  thread.appendChild(turn);
  const agentsBox = turn.querySelector(".agents");

  // Rebuild the agent panels from the stored event log, collapsed.
  try {
    const events = await api(`/api/turns/${t.id}/events?after=0`);
    for (const ev of events) {
      if (ev.type === "agent_start") {
        const p = agentPanel(agentsBox, ev.agent, ev, true);
        p.body.classList.remove("streaming");
      } else if (ev.type === "agent_done") {
        const p = agentPanel(agentsBox, ev.agent, {});
        p.body.innerHTML = md(ev.text || "");
        p.status.className = "agent-status done";
        p.status.textContent = "done";
        p.body.classList.remove("streaming");
      } else if (ev.type === "agent_error") {
        const p = agentPanel(agentsBox, ev.agent, {});
        p.status.className = "agent-status error";
        p.status.textContent = "failed";
        p.body.textContent = ev.error || "failed";
        p.body.classList.remove("streaming");
      } else if (ev.type === "agent_skipped") {
        const p = agentPanel(agentsBox, ev.agent, ev, true);
        p.panel.classList.add("skipped");
        p.status.className = "agent-status skipped";
        p.status.textContent = "no key";
        p.body.textContent = `Skipped - no API key for ${ev.provider}.`;
        p.body.classList.remove("streaming");
      }
    }
  } catch {}

  if (t.status === "done" && t.final_answer) {
    turn.appendChild(finalBlock(t.final_answer));
  } else if (t.status === "error") {
    turn.appendChild(el("div", "failed", t.error || "This run failed."));
  } else if (t.status === "running") {
    // A run still going from another device or a previous page load - attach to it.
    attachStream(t.id, turn);
  }
}

function finalBlock(text) {
  const box = el("div", "final");
  const head = el("div", "final-head");
  head.appendChild(el("span", "", "Answer"));
  const copy = el("button", "final-copy", "Copy");
  copy.addEventListener("click", () => {
    navigator.clipboard.writeText(text).then(
      () => toast("Copied"),
      () => toast("Copy failed")
    );
  });
  head.appendChild(copy);
  box.appendChild(head);
  const body = el("div", "md");
  body.innerHTML = md(text);
  box.appendChild(body);
  return box;
}

function setStage(text) {
  if (!text) { $("#stage-banner").classList.add("hidden"); return; }
  $("#stage-text").textContent = text;
  $("#stage-banner").classList.remove("hidden");
}

function scrollDown() {
  const t = $("#thread");
  t.scrollTop = t.scrollHeight;
}

/* ---------------- streaming ----------------
   Reconnect matters here: a phone locks its screen, switches from Wi-Fi to
   cellular, or backgrounds the tab, and the EventSource dies mid-debate. We
   track the last seq we saw and reopen from there, so nothing is lost and
   nothing is replayed twice. */
function attachStream(turnId, turnEl) {
  state.turnId = turnId;
  state.lastSeq = 0;
  state.running = true;
  $("#send").disabled = true;

  const agentsBox = turnEl.querySelector(".agents");
  let retries = 0;

  const open = () => {
    if (state.stream) state.stream.close();
    const es = new EventSource(`/api/turns/${turnId}/stream?after=${state.lastSeq}`);
    state.stream = es;

    es.onmessage = (e) => {
      retries = 0;
      let ev;
      try { ev = JSON.parse(e.data); } catch { return; }
      if (ev.seq) state.lastSeq = Math.max(state.lastSeq, ev.seq);
      handleEvent(ev, agentsBox, turnEl);
      if (ev.type === "done" || ev.type === "failed" || ev.type === "stream_end") {
        es.close();
        state.stream = null;
        state.running = false;
        $("#send").disabled = false;
        setStage(null);
      }
    };

    es.onerror = () => {
      es.close();
      state.stream = null;
      if (!state.running) return;
      retries += 1;
      if (retries > 12) {
        setStage(null);
        state.running = false;
        $("#send").disabled = false;
        toast("Lost connection to the run. Reload to see the result.");
        return;
      }
      setStage("reconnecting...");
      setTimeout(open, Math.min(1000 * retries, 6000));
    };
  };

  open();
}

function handleEvent(ev, agentsBox, turnEl) {
  switch (ev.type) {
    case "stage":
      setStage(ev.label);
      break;

    case "agent_start": {
      const p = agentPanel(agentsBox, ev.agent, ev, true);
      p.buffer = "";
      p.thinkBuf = "";
      if (p.thinking) { p.thinking.remove(); p.thinking = null; }
      p.status.className = "agent-status running";
      p.status.textContent = "thinking...";
      scrollDown();
      break;
    }

    case "agent_reasoning": {
      // The model's private thinking. Shown dimmed above the answer, and
      // replaced by the answer once it arrives.
      const p = agentPanel(agentsBox, ev.agent, {});
      if (!p.thinking) {
        p.thinking = el("div", "agent-thinking");
        p.body.parentNode.insertBefore(p.thinking, p.body);
      }
      p.thinkBuf = (p.thinkBuf || "") + ev.text;
      p.thinking.textContent = "thinking: " + p.thinkBuf.slice(-400);
      p.status.textContent = "reasoning...";
      break;
    }

    case "agent_chunk": {
      const p = agentPanel(agentsBox, ev.agent, {});
      if (p.thinking) { p.thinking.remove(); p.thinking = null; }
      p.buffer += ev.text;
      // Plain text while streaming (cheap), markdown once complete.
      p.body.textContent = p.buffer;
      p.body.scrollTop = p.body.scrollHeight;
      break;
    }

    case "agent_done": {
      const p = agentPanel(agentsBox, ev.agent, {});
      p.buffer = ev.text || p.buffer;
      p.body.innerHTML = md(p.buffer);
      p.status.className = "agent-status done";
      p.status.textContent = "done";
      p.body.classList.remove("streaming");
      break;
    }

    case "agent_error": {
      const p = agentPanel(agentsBox, ev.agent, {});
      p.status.className = "agent-status error";
      p.status.textContent = "failed";
      p.body.textContent = ev.error || "failed";
      p.body.classList.remove("streaming");
      break;
    }

    case "agent_skipped": {
      // Not a failure: this seat's provider simply has no key configured.
      const p = agentPanel(agentsBox, ev.agent, ev, true);
      p.panel.classList.add("skipped");
      p.status.className = "agent-status skipped";
      p.status.textContent = "no key";
      p.body.textContent =
        `Skipped - no API key for ${ev.provider}. Add one to .env to bring this seat in.`;
      p.body.classList.remove("streaming");
      break;
    }

    case "done":
      turnEl.appendChild(finalBlock(ev.final_answer));
      setStage(null);
      scrollDown();
      loadConversations();
      break;

    case "failed":
      turnEl.appendChild(el("div", "failed", ev.error || "This run failed."));
      setStage(null);
      break;
  }
}

/* ---------------- sending ---------------- */
async function send() {
  const box = $("#prompt");
  const prompt = box.value.trim();
  if (!prompt || state.running) return;
  if (!state.conversationId) await newConversation();

  box.value = "";
  box.style.height = "auto";

  const empty = $("#thread").querySelector(".empty");
  if (empty) empty.remove();

  const turn = buildTurnShell(prompt, state.mode);
  $("#thread").appendChild(turn);
  scrollDown();

  try {
    const { turn_id } = await api(`/api/conversations/${state.conversationId}/turns`, {
      method: "POST",
      body: JSON.stringify({ prompt, mode: state.mode }),
    });
    attachStream(turn_id, turn);
  } catch (err) {
    turn.appendChild(el("div", "failed", err.message));
    state.running = false;
    $("#send").disabled = false;
  }
}

/* ---------------- wiring ---------------- */
$("#send").addEventListener("click", send);

$("#prompt").addEventListener("keydown", (e) => {
  // Enter sends on desktop; on a phone Enter should insert a newline instead.
  const isTouch = window.matchMedia("(max-width: 768px)").matches;
  if (e.key === "Enter" && !e.shiftKey && !isTouch) {
    e.preventDefault();
    send();
  }
});

$("#prompt").addEventListener("input", (e) => {
  e.target.style.height = "auto";
  e.target.style.height = Math.min(e.target.scrollHeight, 190) + "px";
});

$("#modes").addEventListener("click", (e) => {
  const btn = e.target.closest(".mode");
  if (!btn) return;
  document.querySelectorAll(".mode").forEach((b) => b.classList.remove("active"));
  btn.classList.add("active");
  state.mode = btn.dataset.mode;
});

$("#new-chat").addEventListener("click", newConversation);

function openSidebar() { $("#sidebar").classList.add("open"); $("#scrim").classList.add("open"); }
function closeSidebar() { $("#sidebar").classList.remove("open"); $("#scrim").classList.remove("open"); }
$("#menu-btn").addEventListener("click", openSidebar);
$("#scrim").addEventListener("click", closeSidebar);

$("#check-models").addEventListener("click", async () => {
  toast("Checking models against OpenRouter...");
  try {
    const r = await api("/api/models/check");
    if (r.ok) return toast("All configured models are available.");
    if (r.error) return toast(r.error, 6000);
    const bad = r.missing.map((m) => `${m.seat}: ${m.model}`).join(" | ");
    toast(`Not available: ${bad} - fix the slugs in engine/config.py`, 9000);
  } catch (err) {
    toast(err.message, 6000);
  }
});

/* Reconnect when the phone comes back from sleep or the tab is refocused. */
document.addEventListener("visibilitychange", () => {
  if (document.visibilityState === "visible" && state.running && !state.stream && state.turnId) {
    const turnEl = $("#thread").lastElementChild;
    if (turnEl) attachStream(state.turnId, turnEl);
  }
});

/* ---------------- boot ---------------- */
async function boot() {
  const health = await api("/api/health").catch(() => null);
  if (health && !health.api_key_present) {
    toast("No API keys set. Run: python check_key.py", 9000);
  } else if (health && health.proposers_available < health.proposers_total) {
    toast(
      `${health.proposers_available}/${health.proposers_total} models active ` +
      `(${health.providers_configured.join(", ")}). Add more keys for a better council.`,
      7000
    );
  }
  const convs = await api("/api/conversations");
  if (convs.length) await openConversation(convs[0].id);
  else await newConversation();
}

(async function init() {
  try {
    const st = await api("/api/auth-status");
    if (st.required && !st.authenticated) { showLogin(); return; }
    showApp();
    await boot();
  } catch (err) {
    showApp();
    toast("Could not reach the server.");
  }
})();
