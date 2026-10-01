/* Unstuck - client */

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
  guest: false,
  challenge: "medium",
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
  if (res.status === 401 && !path.startsWith("/api/login") && !path.startsWith("/api/signup")) {
    showLogin();
    throw new Error("not authenticated");
  }
  if (!res.ok) {
    let msg = `HTTP ${res.status}`;
    try { msg = (await res.json()).error || msg; } catch {}
    throw new Error(msg);
  }
  return res.status === 204 ? null : res.json();
}

/* ---------------- auth ---------------- */
let authMode = "login";

function showLogin() { $("#login").classList.remove("hidden"); $("#app").classList.add("hidden"); }
function showApp() { $("#login").classList.add("hidden"); $("#app").classList.remove("hidden"); }

function setAuthMode(mode) {
  authMode = mode;
  const signup = mode === "signup";
  // Sign-in and sign-up are no longer the same form with a different button:
  // creating an account takes three steps, signing in takes one.
  $("#auth-form").classList.toggle("hidden", signup);
  $("#signup-flow").classList.toggle("hidden", !signup);
  if (signup) setSignupStep(1);
  $("#auth-hint").textContent = "";
  $("#login-error").textContent = "";
  $("#signup-error").textContent = "";
  document.querySelectorAll(".auth-tab").forEach((b) =>
    b.classList.toggle("active", b.dataset.tab === mode));
  // Slide the underline to the active tab. Motion here is doing a job --
  // showing which control you moved to -- rather than decorating the page.
  const active = document.querySelector(`.auth-tab[data-tab="${mode}"]`);
  const ink = $("#tab-ink");
  if (active && ink) {
    ink.style.width = active.offsetWidth + "px";
    ink.style.transform = `translateX(${active.offsetLeft}px)`;
  }
}

// The auth card was a dead end: no way back to the landing page, and a guest
// who opened it from the trial had no way back to their own chat either.
$("#auth-back").addEventListener("click", (e) => {
  if (state.guest && $("#app").classList.contains("hidden")) {
    // Opened over a trial already in progress: return to it rather than
    // throwing away what they were part-way through.
    e.preventDefault();
    showApp();
  }
});

$("#auth-tabs").addEventListener("click", (e) => {
  const b = e.target.closest(".auth-tab");
  if (b) setAuthMode(b.dataset.tab);
});

$("#auth-form").addEventListener("submit", async (e) => {
  e.preventDefault();
  const btn = $("#auth-submit");
  const username = $("#auth-username").value.trim();
  const password = $("#auth-password").value;
  $("#login-error").textContent = "";

  if (!username || !password) {
    $("#login-error").textContent = "Fill in both fields.";
    return;
  }

  btn.disabled = true;
  const label = btn.textContent;
  btn.textContent = authMode === "signup" ? "Creating..." : "Signing in...";
  try {
    await api(authMode === "signup" ? "/api/signup" : "/api/login", {
      method: "POST",
      body: JSON.stringify({ username, password }),
    });
    showApp();
    await boot();
  } catch (err) {
    // The server's message is the useful one -- "That username is taken",
    // "Password must be at least 8 characters" -- so show it rather than a
    // generic failure.
    $("#login-error").textContent = err.message || "Something went wrong.";
    $("#auth-password").value = "";
    $("#auth-password").focus();
  } finally {
    btn.disabled = false;
    btn.textContent = label;
  }
});


/* ---------------- create an account ----------------
   Three steps. The email and the code are held here between them because the
   server re-checks the code when the account is finally created -- there is no
   half-verified account sitting in the database waiting to be finished. */

const signupState = { email: "", code: "", cooldownUntil: 0 };

