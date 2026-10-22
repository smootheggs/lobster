// Sloth Lobster's app. Plain JavaScript, no libraries, no build step.
// Everything the tablet sends is shown as text (never parsed as HTML), so a web page that
// ends up in a brief can't run code here. Icons are fixed strings in this file.

const S = {
  csrf: "", thread: [], status: null, mode: "chat", sheet: null, reader: null,
  jobs: [], watching: new Set(), timer: null, busySending: false, unreachable: false,
};

// --- Tiny DOM helper: h("div", {class: "x", onclick: fn}, "text", child) ---------------------
function h(tag, attrs = {}, ...kids) {
  const el = document.createElement(tag);
  for (const [k, v] of Object.entries(attrs || {})) {
    if (v === false || v == null) continue;
    if (k.startsWith("on")) el.addEventListener(k.slice(2), v);
    else if (k === "class") el.className = v;
    else if (k === "icon") el.append(icon(v));
    else el.setAttribute(k, v === true ? "" : v);
  }
  for (const kid of kids.flat()) {
    if (kid == null || kid === false) continue;
    el.append(kid instanceof Node ? kid : document.createTextNode(String(kid)));
  }
  return el;
}

const ICONS = {
  menu: '<path d="M4 7h16M4 12h16M4 17h16"/>',
  up: '<path d="M12 19V5M5 12l7-7 7 7"/>',
  x: '<path d="M6 6l12 12M18 6L6 18"/>',
  search: '<circle cx="11" cy="11" r="7"/><path d="M20 20l-3.5-3.5"/>',
  globe: '<circle cx="12" cy="12" r="9"/><path d="M3 12h18M12 3a14 14 0 0 1 0 18M12 3a14 14 0 0 0 0 18"/>',
  note: '<path d="M6 3h9l4 4v14H6z"/><path d="M14 3v5h5M9 13h7M9 17h5"/>',
  link: '<path d="M10 14a4 4 0 0 0 5.7 0l3-3a4 4 0 0 0-5.7-5.7l-1 1"/><path d="M14 10a4 4 0 0 0-5.7 0l-3 3a4 4 0 0 0 5.7 5.7l1-1"/>',
  ask: '<path d="M4 5h16v11H8l-4 4z"/><path d="M9.5 9a2.5 2.5 0 1 1 3 2.4V12"/><path d="M12 14.5v.01"/>',
  plus: '<path d="M12 5v14M5 12h14"/>',
  cards: '<rect x="4" y="5" width="14" height="14" rx="2"/><path d="M8 3h12v14"/>',
  books: '<path d="M4 4h5v16H4zM10 4h5v16h-5z"/><path d="M16 5l4 1-3 14-4-1"/>',
  check: '<path d="M5 12l5 5 9-10"/>',
  loader: '<path d="M12 3a9 9 0 1 0 9 9"/>',
};

function icon(name) {
  const svg = document.createElementNS("http://www.w3.org/2000/svg", "svg");
  svg.setAttribute("viewBox", "0 0 24 24");
  svg.setAttribute("fill", "none");
  svg.setAttribute("stroke", "currentColor");
  svg.setAttribute("stroke-width", "1.8");
  svg.setAttribute("stroke-linecap", "round");
  svg.setAttribute("stroke-linejoin", "round");
  svg.setAttribute("aria-hidden", "true");
  svg.innerHTML = ICONS[name] || "";           // constant strings above, never data
  return svg;
}

// --- Talking to the tablet --------------------------------------------------------------------
class LoggedOut extends Error {}
class Unreachable extends Error {}

async function api(path, body) {
  const opts = { credentials: "same-origin", headers: {} };
  if (body !== undefined) {
    opts.method = "POST";
    opts.headers["Content-Type"] = "application/json";
    opts.headers["X-CSRF-Token"] = S.csrf;
    opts.body = JSON.stringify(body);
  }
  // A tablet that's off or asleep may never answer: give up after 20 seconds, so the app can
  // say so and hold notes. Answers that use the model or fetch a document get 2 minutes.
  const ctrl = new AbortController();
  const slow = ["/api/chat", "/api/approve", "/api/save"].includes(path);
  const timer = setTimeout(() => ctrl.abort(), slow ? 120000 : 20000);
  opts.signal = ctrl.signal;
  let r;
  try {
    r = await fetch(path, opts);
  } catch {
    clearTimeout(timer);
    throw new Unreachable("Can't reach the tablet. Check that Tailscale or the Wi-Fi is on and Sloth Lobster is running.");
  }
  clearTimeout(timer);
  if (r.status === 401) throw new LoggedOut();
  const data = await r.json().catch(() => ({}));
  if (!r.ok) throw new Error(data.error || `The tablet answered ${r.status}.`);
  return data;
}

