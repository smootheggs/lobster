// Lobster's phone app. Plain JavaScript, no libraries, no build step.
// Everything the tablet sends is shown as text (never as HTML), so a web page
// that ends up in a brief can't run code here.

const $root = document.getElementById("root");
const state = { csrf: "", tab: "chat", thread: [], status: null, timer: null };

// --- Tiny DOM helper: h("div", {class: "x", onclick: fn}, "text", child) ---
function h(tag, attrs = {}, ...kids) {
  const el = document.createElement(tag);
  for (const [k, v] of Object.entries(attrs || {})) {
    if (v === false || v == null) continue;
    if (k.startsWith("on")) el.addEventListener(k.slice(2), v);
    else if (k === "class") el.className = v;
    else el.setAttribute(k, v === true ? "" : v);
  }
  for (const kid of kids.flat()) {
    if (kid == null || kid === false) continue;
    el.append(kid instanceof Node ? kid : document.createTextNode(String(kid)));
  }
  return el;
}

// --- Talking to the tablet ---
class LoggedOut extends Error {}

async function api(path, body) {
  const opts = { credentials: "same-origin", headers: {} };
  if (body !== undefined) {
    opts.method = "POST";
    opts.headers["Content-Type"] = "application/json";
    opts.headers["X-CSRF-Token"] = state.csrf;
    opts.body = JSON.stringify(body);
  }
  let r;
  try {
    r = await fetch(path, opts);
  } catch {
    throw new Error("Can't reach the tablet. Check that it's on the same Wi-Fi or hotspot, and that Lobster is running.");
  }
  if (r.status === 401) throw new LoggedOut();
  const data = await r.json().catch(() => ({}));
  if (!r.ok) throw new Error(data.error || `The tablet answered ${r.status}.`);
  return data;
}

// Text with source lines like "[2] https://..." turned into safe links.
function textWithLinks(text) {
  const out = [];
  for (const line of String(text).split("\n")) {
    const m = line.match(/^(\[\d+\]\s+)(https?:\/\/\S+)$/);
    if (m) out.push(m[1], h("a", { href: m[2], target: "_blank", rel: "noopener noreferrer" }, m[2]), "\n");
    else out.push(line, "\n");
  }
  out.pop();
  return out;
}

// --- The status strip ---
function strip() {
  const s = state.status;
  const critter = (face, word, mood, label) =>
    h("div", { class: `critter ${mood}`, role: "status", "aria-label": label }, h("span", { class: "face", "aria-hidden": "true" }, face), h("span", { class: "word" }, word));
  if (!s) return h("header", { class: "strip" }, critter("🦞", "…", "", "Loading"), critter("🕷️", "…", "", ""), critter("🐘", "…", "", ""), critter("🐕", "…", "", ""));
  return h("header", { class: "strip" },
    critter("🦞", s.model_up ? "awake" : "model down", s.model_up ? "" : "alarm", `Lobster: ${s.model_up ? "model running" : "model is down"}`),
    critter("🕷️", s.research_waiting ? `${s.research_waiting} waiting` : `${s.inbox} in inbox`, s.research_waiting ? "working" : "", "Spider"),
    critter("🐘", s.busy ? "digesting" : `${s.web_facts + s.owner_facts} facts`, s.busy ? "working" : "", "Elephant"),
    critter("🐕", s.paused ? "paused" : "on guard", s.paused ? "alarm" : "good", `Shiba: ${s.paused ? "paused" : "on guard"}`));
}

async function refreshStatus() {
  try {
    state.status = await api("/api/status");
    const old = document.querySelector(".strip");
    if (old) old.replaceWith(strip());
  } catch (e) {
    if (e instanceof LoggedOut) showLogin();
  }
}

// --- Screens ---
const TABS = [
  ["chat", "🦞", "Chat"], ["research", "🕷️", "Research"], ["library", "📚", "Library"],
  ["memory", "🐘", "Memory"], ["shiba", "🐕", "Shiba"],
];

function tabs() {
  return h("nav", { class: "tabs", "aria-label": "Screens" },
    TABS.map(([id, icon, label]) => h("button", { "aria-current": state.tab === id ? "page" : false, onclick: () => go(id) },
      h("span", { class: "i", "aria-hidden": "true" }, icon), label)));
}