function setSignupStep(n) {
  [1, 2, 3].forEach((i) => {
    const panel = $(["#step-email", "#step-code", "#step-profile"][i - 1]);
    panel.classList.toggle("hidden", i !== n);
    const dot = document.querySelector(`.step[data-step="${i}"]`);
    if (dot) {
      dot.classList.toggle("is-active", i === n);
      dot.classList.toggle("is-done", i < n);
    }
  });
  $("#signup-error").textContent = "";
  const first = { 1: "#su-email", 2: "#su-code", 3: "#su-name" }[n];
  setTimeout(() => { const e = $(first); if (e) e.focus(); }, 60);
}

function signupFail(err) {
  $("#signup-error").textContent = err.message || "Something went wrong.";
}

async function withBusy(btn, label, fn) {
  const was = btn.textContent;
  btn.disabled = true;
  btn.textContent = label;
  try { await fn(); } finally { btn.disabled = false; btn.textContent = was; }
}

function startResendCountdown(seconds) {
  signupState.cooldownUntil = Date.now() + seconds * 1000;
  const btn = $("#su-resend");
  const tick = () => {
    const left = Math.ceil((signupState.cooldownUntil - Date.now()) / 1000);
    if (left > 0) {
      btn.disabled = true;
      btn.textContent = `Send it again in ${left}s`;
      setTimeout(tick, 500);
    } else {
      btn.disabled = false;
      btn.textContent = "Send it again";
    }
  };
  tick();
}

async function sendCode(email) {
  const res = await api("/api/signup/start", {
    method: "POST",
    body: JSON.stringify({ email }),
  });
  signupState.email = email;
  const where = res.delivery === "console"
    ? "This server has no mail account set up, so the code was printed to its console."
    : `We sent a code to ${email}. Check your spam folder if it is not there.`;
  $("#su-sent-to").textContent = where;
  startResendCountdown(res.cooldown || 60);
  return res;
}

$("#step-email").addEventListener("submit", async (e) => {
  e.preventDefault();
  const email = $("#su-email").value.trim();
  $("#signup-error").textContent = "";
  if (!email) { $("#signup-error").textContent = "Enter your email address."; return; }
  await withBusy($("#su-send"), "Sending...", async () => {
    try {
      await sendCode(email);
      setSignupStep(2);
    } catch (err) { signupFail(err); }
  });
});

$("#step-code").addEventListener("submit", async (e) => {
  e.preventDefault();
  const code = $("#su-code").value.trim();
  $("#signup-error").textContent = "";
  if (code.length !== 6) { $("#signup-error").textContent = "The code is six digits."; return; }
  await withBusy($("#su-verify"), "Checking...", async () => {
    try {
      await api("/api/signup/verify", {
        method: "POST",
        body: JSON.stringify({ email: signupState.email, code }),
      });
      signupState.code = code;
      setSignupStep(3);
    } catch (err) {
      signupFail(err);
      $("#su-code").select();
    }
  });
});

$("#su-code").addEventListener("input", (e) => {
  // Digits only, and submit itself once six are in -- nobody wants to reach
  // for a button after typing a code they just read off a phone.
  const v = e.target.value.replace(/\D/g, "").slice(0, 6);
  e.target.value = v;
  if (v.length === 6) $("#step-code").requestSubmit();
});

$("#su-resend").addEventListener("click", async () => {
  if (Date.now() < signupState.cooldownUntil) return;
  $("#signup-error").textContent = "";
  try { await sendCode(signupState.email); } catch (err) { signupFail(err); }
});

$("#su-back-email").addEventListener("click", () => {
  signupState.code = "";
  $("#su-code").value = "";
  setSignupStep(1);
});

$("#step-profile").addEventListener("submit", async (e) => {
  e.preventDefault();
  const full_name = $("#su-name").value.trim();
  const purpose = $("#su-purpose").value.trim();
  const password = $("#su-password").value;
  $("#signup-error").textContent = "";
  if (!full_name) { $("#signup-error").textContent = "Enter your name."; return; }
  if (password.length < 8) { $("#signup-error").textContent = "Passwords need at least 8 characters."; return; }
  await withBusy($("#su-finish"), "Creating...", async () => {
    try {
      await api("/api/signup/complete", {
        method: "POST",
        body: JSON.stringify({
          email: signupState.email, code: signupState.code,
          full_name, purpose, password,
        }),
      });
      showApp();
      await boot();
    } catch (err) {
      signupFail(err);
      // The code died (expired, or burnt through its attempts). Sending them
      // back to step 1 is the only move that can still succeed.
      if (/code/i.test(err.message || "")) setSignupStep(1);
    }
  });
});

