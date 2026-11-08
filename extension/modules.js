"use strict";

/**
 * Canvas modules mode. Reads a course's modules through the Canvas API (run inside the
 * Canvas tab, so it uses the signed-in session), lets the user pick items, then stores
 * each one in the knowledge base:
 *   Page          the page body (Canvas API), as HTML
 *   File          the file (PDF, HTML, Markdown, text), downloaded and uploaded
 *   ExternalUrl   fetched by the server (POST /api/ingest); if that fails, opened in the
 *                 job window and captured there
 *   ExternalTool  launched in the job window (sessionless launch URL), scrolled and
 *                 captured with every frame, viewer text and links included
 * PDFs and Canvas files found along the way are queued too, and optionally every other
 * web link. Runs only while this page is open; nothing runs in the background.
 */

const $ = (id) => document.getElementById(id);
const { el } = KB;

const params = new URLSearchParams(location.search);
const canvasTab = Number(params.get("tab"));
const canvasOrigin = params.get("origin") || "";
const courseId = params.get("course") || "";

const RISKY = /\b(labs?|quiz(zes)?|exams?|assessments?|knowledge check|test|survey|feedback|challenge|submission)\b/i;
const MAX_LINKS = 300;
const JSON_PDF_LIMIT = 14 * 1024 * 1024; // base64 grows by a third; the server takes 20 MB per request
const FILE_LIMIT = 20 * 1024 * 1024;
const FILE_TYPES = { pdf: "pdf", html: "html", htm: "html", md: "markdown", markdown: "markdown", txt: "markdown" };

let settings = { serverUrl: "", token: "" };
let modules = [];
let stopped = false;
let job = null; // {windowId, tabId} of the capture window

function say(kind, text) {
  $("status").className = kind;
  $("status").textContent = text;
}

const sleep = (ms) => new Promise((resolve) => setTimeout(resolve, ms));
const slug = (s) => String(s).toLowerCase().replace(/[^a-z0-9]+/g, "-").replace(/^-|-$/g, "").slice(0, 60);

// --- Canvas API, run inside the Canvas tab --------------------------------------------

/** GET a Canvas API path in the Canvas tab; returns {data, next}. */
async function canvasGet(path) {
  const [{ result }] = await chrome.scripting.executeScript({
    target: { tabId: canvasTab },
    func: async (p) => {
      try {
        const r = await fetch(p, { headers: { Accept: "application/json" }, credentials: "same-origin" });
        const m = (r.headers.get("Link") || "").match(/<([^>]+)>;\s*rel="next"/);
        return { status: r.status, text: await r.text(), next: m ? m[1] : null };
      } catch (err) {
        return { status: 0, text: String(err), next: null };
      }
    },
    args: [path],
  });
  if (!result || result.status !== 200) {
    throw new Error(`Canvas answered HTTP ${result ? result.status : "?"} for ${path.split("?")[0]}`);
  }
  // Canvas prefixes JSON with "while(1);" against JSON hijacking.
  return { data: JSON.parse(result.text.replace(/^while\(1\);/, "")), next: result.next };
}

async function canvasAll(path) {
  const out = [];
  let next = path;
  for (let page = 0; next && page < 50; page++) {
    const { data, next: following } = await canvasGet(next);
    out.push(...data);
    next = following;
  }
  return out;
}

// --- knowledge base uploads ---------------------------------------------------------

/** Map a server answer to a log result. */
function outcome(res) {
  const d = res.data || {};
  if (res.status === 201 && d.status === "created") return { kind: "ok", docId: d.doc_id, text: `saved, ${d.chunks} chunks` };
  if (res.status === 409 && d.error === "duplicate") return { kind: "dup", docId: d.doc_id, text: "already saved" };
  if (res.ok && d.status === "possible_duplicate") {
    const c = (d.candidates || [])[0];
    return { kind: "near", docId: c && c.doc_id, text: `possible duplicate${c ? ` (${Math.round(c.similarity * 100)}%)` : ""}, not saved` };
  }
  if (res.status === 401) throw new Error("The server rejected the token. Check it in settings.");
  return { kind: "error", text: d.error ? `${d.error}${d.detail ? ": " + d.detail : ""}` : `HTTP ${res.status}` };
}