function guard(fn) {
  return async (...args) => {
    try { return await fn(...args); } catch (e) {
      if (e instanceof LoggedOut) return showLogin();
      say(e.message, "blocked");
    }
  };
}

const isHttp = (u) => /^https?:\/\//.test(u || "");

// Text with source lines like "[2] https://..." turned into safe links.
function richText(text) {
  const out = [];
  for (const line of String(text).split("\n")) {
    const m = line.match(/^(\[\d+\]\s+)(https?:\/\/\S+)$/);
    if (m) out.push(m[1], h("a", { href: m[2], target: "_blank", rel: "noopener noreferrer" }, m[2]), "\n");
    else out.push(line, "\n");
  }
  out.pop();
  return out;
}

// --- Held notes: kept on this device while the tablet can't be reached ----------------------
// Each note has its own id, so if the connection drops after the tablet saved it, sending it
// again doesn't save it twice. At most 50 wait here; they go through Shiba like any note.
const OUTBOX_KEY = "sloth-lobster-outbox";
const OUTBOX_MAX = 50;
const newId = () => (crypto.randomUUID ? crypto.randomUUID()
  : Array.from(crypto.getRandomValues(new Uint8Array(16)), (b) => b.toString(16).padStart(2, "0")).join(""));

function outbox() {
  try { const v = JSON.parse(localStorage.getItem(OUTBOX_KEY) || "[]"); return Array.isArray(v) ? v : []; }
  catch { return []; }
}
function setOutbox(items) {
  try { localStorage.setItem(OUTBOX_KEY, JSON.stringify(items)); return true; } catch { return false; }
}
function hold(note) {
  const items = outbox();
  if (items.length >= OUTBOX_MAX) return false;
  return setOutbox([...items, note]);
}

let flushing = false;
async function flushOutbox() {
  if (flushing || !S.csrf || !outbox().length) return;
  flushing = true;
  try {
    for (const note of outbox()) {
      let reply;
      try {
        reply = (await api("/api/note", { id: note.id, text: note.text })).reply;
      } catch (e) {
        if (e instanceof Unreachable || e instanceof LoggedOut) break;   // try again later
        reply = e.message;                                               // refused: show why, don't retry forever
      }
      setOutbox(outbox().filter((n) => n.id !== note.id));
      S.thread.push({ t: "lob", text: `Held note sent: ${reply}` });
    }
  } finally {
    flushing = false;
    paintThread();
    paintChrome();
  }
}
setInterval(() => { if (!document.hidden) flushOutbox(); }, 15000);
window.addEventListener("online", () => { flushOutbox(); refreshStatus(); });

function heldChip() {
  const n = outbox().length;
  return n ? h("span", { class: "held", title: "Notes kept on this device until the tablet is reachable" }, `Waiting to send (${n})`) : null;
}

// --- Status --------------------------------------------------------------------------------------
function dotState(s) {
  if (S.unreachable) return ["down", "can't reach tablet"];
  if (!s) return ["", "…"];
  if (s.paused) return ["down", "paused"];
  if (!s.model_up) return ["down", "model down"];
  if (s.online === false) return ["off", "offline"];
  if (s.busy || s.research_waiting || (s.now && s.now !== "idle")) return ["busy", "working"];
  return ["ready", "ready"];
}

function dotButton() {
  const [cls, label] = dotState(S.status);
  return h("button", { class: "dot-btn", onclick: () => openSheet("status"), "aria-label": `Status: ${label}` },
    h("span", { class: `dot ${cls}` }), label);
}

async function refreshStatus() {
  try {
    S.status = await api("/api/status");
    S.unreachable = false;
    rememberDoors(S.status.doors);
    if (S.watching.size) await refreshJobs();
    flushOutbox();
  } catch (e) {
    if (e instanceof LoggedOut) return showLogin();
    if (e instanceof Unreachable) S.unreachable = true;
  }
  paintChrome();
  schedule();
}

function schedule() {
  clearTimeout(S.timer);
  const active = S.watching.size || (S.status && (S.status.research_waiting || S.status.busy));
  S.timer = setTimeout(() => { if (!document.hidden) refreshStatus(); else schedule(); }, active ? 3000 : 20000);
}

