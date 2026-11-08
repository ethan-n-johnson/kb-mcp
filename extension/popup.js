"use strict";

const $ = (id) => document.getElementById(id);
const { el } = KB;
let settings = { serverUrl: "", token: "" };
let tab = null;
let captured = null; // {html, url, frames} from the last Capture, reused by Update existing
let allFrames = false; // the user allowed capturing embedded frames (settings)

/** Runs inside the page: frames, embeds and objects in the top document. */
function countFrames() {
  return document.querySelectorAll("iframe, frame, embed, object").length;
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
    const from = captured.frames > 1 ? ` from ${captured.frames} frames` : "";
    show("ok", "Saved: ", docLink(d.doc_id, `doc ${d.doc_id}`), ` with ${d.chunks} chunks${from}.${note}`);
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
  const res = await KB.sendWithRetry(
    () => KB.api(settings, "POST", "/api/docs", body),
    (attempt, waitMs, reason) => show("note", `The server did not answer (${reason}); retry ${attempt} in ${waitMs / 1000} s...`),
  );
  if (res.savedEarlier) {
    return show("ok", "Saved: ", docLink(res.data.doc_id, `doc ${res.data.doc_id}`), " (the first answer was lost on the way; a retry found it stored).");
  }
  showUpload(res);
}

async function updateExisting(docId) {
  const res = await KB.sendWithRetry(
    () => KB.api(settings, "PATCH", `/api/docs/${Number(docId)}`, { content: captured.html, format: "html" }),
    (attempt, waitMs, reason) => show("note", `The server did not answer (${reason}); retry ${attempt} in ${waitMs / 1000} s...`),
  );
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
    if (err && err.name === "RetriesExhausted") {
      show("error", err.message + " It may still have been stored: check the web UI before capturing again.");
    } else {
      show("error", "Could not reach the server. Check the URL in settings.");
    }
  } finally {
    busy(false);
  }
}

async function capture() {
  const selectionOnly = !$("sel-row").hidden && $("sel").checked;
  const grab = (all) => chrome.scripting.executeScript({
    target: { tabId: tab.id, allFrames: all },
    func: grabPage,
    args: [{ selectionOnly }],
  });
  let results;
  try {
    results = await grab(allFrames);
  } catch (err) {
    if (!allFrames) throw err;
    results = await grab(false); // a frame Chrome refused should not cost the whole page
  }
  captured = combineFrames(results, selectionOnly, $("title").value.trim());
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
  showCanvasLink(tab);
  allFrames = await KB.hasFrameAccess();
  try {
    const sels = await chrome.scripting.executeScript({
      target: { tabId: tab.id, allFrames },
      func: () => (getSelection() ? getSelection().toString().trim().length : 0),
    });
    $("sel-row").hidden = !sels.some((r) => r.result > 0);
    if (!allFrames) {
      const [{ result: n }] = await chrome.scripting.executeScript({
        target: { tabId: tab.id },
        func: countFrames,
      });
      if (n > 0) {
        show("note", `This page has ${n} embedded frame${n > 1 ? "s" : ""} that will not be captured. ` +
          "Tick \"Capture embedded frames\" in settings to include them.");
      }
    }
  } catch {
    $("capture").disabled = true;
    show("warn", "This page cannot be captured (browser pages and the Web Store are blocked).");
  }
}

/** On a Canvas course page, offer the modules mode (it opens in its own tab). */
function showCanvasLink(current) {
  let url;
  try {
    url = new URL(current.url);
  } catch {
    return;
  }
  const m = url.protocol === "https:" && url.pathname.match(/^\/courses\/(\d+)(\/|$)/);
  if (!m) return;
  $("canvas-row").hidden = false;
  $("canvas").addEventListener("click", () => {
    const q = new URLSearchParams({ tab: String(current.id), origin: url.origin, course: m[1] });
    chrome.tabs.create({ url: chrome.runtime.getURL("modules.html?" + q) });
    window.close();
  });
}

$("form").addEventListener("submit", (e) => {
  e.preventDefault();
  run(capture);
});
init();
