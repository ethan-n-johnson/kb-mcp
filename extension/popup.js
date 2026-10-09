"use strict";

const $ = (id) => document.getElementById(id);
const { el } = KB;
let settings = { serverUrl: "", token: "" };
let tab = null;
let captured = null; // {html, url} from the last Capture, reused by Update existing

/** Runs inside the page (serialized by executeScript, so it must not use outer variables). */
function grabPage(selectionOnly) {
  const sel = getSelection();
  if (selectionOnly && sel && sel.rangeCount && sel.toString().trim()) {
    const box = document.createElement("div");
    for (let i = 0; i < sel.rangeCount; i++) box.appendChild(sel.getRangeAt(i).cloneContents());
    return { html: box.outerHTML, url: location.href };
  }
  return { html: document.documentElement.outerHTML, url: location.href };
}

function show(kind, ...nodes) {
  $("result").className = kind;
  $("result").replaceChildren(...nodes);
}

function docLink(id, label) {
  const href = `${settings.serverUrl}/docs/${Number(id)}`;
  return el("a", { href, target: "_blank", rel: "noopener", textContent: label });
}

function busy(on) {
  for (const b of document.querySelectorAll("button")) b.disabled = on;
}

function errorText(res) {
  if (res.status === 401) return "The server rejected the token. Check it in settings.";
  const { error, detail } = res.data;
  return error ? `${error}${detail ? ": " + detail : ""}` : `Unexpected response (HTTP ${res.status}).`;
}

/** Turn a POST /api/docs response into UI. */
function showUpload(res) {
  const d = res.data;
  if (res.status === 201 && d.status === "created") {
    const note = d.near_duplicate_check ? " (near-duplicate check skipped)" : "";
    show("ok", "Saved: ", docLink(d.doc_id, `doc ${d.doc_id}`), ` with ${d.chunks} chunks.${note}`);
  } else if (res.status === 409 && d.error === "duplicate") {
    show("warn", "Already saved as ", docLink(d.doc_id, `doc ${d.doc_id}`), ".");
  } else if (res.ok && d.status === "possible_duplicate") {
    showCandidates(d);
  } else {
    show("error", errorText(res));
  }
}

function showCandidates(d) {
  const list = el("ul", { className: "candidates" });
  for (const c of d.candidates || []) {
    const pct = Math.round(Number(c.similarity) * 100);
    const update = el("button", { type: "button", textContent: "Update existing" });
    update.addEventListener("click", () => run(() => updateExisting(c.doc_id)));
    list.append(el("li", {}, docLink(c.doc_id, c.title || `doc ${c.doc_id}`), ` (${pct}% similar) `, update));
  }
  const anyway = el("button", { type: "button", textContent: "Save anyway" });
  anyway.addEventListener("click", () => run(() => post(true)));
  show("warn", d.message || "This looks like a near copy of an existing doc.", list, anyway);
}

async function post(force) {
  const title = $("title").value.trim();
  const tags = [...new Set($("tags").value.split(",").map((t) => t.trim()).filter(Boolean))];
  const body = { content: captured.html, format: "html", tags, force };
  if (title) body.title = title;
  if (/^https?:\/\//.test(captured.url)) body.source_url = captured.url.split("#")[0];
  showUpload(await KB.api(settings, "POST", "/api/docs", body));
}

async function updateExisting(docId) {
  const res = await KB.api(settings, "PATCH", `/api/docs/${Number(docId)}`, {
    content: captured.html,
    format: "html",
  });
  if (res.ok && res.data.status === "updated") {
    show("ok", "Updated ", docLink(docId, `doc ${docId}`), ` (${res.data.chunks} chunks).`);
  } else if (res.ok && res.data.status === "unchanged") {
    show("ok", docLink(docId, `Doc ${docId}`), " already has this content.");
  } else {
    show("error", errorText(res));
  }
}

/** Run a server task with buttons disabled; network problems become a readable error. */
async function run(task) {
  busy(true);
  show("note", "Working...");
  try {
    if (!(await KB.hasAccess(settings.serverUrl))) {
      show("error", "No permission for this server. Open settings and save again.");
    } else {
      await task();
    }
  } catch (err) {
    const timeout = err && err.name === "TimeoutError";
    show("error", timeout ? "The server took too long to answer." : "Could not reach the server. Check the URL in settings.");
  } finally {
    busy(false);
  }
}

async function capture() {
  const selectionOnly = !$("sel-row").hidden && $("sel").checked;
  const [{ result }] = await chrome.scripting.executeScript({
    target: { tabId: tab.id },
    func: grabPage,
    args: [selectionOnly],
  });
  captured = result;
  await post(false);
}

async function init() {
  const openOptions = (e) => { e.preventDefault(); chrome.runtime.openOptionsPage(); };
  $("options-link").addEventListener("click", openOptions);
  $("setup-link").addEventListener("click", openOptions);

  settings = await KB.getSettings();
  if (!settings.serverUrl || !settings.token) {
    $("setup").hidden = false;
    return;
  }
  $("form").hidden = false;
  [tab] = await chrome.tabs.query({ active: true, currentWindow: true });
  $("title").value = (tab && tab.title) || "";
  try {
    const [{ result }] = await chrome.scripting.executeScript({
      target: { tabId: tab.id },
      func: () => (getSelection() ? getSelection().toString().trim().length : 0),
    });
    $("sel-row").hidden = !(result > 0);
  } catch {
    $("capture").disabled = true;
    show("warn", "This page cannot be captured (browser pages and the Web Store are blocked).");
  }
}

$("form").addEventListener("submit", (e) => {
  e.preventDefault();
  run(capture);
});
init();