function go(tab) {
  state.tab = tab;
  try { sessionStorage.setItem("tab", tab); } catch {}
  render();
}

function render() {
  const main = h("main", { id: "main" });
  document.body.className = state.tab;
  $root.replaceChildren(strip(), main, tabs());
  ({ chat: chatScreen, research: researchScreen, library: libraryScreen, memory: memoryScreen, shiba: shibaScreen })[state.tab](main);
}

function failure(main, e) {
  if (e instanceof LoggedOut) return showLogin();
  main.append(h("p", { class: "error" }, e.message));
}

// Chat
function bubble(m) {
  if (m.code) return approvalCard(m);
  const cls = m.role === "user" ? "msg me" : `msg lobster${/^(🐘.*\n\n)?🐕 Shiba blocked/.test(m.content) ? " blocked" : ""}${m.thinking ? " thinking" : ""}`;
  return h("div", { class: cls }, m.content);
}

function approvalCard(m) {
  const card = h("div", { class: `ask${m.settled ? " settled" : ""}`, role: "group", "aria-label": "Approval needed" });
  const settle = async (path, label) => {
    card.querySelectorAll("button").forEach((b) => (b.disabled = true));
    try {
      const r = await api(path, { code: m.code });
      m.settled = true;
      m.result = r.reply;
    } catch (e) {
      if (e instanceof LoggedOut) return showLogin();
      m.result = e.message;
    }
    m.settled = true;
    card.replaceWith(approvalCard(m));
  };
  card.append(h("div", { class: "who" }, "🐕 Shiba asks"), h("p", {}, m.content));
  if (m.settled) card.append(h("p", { class: "quiet" }, m.result));
  else card.append(h("div", { class: "row" },
    h("button", { class: "btn yes", onclick: () => settle("/api/approve") }, "Yes, run it"),
    h("button", { class: "btn plain", onclick: () => settle("/api/cancel") }, "No")));
  return card;
}

function chatScreen(main) {
  const thread = h("div", { class: "thread", "aria-live": "polite" });
  const draw = () => {
    thread.replaceChildren(...(state.thread.length ? state.thread.map(bubble)
      : [h("p", { class: "empty" }, "Ask a question, say \"research the Hanseatic League\", or start with /note to save something exactly as you type it.")]));
    window.scrollTo(0, document.body.scrollHeight);
  };
  const box = h("textarea", { rows: 1, placeholder: "Message Lobster", "aria-label": "Message", maxlength: 2000 });
  const send = h("button", { class: "btn", type: "submit" }, "Send");
  const form = h("form", {
    onsubmit: async (ev) => {
      ev.preventDefault();
      const text = box.value.trim();
      if (!text) return;
      box.value = "";
      send.disabled = true;
      state.thread.push({ role: "user", content: text });
      const wait = { role: "assistant", content: "Thinking…", thinking: true };
      state.thread.push(wait);
      draw();
      try {
        const r = await api("/api/chat", { text });
        Object.assign(wait, { content: r.reply, thinking: false, code: r.code || null });
      } catch (e) {
        if (e instanceof LoggedOut) return showLogin();
        Object.assign(wait, { content: e.message, thinking: false });
      }
      send.disabled = false;
      draw();
      refreshStatus();
    },
  }, box, send);
  box.addEventListener("keydown", (ev) => { if (ev.key === "Enter" && !ev.shiftKey) { ev.preventDefault(); form.requestSubmit(); } });
  main.append(h("h1", { class: "sr" }, "Chat"), thread, h("div", { class: "composer" }, form));
  draw();
}