async function refreshJobs() {
  const { jobs } = await api("/api/research");
  S.jobs = jobs;
  for (const id of [...S.watching]) {
    const job = jobs.find((j) => j.id === id);
    const live = S.thread.find((m) => m.t === "step" && m.live && m.job === id);
    if (!job || job.status === "waiting") {
      if (live) live.text = S.status && S.status.now.includes(`#${id}`) ? S.status.now : `Research #${id} is waiting its turn`;
      continue;
    }
    S.watching.delete(id);
    if (live) Object.assign(live, { live: false, text: job.status === "done" ? `Research #${id} finished` : `Research #${id}: ${job.status}` });
    if (job.status === "done") S.thread.push({ t: "brief", job });
  }
  paintThread();
}

// --- The thread -------------------------------------------------------------------------------------
function say(text, kind = "") {
  S.thread.push({ t: "lob", text, kind });
  paintThread();
}

// While waiting for a reply: the sloth rides his lobster across a small bubble. Pure CSS
// (transform and opacity only), so it costs next to nothing; still with reduced motion.
function slothThinking() {
  return h("div", { class: "swim", role: "status", "aria-label": "Sloth Lobster is thinking" },
    h("span", { class: "swim-wave" }),
    h("div", { class: "swim-glide" },
      h("span", { class: "swim-bub" }), h("span", { class: "swim-bub two" }),
      h("img", { class: "swim-bob", src: "/sloth-lobster.svg", alt: "", width: "34", height: "28" })));
}

function item(m) {
  if (m.t === "me") return h("div", { class: "me" }, m.text);
  if (m.t === "lob" && m.kind === "thinking") return slothThinking();
  if (m.t === "lob") return h("div", { class: `lob ${m.kind || ""}` }, m.text);
  if (m.t === "step") return h("div", { class: `step${m.live ? " live" : ""}`, icon: m.live ? "loader" : "check" }, m.text);
  if (m.t === "ask") return askCard(m);
  if (m.t === "brief") return briefCard(m.job);
  return h("div");
}

function askCard(m) {
  if (m.settled) return h("div", { class: "ask settled" }, h("div", { class: "what", icon: "check" }, ` ${m.result}`));
  const card = h("div", { class: "ask", role: "group", "aria-label": "Needs your OK" });
  const answer = guard(async (path) => {
    card.querySelectorAll("button").forEach((b) => (b.disabled = true));
    const r = await api(path, { code: m.code });
    Object.assign(m, { settled: true, result: r.reply });
    paintThread();
  });
  card.append(
    h("div", { class: "label" }, "Needs your OK · valid 10 minutes"),
    h("div", { class: "what" }, `${m.ask.label} “${m.ask.text}”`),
    h("div", { class: "why" }, `Asking because ${m.ask.why}.`),
    h("div", { class: "row" },
      h("button", { class: "btn solid", onclick: () => answer("/api/approve") }, m.ask.label),
      h("button", { class: "btn", onclick: () => answer("/api/cancel") }, m.ask.label === "Save note" ? "Don't save" : "Cancel")));
  return card;
}

function briefCard(job) {
  const points = (job.brief || "").split("\n").filter((l) => l.startsWith("- ")).slice(0, 2)
    .map((l) => l.slice(2).replace(/\s*\[\d+\]$/, ""));
  return h("div", { class: "card clickable", role: "button", tabindex: "0",
    onclick: () => openReader({ kind: "brief", job }), onkeydown: (e) => { if (e.key === "Enter") openReader({ kind: "brief", job }); } },
    h("div", { class: "kicker" }, h("span", {}, `Brief · ${job.cards || 0} card${job.cards === 1 ? "" : "s"}`), h("span", {}, "in your vault")),
    h("div", { class: "head" }, job.topic || job.question),
    h("div", { class: "body" }, points.join(" · ") || "No usable facts came back."),
    h("div", { class: "body" }, h("a", { href: "#", onclick: (e) => { e.preventDefault(); } }, "Read brief")));
}

function paintThread() {
  const el = document.getElementById("thread");
  if (!el) return;
  const atBottom = el.parentElement.scrollHeight - el.parentElement.scrollTop - el.parentElement.clientHeight < 80;
  el.replaceChildren(...(S.thread.length ? S.thread.map(item) : [h("div", { class: "empty" },
    h("div", { class: "hello" }, "What should we look into?"),
    "Research a topic, ask your vault, or save a note. Everything lands in your vault inbox.")]));
  if (atBottom) el.parentElement.scrollTop = el.parentElement.scrollHeight;
}