$("#logout").addEventListener("click", async () => {
  try { await api("/api/logout", { method: "POST" }); } catch {}
  location.reload();
});

async function showWhoami() {
  try {
    const st = await api("/api/auth-status");
    if (st.username) {
      $("#whoami-name").textContent = st.full_name || st.username;
      $("#whoami-name").title = st.username;
      $("#whoami").classList.remove("hidden");
    }
  } catch {}
}

function showGuestBanner(left) {
  if ($("#guest-banner")) return;
  const b = el("div", "guest-banner");
  b.id = "guest-banner";
  b.appendChild(el("span", "", `Trial · ${left} free run, no account needed`));
  const btn = el("button", "guest-signup", "Create an account");
  btn.addEventListener("click", () => { showLogin(); setAuthMode("signup"); });
  b.appendChild(btn);
  document.querySelector(".main").prepend(b);
}

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
  d.innerHTML = `<h2>What are you stuck on?</h2>
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
  turn.appendChild(el("div", "agents-wrap"));
  return turn;
}

/* Find (or create) a seat's panel.
   `isStart` must be true ONLY for agent_start. A debate seat speaks several
   times across rounds and gets a fresh panel each round, but the chunk/done/
   error events that follow carry no round number -- so looking a panel up by
   round meant they never matched and silently rendered a second, unlabelled
   panel per seat. The live one is tracked per key instead. */
function groupFor(agentsBox, role) {
  // Only the parallel proposers are compared side by side. The aggregator is
  // a single step that reads all of them, so it belongs full width underneath,
  // not as another cell competing with its own inputs.
  const fan = role === "proposer";
  const cls = fan ? "agents fan" : "agents seq";
  let last = agentsBox.lastElementChild;
  if (!last || last.className !== cls) {
    last = el("div", cls);
    agentsBox.appendChild(last);
  }
  return last;
}

function agentPanel(container, key, info, isStart) {
  const box = container.closest(".agents-wrap") || container;
  if (!box._current) box._current = {};
  if (!isStart) {
    const live = box._current[key];
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
  // The vendor and model are not sent to the browser unless SHOW_MODEL_NAMES
  // is on, so this is empty in normal use and the chip simply does not appear.
  const modelBit = (info.model || "").split("/").pop();
  if (modelBit) {
    head.appendChild(el("span", "agent-model",
      info.provider ? `${info.provider} · ${modelBit}` : modelBit));
  }
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
  box._current[key] = panel._parts;
  return panel._parts;
}

async function renderPastTurn(t) {
  const thread = $("#thread");
  const turn = buildTurnShell(t.user_prompt, t.mode);
  turn.dataset.turnId = t.id;
  thread.appendChild(turn);
  const agentsBox = turn.querySelector(".agents-wrap");

  // Rebuild the agent panels from the stored event log, collapsed.
  try {
    const events = await api(`/api/turns/${t.id}/events?after=0`);
    for (const ev of events) {
      if (ev.type === "vault" && ev.notes && ev.notes.length) {
        const box = el("div", "vault-note");
        box.appendChild(el("div", "vault-head",
          `Recalled ${ev.notes.length} note${ev.notes.length === 1 ? "" : "s"} from your vault`));
        const list = el("ul", "vault-list");
        for (const n of ev.notes) {
          const li = el("li", "");
          li.appendChild(el("span", "vault-path", n.path));
          if (n.heading) li.appendChild(el("span", "vault-heading", ` · ${n.heading}`));
          list.appendChild(li);
        }
        box.appendChild(list);
        turn.insertBefore(box, agentsBox);
      } else if (ev.type === "agent_start") {
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
        const p = agentPanel(groupFor(agentsBox, "proposer"), ev.agent, ev, true);
        p.panel.classList.add("skipped");
        p.status.className = "agent-status skipped";
        p.status.textContent = "no key";
        p.body.textContent = ev.reason || "This seat is not configured.";
        p.body.classList.remove("streaming");
      }
    }
  } catch {}

  try {
    const evs = await api(`/api/turns/${t.id}/events?after=0`);
    const d = evs.filter((e) => e.type === "disagreement").pop();
    if (d && d.points && d.points.length) turn.appendChild(disagreementBlock(d.points));
    const dec = evs.filter((e) => e.type === "decisions").pop();
    if (dec && dec.decisions && dec.decisions.length) turn._decisions = dec.decisions;
  } catch {}

  if (t.status === "done" && t.final_answer) {
    collapseRun(turn);
    turn.appendChild(finalBlock(t.final_answer));
    if (turn._decisions) turn.appendChild(decisionsBlock(turn._decisions));
  } else if (t.status === "error") {
    turn.appendChild(el("div", "failed", t.error || "This run failed."));
  } else if (t.status === "running") {
    // A run still going from another device or a previous page load - attach to it.
    attachStream(t.id, turn);
  }
}

/* Where the models disagreed.
   Rendered ABOVE the merged answer, because it is the part a reader cannot get
   from a single model and therefore the whole reason to run five. Each point
   shows the opposing positions side by side, then how it was resolved. */
/* The decisions the reader still has to make.
   Placed AFTER the answer on purpose: you read the conclusion, then you are
   told what you have not decided yet. This is the reason the tool exists --
   the hard part of planning is rarely the answer, it is not knowing which
   question to ask -- so it gets its own panel rather than a heading buried in
   markdown. */
function decisionsBlock(decisions) {
  const box = el("div", "decisions");
  box.appendChild(el("div", "decisions-head", "Decide these"));
  const list = el("ol", "decisions-list");
  for (const d of decisions) {
    const li = el("li", "");
    li.appendChild(el("span", "decision-q", d.question));
    if (d.why) li.appendChild(el("span", "decision-why", d.why));
    list.appendChild(li);
  }
  box.appendChild(list);
  return box;
}

function disagreementBlock(points) {
  const box = el("div", "disagree");
  const head = el("div", "disagree-head");
  head.appendChild(el("span", "", points.length === 1
    ? "They disagreed on one thing"
    : `They disagreed on ${points.length} things`));
  box.appendChild(head);

  for (const p of points) {
    const item = el("div", "disagree-item");
    item.appendChild(el("div", "disagree-point", p.point));

    if (p.sides && p.sides.length) {
      const sides = el("div", "disagree-sides");
      for (const s of p.sides) {
        const side = el("div", "disagree-side");
        if (s.seat) side.appendChild(el("span", "disagree-seat", s.seat));
        side.appendChild(el("span", "disagree-says", s.says));
        sides.appendChild(side);
      }
      item.appendChild(sides);
    }
    if (p.resolution) {
      const res = el("div", "disagree-res");
      res.appendChild(el("span", "disagree-res-label", "Resolved"));
      res.appendChild(el("span", "", p.resolution));
      item.appendChild(res);
    }
    box.appendChild(item);
  }
  return box;
}

/* A one-line account of what happened, in place of the raw pipeline.
   "5 agents participated, 1 failed, 3 disagreements" tells a reader everything
   they need; the twelve streaming panels underneath tell them nothing they
   asked for. The panels stay -- behind a toggle -- because when a run goes
   wrong the detail is the only thing that helps. */
function runSummary(turnEl) {
  const wrap = turnEl.querySelector(".agents-wrap");
  if (!wrap) return null;
  const panels = [...wrap.querySelectorAll(".agent")];
  const done = panels.filter((p) => p.querySelector(".agent-status.done")).length;
  const failed = panels.filter((p) => p.querySelector(".agent-status.error")).length;
  const skipped = panels.filter((p) => p.querySelector(".agent-status.skipped")).length;
  const disagreements = turnEl.querySelectorAll(".disagree-item").length;

  const bar = el("div", "run-summary");
  const bits = [];
  if (done) bits.push(`${done} model${done === 1 ? "" : "s"} answered`);
  if (failed) bits.push(`${failed} failed`);
  if (skipped) bits.push(`${skipped} skipped`);
  if (disagreements) bits.push(`${disagreements} disagreement${disagreements === 1 ? "" : "s"}`);
  bar.appendChild(el("span", "run-summary-text", bits.join(" · ") || "run complete"));

  const toggle = el("button", "run-toggle", "Show the debate");
  toggle.addEventListener("click", () => {
    const open = wrap.classList.toggle("revealed");
    toggle.textContent = open ? "Hide the debate" : "Show the debate";
  });
  bar.appendChild(toggle);
  return bar;
}

/* Collapse the machinery once a run ends, and put the summary in its place.
   During the run the panels stay open, because watching five models work IS
   the reassurance that a seven-minute wait needs. The moment there is an
   answer, that reassurance has done its job and becomes clutter. */
function collapseRun(turnEl) {
  const wrap = turnEl.querySelector(".agents-wrap");
  if (!wrap || wrap.classList.contains("collapsed")) return;
  wrap.classList.add("collapsed");
  const summary = runSummary(turnEl);
  if (summary) wrap.parentNode.insertBefore(summary, wrap);
}

/* Export a run as a memo. The turn id is read off the DOM rather than kept in
   module state, so this still works on a conversation reopened days later. */
async function exportMemo(node, save) {
  const turnEl = node.closest(".turn");
  const id = turnEl && turnEl.dataset.turnId;
  if (!id) { toast("This run has no saved record yet."); return; }
  try {
    const r = await api(`/api/turns/${id}/memo${save ? "?save=1" : ""}`);
    if (save) {
      toast(`Saved to your vault: ${r.filename}`, 5000);
      return;
    }
    // Downloaded rather than shown: the point of a memo is that it is a file.
    const blob = new Blob([r.markdown], { type: "text/markdown;charset=utf-8" });
    const url = URL.createObjectURL(blob);
    const a = document.createElement("a");
    a.href = url;
    a.download = r.filename;
    a.click();
    URL.revokeObjectURL(url);
    toast(`Downloaded ${r.filename}`);
  } catch (err) {
    toast(err.message, 6000);
  }
}

function finalBlock(text) {
  const box = el("div", "final");
  const head = el("div", "final-head");
  head.appendChild(el("span", "", "Answer"));
  const memoBtn = el("button", "final-copy", "Memo");
  memoBtn.title = "Export this run as a markdown decision memo";
  memoBtn.addEventListener("click", () => exportMemo(box, false));
  head.appendChild(memoBtn);

  const saveBtn = el("button", "final-copy", "Save to vault");
  saveBtn.title = "Write the memo into your notes, where later runs can find it";
  saveBtn.addEventListener("click", () => exportMemo(box, true));
  head.appendChild(saveBtn);

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

let _timer = null, _startedAt = 0;

function startTimer() {
  _startedAt = Date.now();
  clearInterval(_timer);
  const tick = () => {
    const s = Math.floor((Date.now() - _startedAt) / 1000);
    const el = $("#elapsed");
    // Elapsed time, never an estimate of time remaining. A Full run is 12+
    // calls whose length nobody can predict, and a wrong "2 min left" is worse
    // than no number at all.
    if (el) el.textContent = `${Math.floor(s / 60)}:${String(s % 60).padStart(2, "0")}`;
  };
  tick();
  _timer = setInterval(tick, 1000);
}

function stopTimer() { clearInterval(_timer); _timer = null; }

function setStage(text) {
  const banner = $("#stage-banner");
  if (!text) { banner.classList.add("hidden"); stopTimer(); return; }
  $("#stage-text").textContent = text;
  banner.classList.remove("hidden");
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

  const agentsBox = turnEl.querySelector(".agents-wrap");
  startTimer();
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
        state.turnId = null;
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

    case "vault": {
      // Show exactly which notes were sent. This text left the machine for
      // four AI providers, so it is stated plainly rather than assumed.
      const box = el("div", "vault-note");
      const head = el("div", "vault-head",
        `Recalled ${ev.notes.length} note${ev.notes.length === 1 ? "" : "s"} from your vault`);
      box.appendChild(head);
      const list = el("ul", "vault-list");
      for (const n of ev.notes) {
        const li = el("li", "");
        li.appendChild(el("span", "vault-path", n.path));
        if (n.heading) li.appendChild(el("span", "vault-heading", ` · ${n.heading}`));
        list.appendChild(li);
      }
      box.appendChild(list);
      turnEl.insertBefore(box, turnEl.querySelector(".agents-wrap"));
      break;
    }

    case "agent_start": {
      const p = agentPanel(groupFor(agentsBox, ev.role), ev.agent, ev, true);
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
      // A character count is honest progress. A percentage bar would need a
      // total nobody knows, so it would be a lie that looks precise.
      p.status.textContent = `${p.buffer.length.toLocaleString()} chars`;
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
      p.status.textContent = `${p.buffer.length.toLocaleString()} chars`;
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
      const p = agentPanel(groupFor(agentsBox, "proposer"), ev.agent, ev, true);
      p.panel.classList.add("skipped");
      p.status.className = "agent-status skipped";
      p.status.textContent = "no key";
      p.body.textContent = ev.reason || "This seat is not configured.";
      p.body.classList.remove("streaming");
      break;
    }

    case "disagreement":
      turnEl.appendChild(disagreementBlock(ev.points));
      scrollDown();
      break;

    case "decisions":
      turnEl._decisions = ev.decisions;
      break;

    case "done":
      collapseRun(turnEl);
      turnEl.appendChild(finalBlock(ev.final_answer));
      if (turnEl._decisions) turnEl.appendChild(decisionsBlock(turnEl._decisions));
      setStage(null);
      scrollDown();
      loadConversations();
      break;

    case "failed":
      // A failed run is the one case where the detail is the point, so the
      // machinery stays open.
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
    const res = await api(`/api/conversations/${state.conversationId}/turns`, {
      method: "POST",
      body: JSON.stringify({ prompt, mode: state.mode, challenge: state.challenge }),
    });
    turn.dataset.turnId = res.turn_id;
    if (res.small_talk) {
      // No run was started, so there is no stream to attach to. The server
      // already stored the reply; show it and stop.
      const t = await api(`/api/turns/${res.turn_id}`);
      turn.querySelectorAll(".agents, .stage").forEach((n) => n.remove());
      turn.appendChild(finalBlock(t.final_answer));
      scrollDown();
      return;
    }
    attachStream(res.turn_id, turn);
  } catch (err) {
    if (state.guest && /free run|not authenticated/i.test(err.message)) {
      // Their result is still on screen; that is the argument for signing up.
      turn.appendChild(el("div", "guest-wall",
        "That was your free run. Create an account to keep going — your "
        + "history is saved and you get five runs a day."));
      const cta = el("button", "guest-signup", "Create an account");
      cta.addEventListener("click", () => { showLogin(); setAuthMode("signup"); });
      turn.appendChild(cta);
    } else {
      turn.appendChild(el("div", "failed", err.message));
    }
    state.running = false;
    $("#send").disabled = false;
  }
}

/* ---------------- wiring ---------------- */
$("#send").addEventListener("click", send);

$("#stop").addEventListener("click", async () => {
  if (!state.turnId) return;
  try {
    const r = await api(`/api/turns/${state.turnId}/cancel`, { method: "POST" });
    toast(r.ok ? "Stopping..." : "That run already finished");
  } catch (err) {
    toast(err.message);
  }
});

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


// Three bare words explained only by a `title` attribute -- which needs a
// hover, so on a phone there was no explanation at all. The person who built
// this could not say what the control did, which settles whether it needed
// one. Wording follows what each level actually puts in the prompt.
const CHALLENGE_HINT = {
  low: "Answers what you asked. Raises a risk only if ignoring it would be negligent.",
  medium: "Answers, then names the risks and the decisions you have not made yet.",
  high: "Treats your question as a claim to test. Attacks your assumptions first, including the ones you did not say out loud.",
};

function showChallengeHint() {
  const el = $("#challenge-hint");
  if (el) el.textContent = CHALLENGE_HINT[state.challenge] || CHALLENGE_HINT.medium;
}

$("#challenge").addEventListener("click", (e) => {
  const b = e.target.closest(".chal");
  if (!b) return;
  document.querySelectorAll(".chal").forEach((x) => x.classList.remove("active"));
  b.classList.add("active");
  state.challenge = b.dataset.level;
  showChallengeHint();
  // Remembered per browser: a preference, not shared state.
  try { localStorage.setItem("council_challenge", state.challenge); } catch {}
});

try {
  const saved = localStorage.getItem("council_challenge");
  if (saved && ["low", "medium", "high"].includes(saved)) {
    state.challenge = saved;
    document.querySelectorAll(".chal").forEach((x) =>
      x.classList.toggle("active", x.dataset.level === saved));
  }
} catch {}
// On load as well as on click, or the default arrives unexplained.
showChallengeHint();

$("#new-chat").addEventListener("click", newConversation);

function openSidebar() { $("#sidebar").classList.add("open"); $("#scrim").classList.add("open"); }
function closeSidebar() { $("#sidebar").classList.remove("open"); $("#scrim").classList.remove("open"); }
$("#menu-btn").addEventListener("click", openSidebar);
$("#scrim").addEventListener("click", closeSidebar);

/* ---------------- API keys ---------------- */
function keyRow(p) {
  const row = el("div", "key-row");
  const head = el("div", "key-head");
  head.appendChild(el("span", "key-label", p.label));

  if (p.yours) {
    const b = el("span", "key-badge yours", p.yours);
    head.appendChild(b);
  } else if (p.server_fallback) {
    // Honest about whose quota is being spent.
    head.appendChild(el("span", "key-badge shared", "using host's key"));
  } else {
    head.appendChild(el("span", "key-badge none", "not set"));
  }
  row.appendChild(head);
  row.appendChild(el("div", "key-note", p.notes));

  const form = el("div", "key-form");
  const input = el("input", "key-input");
  input.type = "password";
  input.placeholder = p.yours ? "Replace key..." : "Paste your key";
  input.autocomplete = "off";
  input.spellcheck = false;
  form.appendChild(input);

  const save = el("button", "key-save", "Save");
  save.addEventListener("click", async () => {
    const key = input.value.trim();
    if (!key) return;
    save.disabled = true;
    save.textContent = "Checking...";
    try {
      // The server makes a real call before storing anything, so a key that
      // authenticates but lacks permission is caught here rather than
      // halfway through a seven-minute run.
      const r = await api(`/api/keys/${p.provider}`, {
        method: "PUT", body: JSON.stringify({ key }),
      });
      toast(`${p.label} key saved - ${r.detail}`);
      input.value = "";
      await loadKeys();
    } catch (err) {
      toast(err.message, 7000);
      save.disabled = false;
      save.textContent = "Save";
    }
  });
  form.appendChild(save);

  if (p.yours) {
    const del = el("button", "key-del", "Remove");
    del.addEventListener("click", async () => {
      await api(`/api/keys/${p.provider}`, { method: "DELETE" });
      toast(`${p.label} key removed`);
      await loadKeys();
    });
    form.appendChild(del);
  }
  row.appendChild(form);

  const link = el("a", "key-link", "Get a free key");
  link.href = p.signup.split(" ")[0];
  link.target = "_blank";
  link.rel = "noopener";
  row.appendChild(link);
  return row;
}

async function loadKeys() {
  const box = $("#keys-list");
  try {
    const data = await api("/api/keys");
    box.innerHTML = "";
    if (data.byok_only) {
      box.appendChild(el("div", "key-mode",
        "This instance requires your own keys - the host does not provide any."));
    }
    for (const p of data.providers) box.appendChild(keyRow(p));
  } catch (err) {
    box.textContent = err.message;
  }
}

async function loadVault() {
  const st = $("#vault-status");
  try {
    const v = await api("/api/vault");
    $("#vault-path").value = v.path || "";
    st.textContent = v.configured
      ? `${v.sections} section${v.sections === 1 ? "" : "s"} indexed`
      : "No memory folder set.";
    st.className = "vault-status" + (v.configured ? " ok" : "");
  } catch (err) {
    st.textContent = err.message;
  }
}

$("#vault-save").addEventListener("click", async () => {
  const btn = $("#vault-save");
  btn.disabled = true;
  const label = btn.textContent;
  btn.textContent = "Indexing...";
  try {
    const r = await api("/api/vault", {
      method: "PUT", body: JSON.stringify({ path: $("#vault-path").value.trim() }),
    });
    toast(r.configured ? `Memory indexed - ${r.sections} sections` : "Memory turned off");
    await loadVault();
  } catch (err) {
    toast(err.message, 6000);
  } finally {
    btn.disabled = false;
    btn.textContent = label;
  }
});

$("#open-keys").addEventListener("click", async () => {
  $("#keys-modal").classList.remove("hidden");
  closeSidebar();
  await Promise.all([loadKeys(), loadVault()]);
});
$("#keys-close").addEventListener("click", () => $("#keys-modal").classList.add("hidden"));
$("#keys-modal").addEventListener("click", (e) => {
  if (e.target.id === "keys-modal") $("#keys-modal").classList.add("hidden");
});
document.addEventListener("keydown", (e) => {
  if (e.key === "Escape") $("#keys-modal").classList.add("hidden");
});

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
  // boot() only runs for a signed-in session, so the trial banner is stale by
  // definition here. Leaving it up told a new account it had no account.
  state.guest = false;
  const banner = $("#guest-banner");
  if (banner) banner.remove();
  // Also after a fresh sign-in, not only on a reload of an existing session.
  showWhoami();

  const health = await api("/api/health").catch(() => null);
  if (health && !health.api_key_present) {
    toast("No API keys set. Run: python check_key.py", 9000);
  } else if (health && health.proposers_available < health.proposers_total) {
    toast(
      `${health.proposers_available}/${health.proposers_total} models active.`,
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
    const asked = new URLSearchParams(location.search).get("mode");
    const askedForAuth = asked === "login" || asked === "signup";

    if (st.required && !st.authenticated && askedForAuth) {
      // Someone who clicked "Sign in" asked for the form, and the guest trial
      // below would have swallowed that and dropped them into a chat instead.
      // A returning user with a trial run still unspent could not reach the
      // login form at all.
      showLogin();
      setAuthMode(asked);
      return;
    }

    if (st.required && !st.authenticated && (st.guest_runs_left || 0) > 0) {
      // A stranger should see the product working before being asked to commit
      // to an account. One run, then the wall -- with their result on screen.
      state.guest = true;
      showApp();
      showGuestBanner(st.guest_runs_left);
      const thread = $("#thread");
      thread.innerHTML = "";
      thread.appendChild(buildEmpty());
      return;
    }
    if (st.required && !st.authenticated) {
      showLogin();
      // No explicit request: a fresh instance with no accounts opens on
      // "create account", because there is nothing yet to sign in to.
      setAuthMode(st.mode === "accounts" ? "login" : "signup");
      return;
    }
    showApp();
    await showWhoami();
    await boot();
  } catch (err) {
    showApp();
    toast("Could not reach the server.");
  }
})();