// Research
async function researchScreen(main) {
  const q = h("input", { placeholder: "Ask the Spider…", "aria-label": "Research question", maxlength: 300 });
  const note = h("p", { class: "quiet", "aria-live": "polite" });
  const list = h("ul", { class: "list" });
  const load = async () => {
    const { jobs } = await api("/api/research");
    const newest = jobs.find((j) => j.brief);
    list.replaceChildren(...(jobs.length ? jobs.map((j) => h("li", {},
      h("div", { class: "title" }, j.question),
      h("div", { class: "meta" }, h("span", { class: `tag ${j.status === "waiting" ? "waiting" : ""}` }, j.status),
        `#${j.id} · asked ${j.queued}${j.finished ? `, finished ${j.finished}` : ""}`),
      j.brief ? h("details", { open: newest === j }, h("summary", {}, "Read the brief"), h("div", { class: "brief" }, textWithLinks(j.brief))) : null))
      : [h("li", { class: "empty" }, "No research yet. Ask a question above; the brief arrives here and on Telegram.")]));
  };
  main.append(h("h1", {}, "Research"),
    h("form", { class: "inline", onsubmit: async (ev) => {
      ev.preventDefault();
      if (!q.value.trim()) return;
      try { note.textContent = (await api("/api/research", { question: q.value.trim() })).reply; q.value = ""; await load(); refreshStatus(); }
      catch (e) { if (e instanceof LoggedOut) return showLogin(); note.textContent = e.message; }
    } }, q, h("button", { class: "btn", type: "submit" }, "Research")),
    note, list);
  try { await load(); } catch (e) { failure(main, e); }
}