// --- Composer: chat, or one of the four direct actions -------------------------------------------
const MODES = {
  chat: { placeholder: "Ask Sloth Lobster" },
  research: { chip: "Research", icon: "globe", placeholder: "Research a topic", path: "/api/research", key: "question" },
  ask: { chip: "Ask", icon: "ask", placeholder: "Ask your vault", path: "/api/ask", key: "question" },
  note: { chip: "Note", icon: "note", placeholder: "Write a note, exactly as it should be saved", path: "/api/note", key: "text" },
  save: { chip: "Save link", icon: "link", placeholder: "Paste a link to a document", path: "/api/save", key: "url" },
};

function composer() {
  const box = h("textarea", { id: "box", rows: 1, placeholder: MODES[S.mode].placeholder, "aria-label": "Message", maxlength: 2000 });
  const send = h("button", { class: "send", type: "submit", "aria-label": "Send", icon: "up" });
  const grow = () => { box.style.height = "auto"; box.style.height = `${box.scrollHeight}px`; };
  box.addEventListener("input", grow);
  box.addEventListener("keydown", (e) => {
    if (e.key === "Enter" && !e.shiftKey && !e.isComposing) { e.preventDefault(); form.requestSubmit(); }
  });
  const chips = h("div", { class: "chips", role: "toolbar", "aria-label": "Actions" },
    Object.entries(MODES).filter(([k]) => k !== "chat").map(([k, m]) => h("button", {
      class: "chip", type: "button", icon: m.icon, "aria-pressed": S.mode === k ? "true" : "false",
      onclick: () => { S.mode = S.mode === k ? "chat" : k; paintComposer(true); },
    }, m.chip)));
  const form = h("form", { onsubmit: (e) => { e.preventDefault(); submit(box.value); } },
    h("div", { class: "field" }, box, send));
  return h("div", { class: "composer" }, h("div", { class: "composer-inner" }, chips, form));
}

function paintComposer(focus = false) {
  const old = document.querySelector(".composer");
  const text = document.getElementById("box")?.value || "";
  const fresh = composer();
  old.replaceWith(fresh);
  const box = document.getElementById("box");
  box.value = text;
  if (focus) box.focus();
}