let currentCell = null; // the log cell of the item in progress, for retry notes
let struggling = false; // a write needed a retry: give the server a pause before the next item

/** Send a write with retries (KB.sendWithRetry) and map the answer to a log result. */
async function write(send) {
  const res = await KB.sendWithRetry(send, (attempt, waitMs, reason) => {
    struggling = true;
    if (currentCell) currentCell.textContent = `${reason}; retry ${attempt} in ${waitMs / 1000} s...`;
  });
  if (res.savedEarlier) {
    return { kind: "ok", docId: res.data.doc_id, text: "saved (the first answer was lost on the way; a retry found it stored)" };
  }
  return outcome(res);
}

function postHtml(html, title, tags, sourceUrl) {
  const body = { content: html, format: "html", tags };
  if (title) body.title = title.slice(0, 500);
  if (/^https?:\/\//.test(sourceUrl || "")) body.source_url = sourceUrl.split("#")[0].slice(0, 2000);
  return write(() => KB.api(settings, "POST", "/api/docs", body));
}

function toBase64(buf) {
  const bytes = new Uint8Array(buf);
  let s = "";
  for (let i = 0; i < bytes.length; i += 0x8000) s += String.fromCharCode(...bytes.subarray(i, i + 0x8000));
  return btoa(s);
}

/** Upload downloaded bytes: JSON (keeps the source URL) when small enough, else multipart. */
async function postBytes(buf, fmt, filename, title, tags, sourceUrl) {
  if (fmt === "pdf" && buf.byteLength <= JSON_PDF_LIMIT) {
    const body = { content: toBase64(buf), format: "pdf", tags };
    if (title) body.title = title.slice(0, 500);
    if (/^https?:\/\//.test(sourceUrl || "")) body.source_url = sourceUrl.split("#")[0].slice(0, 2000);
    return write(() => KB.api(settings, "POST", "/api/docs", body, 180000));
  }
  if (fmt !== "pdf") {
    const text = new TextDecoder().decode(buf);
    if (fmt === "html") return postHtml(text, title, tags, sourceUrl);
    const body = { content: text, format: "markdown", tags };
    if (title) body.title = title.slice(0, 500);
    if (/^https?:\/\//.test(sourceUrl || "")) body.source_url = sourceUrl.split("#")[0].slice(0, 2000);
    return write(() => KB.api(settings, "POST", "/api/docs", body));
  }
  const form = new FormData();
  form.append("file", new Blob([buf], { type: "application/pdf" }), filename.endsWith(".pdf") ? filename : filename + ".pdf");
  if (title) form.append("title", title.slice(0, 500));
  form.append("tags", tags.join(","));
  return write(async () => {
    const res = await fetch(settings.serverUrl + "/api/docs/file", {
      method: "POST",
      headers: { Authorization: "Bearer " + settings.token, Accept: "application/json" },
      body: form,
      signal: AbortSignal.timeout(180000),
    });
    let data = {};
    try {
      data = await res.json();
    } catch {
      /* not JSON */
    }
    return { ok: res.ok, status: res.status, data };
  });
}

/** Download a file (with the browser's cookies for that site). */
async function download(url) {
  const res = await fetch(url, { credentials: "include", signal: AbortSignal.timeout(120000) });
  if (!res.ok) throw new Error(`download failed: HTTP ${res.status}`);
  const size = Number(res.headers.get("Content-Length") || 0);
  if (size > FILE_LIMIT) throw new Error("file is over the 20 MB upload limit");
  const buf = await res.arrayBuffer();
  if (buf.byteLength > FILE_LIMIT) throw new Error("file is over the 20 MB upload limit");
  return { buf, type: (res.headers.get("Content-Type") || "").toLowerCase(), finalUrl: res.url || url };
}

/** The upload format of a file, from its name or type; null when the server cannot take it. */
function fileFormat(name, type) {
  const ext = (String(name).toLowerCase().match(/\.([a-z0-9]+)(?:$|[?#])/) || [])[1];
  if (ext && FILE_TYPES[ext]) return FILE_TYPES[ext];
  if (type.includes("pdf")) return "pdf";
  if (type.includes("html")) return "html";
  if (type.includes("markdown") || type.startsWith("text/plain")) return "markdown";
  return null;
}

// --- the capture window -----------------------------------------------------------

/** Wait until the job tab has finished loading and stayed loaded for a moment. */
async function settle(tabId) {
  const deadline = Date.now() + 60000;
  let quietSince = 0;
  while (Date.now() < deadline) {
    const t = await chrome.tabs.get(tabId);
    if (t.status === "complete") {
      if (!quietSince) quietSince = Date.now();
      if (Date.now() - quietSince > 3000) return t; // launches redirect through several pages
    } else {
      quietSince = 0;
    }
    await sleep(500);
  }
  return chrome.tabs.get(tabId);
}

async function openInJob(url) {
  if (job) {
    try {
      await chrome.tabs.update(job.tabId, { url });
      return settle(job.tabId);
    } catch {
      job = null; // the user closed the window
    }
  }
  const win = await chrome.windows.create({ url, focused: false, width: 1280, height: 900 });
  job = { windowId: win.id, tabId: win.tabs[0].id };
  return settle(job.tabId);
}

async function closeJob() {
  if (!job) return;
  try {
    await chrome.windows.remove(job.windowId);
  } catch {
    /* already closed */
  }
  job = null;
}

/** Open a URL in the job window, scroll and capture every frame. */
async function captureInJob(url, title, tags, sourceUrl, queue) {
  const t = await openInJob(url);
  await sleep(2000); // tools often draw their content after the load event
  let results = [];
  try {
    results = await chrome.scripting.executeScript({
      target: { tabId: job.tabId, allFrames: true },
      func: grabPage,
      args: [{ scroll: true }],
    });
  } catch {
    /* the page itself cannot be scripted, for example Chrome's PDF viewer */
  }
  const finalUrl = (t && t.url) || url;
  const captured = combineFrames(results, false, title);
  enqueueFound(queue, captured, tags);
  if (/\.pdf($|[?#])/i.test(finalUrl)) return storeFile(finalUrl, title, tags, sourceUrl); // launch ended on a PDF
  if (!captured.frames) {
    if (captured.pdfs.length) return { kind: "skip", text: "page could not be read; its PDFs are queued" };
    return { kind: "error", text: `Chrome would not let the extension read ${new URL(finalUrl).host}` };
  }
  if (captured.text < 200) {
    const pdfNote = captured.pdfs.length ? "; its PDFs are queued" : "";
    return { kind: "skip", text: `almost no text found (${captured.text} characters)${pdfNote}` };
  }
  const res = await postHtml(captured.html, title, tags, sourceUrl);
  if (captured.frames > 1 && res.kind === "ok") res.text += `, ${captured.frames} frames`;
  return res;
}

// --- item handlers ---------------------------------------------------------------

async function storeFile(url, title, tags, sourceUrl) {
  const { buf, type, finalUrl } = await download(url);
  const name = decodeURIComponent((new URL(finalUrl).pathname.split("/").pop() || "file").slice(0, 200));
  const fmt = fileFormat(name, type) || fileFormat(url, type);
  if (!fmt) return { kind: "skip", text: `not a type the server stores (${type || "unknown"})` };
  return postBytes(buf, fmt, name, title || name, tags, sourceUrl || url);
}

async function storeCanvasFile(fileId, title, tags, sourceUrl) {
  const { data: f } = await canvasGet(`/api/v1/files/${Number(fileId)}`);
  const fmt = fileFormat(f.filename || f.display_name || "", (f["content-type"] || "").toLowerCase());
  if (!fmt) return { kind: "skip", text: `not a type the server stores (${f["content-type"] || f.display_name})` };
  if (f.size > FILE_LIMIT) return { kind: "skip", text: "file is over the 20 MB upload limit" };
  const { buf } = await download(f.url);
  return postBytes(buf, fmt, f.filename || f.display_name, title || f.display_name, tags, sourceUrl || f.url);
}

async function storePage(item, tags, queue) {
  const { data: page } = await canvasGet(item.url);
  const doc = document.implementation.createHTMLDocument(page.title || item.title);
  const h1 = doc.createElement("h1");
  h1.textContent = page.title || item.title;
  const content = doc.createElement("div");
  content.innerHTML = page.body || ""; // inert document: nothing here runs
  doc.body.append(h1, content);
  const links = new Set();
  const pdfs = new Set();
  for (const a of content.querySelectorAll("a[href]")) {
    let href;
    try {
      href = new URL(a.getAttribute("href"), canvasOrigin).href;
    } catch {
      continue;
    }
    if (!/^https?:/.test(href)) continue;
    links.add(href.split("#")[0]);
    if (/\.pdf($|[?#])/i.test(href)) pdfs.add(href);
  }
  for (const node of content.querySelectorAll("iframe[src], embed[src], object[data]")) {
    const src = node.getAttribute("src") || node.getAttribute("data");
    try {
      const u = new URL(src, canvasOrigin).href;
      if (/pdf/i.test(node.getAttribute("type") || "") || /\.pdf($|[?#])/i.test(u)) pdfs.add(u);
    } catch {
      /* not a URL */
    }
  }
  enqueueFound(queue, { links: [...links], pdfs: [...pdfs] }, tags);
  return postHtml(doc.documentElement.outerHTML, page.title || item.title, tags, page.html_url || item.html_url);
}

async function storeTool(item, tags, queue) {
  let launch = item.html_url;
  if (item.url) {
    try {
      const { data } = await canvasGet(item.url); // sessionless launch: a one-time URL
      if (data && data.url) launch = data.url;
    } catch {
      /* fall back to the module item page */
    }
  }
  return captureInJob(launch, item.title, tags, item.html_url, queue);
}

async function storeExternalUrl(item, tags, queue) {
  const res = await write(() => KB.api(settings, "POST", "/api/ingest", { url: item.external_url, tags }, 180000));
  if (res.kind !== "error") return res;
  // The server could not fetch it (a login page, a blocked site): capture it in the browser.
  const viaTab = await captureInJob(item.external_url, item.title, tags, item.external_url, queue);
  if (viaTab.kind === "ok") viaTab.text += " (captured in the browser; the server could not fetch it)";
  return viaTab;
}

async function storeLink(url, tags) {
  return write(() => KB.api(settings, "POST", "/api/ingest", { url, tags }, 180000));
}

// --- queue -----------------------------------------------------------------------

const seen = new Set();
let linkCount = 0;

/** Queue the PDFs, Canvas files and (when asked) web links found in captured content. */
function enqueueFound(queue, found, tags) {
  const canvasFile = (u) => {
    try {
      const url = new URL(u);
      if (url.origin !== canvasOrigin) return null;
      const m = url.pathname.match(/\/files\/(\d+)/);
      return m ? m[1] : null;
    } catch {
      return null;
    }
  };
  for (const u of found.pdfs || []) {
    const id = canvasFile(u);
    const key = id ? "file:" + id : "url:" + u;
    if (seen.has(key)) continue;
    seen.add(key);
    queue.push(id ? { kind: "Canvas file", title: `file ${id}`, tags, run: () => storeCanvasFile(id, null, tags) }
      : { kind: "PDF", title: u, tags, run: () => storeFile(u, null, tags) });
  }
  for (const u of found.links || []) {
    const id = canvasFile(u);
    if (id) {
      if (seen.has("file:" + id)) continue;
      seen.add("file:" + id);
      queue.push({ kind: "Canvas file", title: `file ${id}`, tags, run: () => storeCanvasFile(id, null, tags) });
      continue;
    }
    if (!$("follow").checked || linkCount >= MAX_LINKS) continue;
    let url;
    try {
      url = new URL(u);
    } catch {
      continue;
    }
    // Canvas's own pages and the knowledge base itself are navigation, not content.
    if (url.origin === canvasOrigin || url.origin === settings.serverUrl) continue;
    if (seen.has("url:" + u)) continue;
    seen.add("url:" + u);
    linkCount++;
    queue.push({ kind: "Link", title: u, tags, run: () => storeLink(u, tags) });
  }
}

function logRow(entry) {
  const result = el("td", { textContent: "queued" });
  const row = el("tr", {}, el("td", { textContent: entry.title }), el("td", { textContent: entry.kind }), result);
  $("log").tBodies[0].append(row);
  return result;
}

function showResult(cell, res) {
  cell.className = { ok: "ok", dup: "note", near: "warn", skip: "note", error: "error" }[res.kind] || "";
  const nodes = [res.text];
  if (res.docId) {
    const a = el("a", { href: `${settings.serverUrl}/docs/${Number(res.docId)}`, target: "_blank", rel: "noopener", textContent: `doc ${res.docId}` });
    nodes.unshift(a, ": ");
  }
  cell.replaceChildren(...nodes);
}

async function start(event) {
  event.preventDefault();
  const baseTags = [...new Set($("tags").value.split(",").map((t) => t.trim()).filter(Boolean))];
  const queue = [];
  seen.clear(); // a new run starts fresh; the server still refuses exact duplicates
  linkCount = 0;
  for (const m of modules) {
    const tags = $("module-tag").checked ? [...baseTags, slug(m.name)].filter(Boolean) : baseTags;
    for (const item of m.items || []) {
      const box = document.getElementById(`item-${item.id}`);
      if (!box || !box.checked) continue;
      const handler = { Page: storePage, File: null, ExternalTool: storeTool, ExternalUrl: storeExternalUrl }[item.type];
      const run = item.type === "File"
        ? () => storeCanvasFile(item.content_id, item.title, tags, item.html_url)
        : () => handler(item, tags, queue);
      if (item.type === "File") seen.add("file:" + item.content_id);
      queue.push({ kind: item.type, title: `${m.name}: ${item.title}`, tags, run });
    }
  }
  if (!queue.length) return say("warn", "Tick at least one item.");

  $("log").tBodies[0].replaceChildren();
  for (const entry of queue) entry.cell = logRow(entry);
  stopped = false;
  $("start").disabled = true;
  $("stop").disabled = false;
  $("log").hidden = false;
  const counts = { ok: 0, dup: 0, near: 0, skip: 0, error: 0 };
  try {
    for (let i = 0; i < queue.length && !stopped; i++) {
      const entry = queue[i];
      const cell = entry.cell || logRow(entry);
      if (struggling) {
        say("note", "The server needed retries; waiting 15 s before the next item...");
        await sleep(15000);
        struggling = false;
      }
      say("note", `Working on ${i + 1} of ${queue.length}: ${entry.title}`);
      cell.textContent = "working...";
      currentCell = cell;
      let res;
      try {
        const before = queue.length;
        res = await entry.run();
        // Rows for what this item found, so the log shows the queue.
        for (let j = before; j < queue.length; j++) queue[j].cell = logRow(queue[j]);
      } catch (err) {
        if (/rejected the token/.test(err.message)) {
          showResult(cell, { kind: "error", text: err.message });
          stopped = true;
          break;
        }
        res = { kind: "error", text: err.message || String(err) };
      }
      counts[res.kind] = (counts[res.kind] || 0) + 1;
      showResult(cell, res);
    }
  } finally {
    await closeJob();
    $("start").disabled = false;
    $("stop").disabled = true;
  }
  const summary = `${counts.ok} saved, ${counts.dup} already saved, ${counts.near} possible duplicates, ${counts.skip} skipped, ${counts.error} failed.`;
  say(counts.error ? "warn" : "ok", (stopped ? "Stopped. " : "Done. ") + summary);
}

// --- module picker ---------------------------------------------------------------

const SUPPORTED = new Set(["Page", "File", "ExternalTool", "ExternalUrl"]);

function renderTree() {
  const tree = $("tree");
  tree.replaceChildren();
  for (const m of modules) {
    const items = (m.items || []).filter((i) => i.type !== "SubHeader");
    const boxes = [];
    const moduleBox = el("input", { type: "checkbox" });
    const list = el("ul", { className: "items" });
    for (const item of items) {
      const ok = SUPPORTED.has(item.type);
      const box = el("input", { type: "checkbox", id: `item-${item.id}`, disabled: !ok });
      box.dataset.risky = String(ok && item.type === "ExternalTool" && RISKY.test(item.title));
      if (ok) boxes.push(box);
      const note = ok ? item.type : `${item.type}, not supported`;
      list.append(el("li", {}, el("label", { className: "check" }, box, `${item.title} `, el("span", { className: "hint", textContent: `(${note})` }))));
    }
    moduleBox.addEventListener("change", () => {
      for (const b of boxes) b.checked = moduleBox.checked && b.dataset.risky !== "true";
    });
    const usable = boxes.length;
    const head = el("label", { className: "check module" }, moduleBox, `${m.name} `, el("span", { className: "hint", textContent: `(${usable} of ${items.length} items supported)` }));
    tree.append(el("details", {}, el("summary", {}, head), list));
  }
}

function tickAll(on) {
  for (const box of document.querySelectorAll("#tree input[type=checkbox]")) {
    if (box.disabled) continue;
    box.checked = on && box.dataset.risky !== "true";
  }
}

async function load() {
  say("note", "Reading the course modules from Canvas...");
  try {
    let courseName = `course ${courseId}`;
    try {
      const { data: course } = await canvasGet(`/api/v1/courses/${courseId}`);
      courseName = course.name || course.course_code || courseName;
      $("tags").value = ["canvas", slug(course.course_code || course.name || courseId)].join(", ");
    } catch {
      $("tags").value = "canvas";
    }
    $("course").textContent = `${courseName} on ${new URL(canvasOrigin).host}`;
    modules = await canvasAll(`/api/v1/courses/${courseId}/modules?include[]=items&per_page=100`);
    // Modules with many items return them separately.
    for (const m of modules) {
      if (!m.items) m.items = await canvasAll(`/api/v1/courses/${courseId}/modules/${m.id}/items?per_page=100`);
    }
    renderTree();
    $("pick").hidden = false;
    say("ok", `${modules.length} modules. Pick what to capture.`);
  } catch (err) {
    say("error", `${err.message}. Keep the Canvas tab open on the course and reload this page.`);
  }
}

async function init() {
  if (!canvasTab || !/^https:\/\//.test(canvasOrigin) || !/^\d+$/.test(courseId)) {
    return say("error", "Open this from the extension's popup on a Canvas course page.");
  }
  settings = await KB.getSettings();
  if (!settings.serverUrl || !settings.token) return say("error", "Set the server URL and write token in settings first.");
  $("all").addEventListener("click", () => tickAll(true));
  $("none").addEventListener("click", () => tickAll(false));
  $("pick").addEventListener("submit", start);
  $("stop").addEventListener("click", () => {
    stopped = true;
    say("note", "Stopping after the current item...");
  });
  window.addEventListener("beforeunload", () => {
    if (job) chrome.windows.remove(job.windowId).catch(() => {});
  });
  if (await KB.hasFrameAccess()) return load();
  $("access").hidden = false;
  $("allow").addEventListener("click", async () => {
    // permissions.request needs this click's user gesture, so it comes first.
    const granted = await chrome.permissions.request({ origins: [KB.FRAMES] });
    if (!granted) return say("warn", "Chrome access was not granted.");
    $("access").hidden = true;
    load();
  });
}

init();