// Library
async function libraryScreen(main) {
  main.append(h("h1", {}, "Library"), h("p", { class: "quiet" }, "Documents saved with /save, each with a receipt of where and when it came from."));
  try {
    const { items } = await api("/api/library");
    main.append(h("ul", { class: "list" }, items.length ? items.map((it) => h("li", {},
      h("a", { class: "title", href: `/library/${encodeURIComponent(it.name)}`, download: it.name }, it.name),
      h("div", { class: "meta" }, `${(it.bytes / 1024).toFixed(0)} KB${it.saved ? ` · saved ${it.saved}` : ""}`),
      it.url ? h("div", { class: "meta" }, "From ", h("a", { href: /^https?:\/\//.test(it.url) ? it.url : "#", target: "_blank", rel: "noopener noreferrer" }, it.url)) : null,
      it.sha256 ? h("div", { class: "meta hash", title: "Fingerprint: proves the file hasn't changed since it was saved" }, `sha256 ${it.sha256.slice(0, 16)}…`) : null))
      : h("li", { class: "empty" }, "Nothing saved yet. Send /save <link> on Telegram to keep a document, like an annual report.")));
  } catch (e) { failure(main, e); }
}

// Memory
async function memoryScreen(main) {
  const q = h("input", { type: "search", placeholder: "Search what the Elephant remembers", "aria-label": "Search memory", maxlength: 200 });
  const results = h("div", { "aria-live": "polite" });
  const search = async () => {
    try {
      const { facts, notes } = await api(`/api/memory?q=${encodeURIComponent(q.value.trim())}`);
      results.replaceChildren(
        q.value.trim() ? h("ul", { class: "list" }, facts.length ? facts.map((f) => h("li", {},
          h("div", {}, f.text),
          h("div", { class: "meta" }, h("span", { class: `tag ${f.tag}` }, f.tag === "owner" ? "from you" : "from the web"),
            f.tag === "web" ? h("a", { href: /^https?:\/\//.test(f.source) ? f.source : "#", target: "_blank", rel: "noopener noreferrer" }, f.source) : f.added)))
          : h("li", { class: "empty" }, "Nothing matches. Try other words, or research it.")) : null,
        h("h2", {}, "Latest notes"),
        h("ul", { class: "list" }, notes.length ? notes.map((n) => h("li", {}, n)) : h("li", { class: "empty" }, "No notes yet. Type /note in the chat to save one.")));
    } catch (e) { failure(main, e); }
  };
  main.append(h("h1", {}, "Memory"), h("form", { class: "inline", onsubmit: (ev) => { ev.preventDefault(); search(); } },
    q, h("button", { class: "btn", type: "submit" }, "Search")), results);
  search();
}

// Shiba
async function shibaScreen(main) {
  main.append(h("h1", {}, "Shiba"));
  try {
    const [s, { rows }] = await Promise.all([api("/api/status"), api("/api/audit")]);
    state.status = s;
    const toggle = h("button", { class: s.paused ? "btn" : "btn stop", onclick: async () => {
      toggle.disabled = true;
      try { await api(s.paused ? "/api/resume" : "/api/pause", {}); } catch (e) { if (e instanceof LoggedOut) return showLogin(); }
      render();
    } }, s.paused ? "Resume" : "Pause everything");
    const row = (k, v) => [h("dt", {}, k), h("dd", {}, v)];
    main.append(
      h("div", { class: `guard${s.paused ? " paused" : ""}` },
        h("div", {}, h("div", { class: "title" }, s.paused ? "Paused: nothing is being written" : "On guard"),
          h("div", { class: "meta" }, s.paused ? "Reading still works." : "Pause freezes every write until you resume.")), toggle),
      h("dl", { class: "facts" },
        row("Model", `${s.model_up ? "running" : "down"} (${s.model})`),
        row("Blocked today", s.blocks_24h),
        row("Your facts", s.owner_facts),
        row("Web facts", s.web_facts),
        row("Spider sources", s.sources),
        row("Last backup", s.last_backup || "never"),
        row("Version", s.version)),
      h("h2", {}, "Latest decisions"),
      h("ul", { class: "list audit" }, rows.map((r) => {
        const bad = r.verdict === "BLOCK" || /refused|lockout|rejected|error|rate_limited/.test(r.kind) || r.ok === false;
        const kind = r.kind === "web_login" ? (r.ok ? "app login" : r.reason === "locked" ? "app login refused (locked)" : "app login: wrong password") : r.kind.replaceAll("_", " ");
        const what = [kind, r.action, r.verdict, r.reason && r.reason !== "ok" ? `(${r.reason})` : ""].filter(Boolean).join(" ");
        return h("li", { class: bad ? "bad" : "" }, h("span", { class: "meta" }, `${(r.ts || "").slice(5, 16)}  `), what);
      })),
      h("h2", {}, "This phone"),
      h("button", { class: "btn plain", onclick: async () => { try { await api("/api/logout", {}); } catch {} showLogin(); } }, "Log out"));
  } catch (e) { failure(main, e); }
}

// --- Login ---
function showLogin() {
  clearInterval(state.timer);
  state.csrf = "";
  const pw = h("input", { type: "password", autocomplete: "current-password", "aria-label": "Password", placeholder: "Password", required: true });
  const err = h("p", { class: "error", role: "alert" });
  const btn = h("button", { class: "btn", type: "submit" }, "Log in");
  $root.replaceChildren(h("div", { class: "login" },
    h("h1", {}, "🦞 Lobster"),
    h("p", { class: "quiet" }, "Your private agent on the tablet. Set the password on the tablet with python set_password.py."),
    h("form", { onsubmit: async (ev) => {
      ev.preventDefault();
      btn.disabled = true;
      err.textContent = "";
      try {
        const r = await fetch("/api/login", { method: "POST", credentials: "same-origin",
          headers: { "Content-Type": "application/json" }, body: JSON.stringify({ password: pw.value }) });
        const data = await r.json().catch(() => ({}));
        if (!r.ok) throw new Error(data.error || "Login failed.");
        pw.value = "";
        await start();
      } catch (e) {
        err.textContent = e.message === "Failed to fetch" ? "Can't reach the tablet." : e.message;
        btn.disabled = false;
      }
    } }, pw, err, btn)));
  pw.focus();
}

// --- Start ---
async function start() {
  try {
    const me = await api("/api/me");
    state.csrf = me.csrf;
    state.thread = (me.history || []).map((m) => ({ role: m.role, content: m.content }));
  } catch (e) {
    if (e instanceof LoggedOut) return showLogin();
    $root.replaceChildren(h("p", { class: "boot" }, e.message));
    return;
  }
  try { state.tab = sessionStorage.getItem("tab") || "chat"; } catch {}
  render();
  refreshStatus();
  clearInterval(state.timer);
  state.timer = setInterval(() => { if (!document.hidden) refreshStatus(); }, 20000);
}

if ("serviceWorker" in navigator) navigator.serviceWorker.register("/sw.js").catch(() => {});
start();