const submit = guard(async (raw) => {
  const text = raw.trim();
  if (!text || S.busySending) return;
  const mode = MODES[S.mode];
  S.busySending = true;
  document.getElementById("box").value = "";
  S.thread.push({ t: "me", text: mode.chip ? `${mode.chip}: ${text}` : text });
  const wait = { t: "lob", text: "…", kind: "thinking" };
  S.thread.push(wait);
  paintThread();
  const noteId = S.mode === "note" ? newId() : null;
  try {
    let r;
    try {
      r = await api(mode.path || "/api/chat", mode.path ? { [mode.key]: text, ...(noteId ? { id: noteId } : {}) } : { text });
    } catch (e) {
      if (!(noteId && e instanceof Unreachable)) { S.thread.splice(S.thread.indexOf(wait), 1); throw e; }
      S.thread.splice(S.thread.indexOf(wait), 1);
      S.thread.push(hold({ id: noteId, text, at: Date.now() })
        ? { t: "lob", text: "📥 Held on this device. It's sent by itself when the tablet is reachable." }
        : { t: "lob", text: `Can't hold more than ${OUTBOX_MAX} notes here. Copy this one somewhere safe.`, kind: "blocked" });
      return;
    }
    S.thread.splice(S.thread.indexOf(wait), 1);
    if (r.code && r.ask) {
      if (r.reply) S.thread.push({ t: "lob", text: r.reply.split("\n\n")[0] });
      S.thread.push({ t: "ask", code: r.code, ask: r.ask });
    } else {
      S.thread.push({ t: "lob", text: r.reply || "Done.", kind: /Shiba blocked|paused/.test(r.reply || "") ? "blocked" : "" });
    }
    const job = (r.reply || "").match(/Research #(\d+)(?: is)? queued/);
    if (job) watch(Number(job[1]));
  } finally {
    S.busySending = false;
    if (S.mode !== "chat") { S.mode = "chat"; paintComposer(); }
    paintThread();
    refreshStatus();
  }
});

function watch(id) {
  S.watching.add(id);
  S.thread.push({ t: "step", live: true, job: id, text: `Research #${id} is waiting its turn` });
  paintThread();
  schedule();
}

// --- Sidebar (drawer on small screens) ---------------------------------------------------------------
function navigation() {
  const s = S.status || {};
  const go = (fn) => () => { closeSheet(); fn(); };
  return h("div", {},
    h("div", { class: "nav" },
      h("button", { class: "nav-item", icon: "plus", onclick: go(newChat) }, "New chat"),
      h("button", { class: "nav-item", icon: "search", onclick: go(() => openReader({ kind: "memory" })) }, "Search your vault"),
      h("button", { class: "nav-item", icon: "cards", onclick: go(() => openReader({ kind: "review" })) },
        "Cards to review", s.unreviewed ? h("span", { class: "count" }, s.unreviewed) : null),
      h("button", { class: "nav-item", icon: "books", onclick: go(() => openReader({ kind: "library" })) }, "Library")),
    h("div", { class: "nav-label" }, "Briefs"),
    h("div", { class: "history" }, S.jobs.length ? S.jobs.slice(0, 25).map((j) => h("button", {
      class: "nav-item", title: j.question,
      onclick: go(() => (j.brief ? openReader({ kind: "brief", job: j }) : null)),
    }, h("span", {}, j.topic || j.question), j.status === "waiting" ? h("span", { class: "count" }, "waiting") : null))
      : h("div", { class: "hint pad" }, "Your research briefs will appear here.")),
    h("div", { class: "guard-row" }, dotButton()));
}

const newChat = guard(async () => {
  await api("/api/new", {});
  S.thread = [];
  paintThread();
});

// --- Sheets: drawer, status, reader ------------------------------------------------------------------
function openSheet(name) { S.sheet = name; paintSheets(); }
function closeSheet() { S.sheet = null; paintSheets(); }
function openReader(r) { S.reader = r; paintSheets(); }
function closeReader() { S.reader = null; paintSheets(); }

async function statusSheet() {
  const box = h("div", { class: "status-sheet", role: "dialog", "aria-label": "Status" }, h("p", { class: "hint" }, "Checking…"));
  try {
    const [s, { rows }] = await Promise.all([api("/api/status"), api("/api/audit")]);
    S.status = s;
    const [cls, label] = dotState(s);
    const dd = (k, v) => [h("dt", {}, k), h("dd", {}, v)];
    const running = s.research_waiting || (s.now && s.now !== "idle");
    box.replaceChildren(
      h("div", { class: "sheet-title" }, h("span", { class: `dot ${cls}` }), label === "ready" ? "Ready" : label[0].toUpperCase() + label.slice(1)),
      h("dl", { class: "facts" },
        dd("Model", s.model_up ? `running (${s.model})` : "down"),
        dd("Now", s.now || "idle"),
        dd("Research waiting", s.research_waiting),
        dd("Cards to review", `${s.unreviewed} of ${s.cards}`),
        dd("Facts", `${s.owner_facts} yours · ${s.web_facts} web`),
        dd("Blocked today", s.blocks_24h),
        dd("Typical research", s.research_seconds == null ? "not measured yet"
          : s.research_seconds < 90 ? `${Math.round(s.research_seconds)} s` : `${(s.research_seconds / 60).toFixed(1)} min`),
        dd("Internet", s.online === false ? "offline · research waits" : "online"),
        dd("Held here", `${outbox().length} waiting`),
        ...(s.doors || []).flatMap((d) => dd(`App (${d.kind})`, d.url.replace("https://", ""))),
        ...(s.wifi ? dd("Wi-Fi door", s.wifi) : []),
        dd("Last backup", s.last_backup || "never"),
        dd("Version", s.version)),
      ...(s.ollama ? [h("p", { class: "error" }, `⚠️ ${s.ollama}`)] : []),
      h("div", { class: "row" },
        running ? h("button", { class: "btn", onclick: guard(async () => { say((await api("/api/stop", {})).reply); closeSheet(); refreshStatus(); }) }, "Stop research") : null,
        h("button", { class: s.paused ? "btn solid" : "btn danger", onclick: guard(async () => {
          say((await api(s.paused ? "/api/resume" : "/api/pause", {})).reply);
          closeSheet(); refreshStatus();
        }) }, s.paused ? "Resume" : "Pause all writes"),
        h("button", { class: "btn", onclick: () => location.reload() }, "Refresh"),
        h("button", { class: "btn", onclick: guard(async () => {
          await api("/api/logout_all", {});
          showLogin();
        }) }, "Log out everywhere")),
      h("ul", { class: "events", "aria-label": "Latest guard events" }, rows.slice(0, 6).map((r) => {
        const bad = r.verdict === "BLOCK" || /refused|lockout|rejected|error|rate_limited/.test(r.kind) || r.ok === false;
        const what = [r.kind.replaceAll("_", " "), r.action, r.verdict, r.reason && r.reason !== "ok" ? `(${r.reason})` : ""].filter(Boolean).join(" ");
        return h("li", { class: bad ? "bad" : "" }, `${(r.ts || "").slice(11, 16)}  ${what}`);
      })));
  } catch (e) {
    if (e instanceof LoggedOut) return showLogin();
    box.replaceChildren(h("p", { class: "error" }, e.message));
  }
  return box;
}

const norm = (t) => String(t || "").toLowerCase().replace(/[^\p{L}\p{N}]+/gu, " ").trim();
// A title made from the sentence itself shouldn't be shown twice.
const sameText = (text, title) => norm(text).startsWith(norm(title)) && norm(text).length - norm(title).length < 24;

function cardView(c, onChange) {
  const body = h("div", { class: "card" },
    h("div", { class: "kicker" }, h("span", {}, "Card"), h("span", { class: `status-${c.status}` }, c.status)),
    h("div", { class: "head" }, c.title),
    c.text && !sameText(c.text, c.title) ? h("div", { class: "body" }, c.text) : null,
    h("div", { class: "actions" },
      ["verified", "wrong"].map((st) => h("button", {
        class: "btn small", "aria-pressed": c.status === st ? "true" : "false",
        onclick: guard(async () => {
          const r = await api("/api/review", { id: c.id, status: c.status === st ? "unreviewed" : st });
          if (!r.ok) return say(r.reply, "blocked");
          c.status = c.status === st ? "unreviewed" : st;
          onChange?.();
          refreshStatus();
        }),
      }, st === "verified" ? "Mark verified" : "Mark wrong")),
      isHttp(c.source) ? h("a", { class: "btn small", href: c.source, target: "_blank", rel: "noopener noreferrer" }, "Source") : null));
  return body;
}

async function readerBody(r) {
  const list = (items, view, empty) => items.length ? h("div", { class: "list" }, items.map(view)) : h("p", { class: "hint" }, empty);
  if (r.kind === "brief") {
    const { cards } = await api(`/api/cards?job=${r.job.id}`);
    const box = h("div", { class: "reader-body" });
    const paint = () => box.replaceChildren(
      h("div", { class: "brief-text" }, richText(r.job.brief)),
      h("div", { class: "nav-label flush" }, "Cards from this brief"),
      list(cards, (c) => cardView(c, paint), "This brief linked to cards you already had."));
    paint();
    return [r.job.topic || r.job.question, box];
  }
  if (r.kind === "review") {
    const { cards } = await api("/api/cards?status=unreviewed");
    const box = h("div", { class: "reader-body" });
    const paint = () => box.replaceChildren(
      h("p", { class: "hint" }, "Check each card against its source. Wrong cards are never used again; nothing is deleted."),
      list(cards, (c) => cardView(c, paint), "Nothing to review. New research will add cards here."));
    paint();
    return ["Cards to review", box];
  }
  if (r.kind === "library") {
    const { items } = await api("/api/library");
    return ["Library", h("div", { class: "reader-body" }, list(items, (it) => h("div", { class: "card" },
      h("div", { class: "head" }, h("a", { href: `/library/${encodeURIComponent(it.name)}`, download: it.name }, it.name)),
      h("div", { class: "body" }, `${Math.max(1, Math.round(it.bytes / 1024))} KB${it.saved ? ` · saved ${it.saved}` : ""}`),
      isHttp(it.url) ? h("div", { class: "body" }, "From ", h("a", { href: it.url, target: "_blank", rel: "noopener noreferrer" }, new URL(it.url).hostname)) : null),
    "Nothing saved yet. Use Save link to keep a document, like an annual report."))];
  }
  if (r.kind === "memory") {
    const results = h("div", { class: "list", "aria-live": "polite" });
    const input = h("input", { type: "search", placeholder: "What do you know about…", "aria-label": "Search your vault", maxlength: 200 });
    const run = guard(async () => {
      const { facts, notes } = await api(`/api/memory?q=${encodeURIComponent(input.value.trim())}`);
      results.replaceChildren(...(input.value.trim()
        ? (facts.length ? facts.map((f) => h("div", { class: "card" },
          h("div", { class: "kicker" }, h("span", {}, f.tag === "owner" ? "Your note" : f.source_title), f.status === "verified" ? h("span", { class: "status-verified" }, "verified") : null),
          h("div", { class: "body strong" }, f.text)))
          : [h("p", { class: "hint" }, "Nothing in your vault about that yet.")])
        : [h("div", { class: "nav-label flush" }, "Latest notes"),
          ...(notes.length ? notes.map((n) => h("div", { class: "card" }, h("div", { class: "body strong" }, n)))
            : [h("p", { class: "hint" }, "No notes yet.")])]));
    });
    run();
    setTimeout(() => input.focus(), 50);
    return ["Search your vault", h("div", { class: "reader-body" },
      h("form", { class: "search", onsubmit: (e) => { e.preventDefault(); run(); } }, input, h("button", { class: "btn", type: "submit" }, "Search")),
      results)];
  }
  return ["", h("div")];
}

async function paintSheets() {
  document.querySelectorAll(".scrim, .drawer, .status-sheet, .reader").forEach((el) => el.remove());
  const app = document.querySelector(".app");
  if (!app) return;
  app.classList.toggle("reading", !!S.reader);
  if (S.sheet === "drawer") {
    app.append(h("div", { class: "scrim for-drawer", onclick: closeSheet }),
      h("nav", { class: "drawer", "aria-label": "Menu" }, navigation()));
  }
  if (S.reader) {
    const reader = h("aside", { class: "reader", "aria-label": "Reader" }, h("p", { class: "hint" }, "Loading…"));
    app.append(h("div", { class: "scrim for-reader", onclick: closeReader }), reader);
    try {
      const [title, body] = await readerBody(S.reader);
      reader.replaceChildren(h("div", { class: "reader-head" },
        h("div", { class: "title" }, title),
        h("button", { class: "icon-btn", "aria-label": "Close", onclick: closeReader, icon: "x" })), body);
    } catch (e) {
      if (e instanceof LoggedOut) return showLogin();
      reader.replaceChildren(h("p", { class: "error" }, e.message));
    }
  }
  if (S.sheet === "status") {
    app.append(h("div", { class: "scrim", onclick: closeSheet }), await statusSheet());
  }
}

// --- Reaching the tablet: remembered doors, "Try now", hand-off between apps ------------------------
// The addresses come only from the tablet's own status answer, and links only ever point at
// private addresses on Sloth Lobster's port, so this bar can't send you anywhere else.
const DOORS_KEY = "sloth-lobster-doors";
const PRIVATE = /^https:\/\/(127\.|10\.|192\.168\.|172\.(1[6-9]|2\d|3[01])\.|100\.(6[4-9]|[7-9]\d|1[01]\d|12[0-7])\.)[\d.]+:\d+$/;

function rememberDoors(doors) {
  // Merge, one address per kind: the hotspot door is remembered even while it's shut.
  const byKind = {};
  try { for (const d of JSON.parse(localStorage.getItem(DOORS_KEY) || "[]")) if (PRIVATE.test(d.url || "")) byKind[d.kind] = d; }
  catch { /* start fresh */ }
  for (const d of doors || []) if (PRIVATE.test(d.url || "")) byKind[String(d.kind).slice(0, 20)] = { url: d.url, kind: String(d.kind).slice(0, 20) };
  try { localStorage.setItem(DOORS_KEY, JSON.stringify(Object.values(byKind).slice(0, 6))); } catch { /* fine */ }
}

function otherDoors() {
  let doors = [];
  try { doors = JSON.parse(localStorage.getItem(DOORS_KEY) || "[]"); } catch { doors = []; }
  const onTablet = /^127\./.test(location.hostname);
  return (Array.isArray(doors) ? doors : []).filter((d) => PRIVATE.test(d.url || "") && d.url !== location.origin
    && (onTablet || !/^https:\/\/127\./.test(d.url)));
}

function reachBar() {
  if (!S.unreachable) return null;
  const tryNow = h("button", { class: "btn small", onclick: async () => {
    tryNow.disabled = true;
    await refreshStatus();
    if (S.unreachable) tryNow.disabled = false;
  } }, "Try now");
  return h("div", { class: "reach", role: "status" },
    h("span", {}, "🦞 Can't reach the tablet."), tryNow,
    ...otherDoors().map((d) => h("a", { class: "btn small", href: d.url },
      d.kind === "Wi-Fi" ? "Try hotspot" : d.kind === "Tailscale" ? "Try Tailscale" : `Try ${d.kind}`)));
}

// --- Shell -------------------------------------------------------------------------------------------
function paintChrome() {
  const reach = document.querySelector(".reach-slot");
  if (reach) reach.replaceChildren(...[reachBar()].filter(Boolean));
  const side = document.querySelector(".sidebar");
  if (side) side.replaceChildren(navigation());
  const top = document.querySelector(".topbar .dot-btn");
  if (top) top.replaceWith(dotButton());
  const held = document.querySelector(".topbar .held-slot");
  if (held) held.replaceChildren(...[heldChip()].filter(Boolean));
}

function renderApp() {
  document.body.replaceChildren(h("div", { class: "app" },
    h("nav", { class: "sidebar", "aria-label": "Menu" }, navigation()),
    h("main", { class: "chat" },
      h("header", { class: "topbar" },
        h("button", { class: "icon-btn menu", "aria-label": "Menu", onclick: () => openSheet("drawer"), icon: "menu" }),
        h("div", { class: "title" }, "Sloth Lobster"),
        h("span", { class: "held-slot" }, heldChip()),
        dotButton()),
      h("div", { class: "reach-slot" }, reachBar()),
      h("div", { class: "scroll" }, h("div", { class: "thread", id: "thread", "aria-live": "polite" })),
      composer())));
  paintThread();
}

document.addEventListener("keydown", (e) => {
  if (e.key !== "Escape") return;
  if (S.sheet) closeSheet(); else if (S.reader) closeReader();
});

// --- Login -------------------------------------------------------------------------------------------
function showLogin() {
  clearTimeout(S.timer);
  S.csrf = "";
  const pw = h("input", { type: "password", autocomplete: "current-password", "aria-label": "Password", placeholder: "Password", required: true });
  const err = h("p", { class: "error", role: "alert" });
  const btn = h("button", { class: "btn solid", type: "submit" }, "Log in");
  document.body.replaceChildren(h("div", { class: "login" }, h("form", {
    onsubmit: async (e) => {
      e.preventDefault();
      btn.disabled = true;
      err.textContent = "";
      try {
        const r = await fetch("/api/login", { method: "POST", credentials: "same-origin",
          headers: { "Content-Type": "application/json" }, body: JSON.stringify({ password: pw.value }) });
        const data = await r.json().catch(() => ({}));
        if (!r.ok) throw new Error(data.error || "Login failed.");
        pw.value = "";
        await start();
      } catch (e2) {
        err.textContent = e2.message === "Failed to fetch" ? "Can't reach the tablet." : e2.message;
        btn.disabled = false;
      }
    },
  }, h("h1", {}, "Sloth Lobster"), h("p", {}, "Your private agent on the tablet."), pw, err, btn)));
  pw.focus();
}

// --- Start -------------------------------------------------------------------------------------------
async function start() {
  try {
    const me = await api("/api/me");
    S.csrf = me.csrf;
    S.thread = (me.history || []).map((m) => ({ t: m.role === "user" ? "me" : "lob", text: m.content }));
    const [{ jobs }, status] = await Promise.all([api("/api/research"), api("/api/status")]);
    S.jobs = jobs;
    S.status = status;
    rememberDoors(status.doors);
    for (const j of jobs) if (j.status === "waiting") S.watching.add(j.id);
  } catch (e) {
    if (e instanceof LoggedOut) return showLogin();
    if (!(e instanceof Unreachable)) {
      document.body.replaceChildren(h("div", { class: "boot" }, h("p", {}, e.message),
        h("button", { class: "btn", onclick: () => start() }, "Try again")));
      return;
    }
    // The tablet can't be reached: open anyway, so notes can be held here until it can.
    S.unreachable = true;
    if (!document.querySelector(".app")) {
      renderApp();
      say("Can't reach the tablet right now. Notes you write are held on this device and sent later.");
    }
    clearTimeout(S.retry);
    S.retry = setTimeout(start, 15000);
    return;
  }
  S.unreachable = false;
  renderApp();
  schedule();
  flushOutbox();
}

// --- Staying fresh: on return, on reconnect, and after an update ---------------------------------------
document.addEventListener("visibilitychange", () => {
  if (document.hidden || !S.csrf) return;
  refreshStatus();
  if (S.swReg) S.swReg.update().catch(() => {});
});

function updateToast() {
  if (document.querySelector(".toast")) return;
  document.body.append(h("div", { class: "toast", role: "status" },
    h("span", {}, "🦞 A new version is ready."),
    h("button", { class: "btn small solid", onclick: () => location.reload() }, "Reload")));
}

if ("serviceWorker" in navigator) {
  const hadController = !!navigator.serviceWorker.controller;
  navigator.serviceWorker.addEventListener("controllerchange", () => { if (hadController) updateToast(); });
  navigator.serviceWorker.register("/sw.js").then((reg) => { S.swReg = reg; }).catch(() => {});
}
start();
