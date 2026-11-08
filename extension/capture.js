"use strict";

/**
 * Page capture shared by the popup and the Canvas modules page (plain script, no build).
 * `grabPage` runs inside each frame through chrome.scripting.executeScript, which
 * serializes it, so it must not use anything outside its own body.
 */

/**
 * Capture one frame. Options:
 *   selectionOnly  send the selection when the frame has one
 *   scroll         scroll the frame from top to bottom first, collecting viewer text on
 *                  the way (viewers only draw the pages near the screen, and often drop
 *                  pages that scrolled far away)
 * Returns {html, body, url, top, selection, text, layer, links, pdfs}: `links` are the
 * http(s) links seen in the frame, `pdfs` the addresses of embedded or linked PDFs.
 */
async function grabPage(opts) {
  const { selectionOnly = false, scroll = false } = opts || {};
  const MIN_PIECES = 20;
  const MAX_STEPS = 80;
  const top = window === window.top;
  const links = new Set();
  const pdfs = new Set();
  const isPdf = (u) => /\.pdf($|[?#])/i.test(u);

  const sel = getSelection();
  if (selectionOnly && sel && sel.rangeCount && sel.toString().trim()) {
    const box = document.createElement("div");
    for (let i = 0; i < sel.rangeCount; i++) box.appendChild(sel.getRangeAt(i).cloneContents());
    collectResources(box);
    return result(box.outerHTML, box.innerHTML, 1, { selection: true });
  }
  const body = document.body;
  if (!body) return result("", "", 0);

  // Viewer text pieces, keyed by position and text so a piece seen twice counts once.
  const pieces = new Map();
  let firstView = null;
  const scroller = scroll ? findScroller() : null;
  if (scroller) {
    scroller.scrollTop = 0;
    await sleep(800);
    for (let step = 0; step < MAX_STEPS; step++) {
      collectPieces(body, scroller);
      collectResources(body);
      if (firstView === null) firstView = viewStats();
      const before = scroller.scrollTop;
      scroller.scrollTop = before + Math.max(200, scroller.clientHeight * 0.8);
      await sleep(700);
      if (scroller.scrollTop <= before + 1) break; // at the bottom
    }
  }
  collectPieces(body, scroller);
  collectResources(body);
  if (firstView === null) firstView = viewStats();

  // A text layer: enough positioned pieces holding most of the text seen on screen.
  const all = [...pieces.values()];
  const chars = all.reduce((n, p) => n + p.text.trim().length, 0);
  if (all.length >= MIN_PIECES && firstView.pieceChars >= 0.6 * firstView.allChars) {
    const layer = buildLayer(all);
    return result(
      "<!doctype html><html><head><title></title></head><body>" + layer + "</body></html>",
      layer,
      chars,
      { layer: true },
    );
  }
  return result(document.documentElement.outerHTML, body.innerHTML, body.innerText.trim().length);

  function result(html, bodyHtml, text, extra) {
    return {
      html,
      body: bodyHtml,
      url: location.href,
      top,
      selection: false,
      text,
      layer: false,
      links: [...links].slice(0, 1000),
      pdfs: [...pdfs].slice(0, 200),
      ...extra,
    };
  }

  function sleep(ms) {
    return new Promise((resolve) => setTimeout(resolve, ms));
  }

  /** The element that scrolls the most content (a viewer's page list), else the document. */
  function findScroller() {
    let best = document.scrollingElement;
    let bestExtra = best ? best.scrollHeight - best.clientHeight : 0;
    for (const node of body.querySelectorAll("*")) {
      const extra = node.scrollHeight - node.clientHeight;
      if (extra <= bestExtra + 50 || node.clientHeight < 100) continue;
      const overflow = getComputedStyle(node).overflowY;
      if (overflow === "auto" || overflow === "scroll") {
        best = node;
        bestExtra = extra;
      }
    }
    return best;
  }

  /** How much of the visible text is in positioned pieces (decides "is a viewer"). */
  function viewStats() {
    let pieceChars = 0;
    for (const p of pieces.values()) pieceChars += p.text.trim().length;
    const allChars = (body.innerText || body.textContent || "").replace(/\s/g, "").length;
    return { pieceChars, allChars };
  }

  /** Links in the frame, and PDFs that are embedded or linked. */
  function collectResources(root) {
    for (const a of root.querySelectorAll("a[href]")) {
      const href = a.href;
      if (!/^https?:/.test(href)) continue;
      const bare = href.split("#")[0];
      if (bare === location.href.split("#")[0]) continue; // a jump within this page
      links.add(bare);
      if (isPdf(bare)) pdfs.add(bare);
    }
    for (const node of root.querySelectorAll("embed[src], object[data], iframe[src]")) {
      const src = node.src || node.data;
      if (!/^https?:/.test(src || "")) continue;
      if (/pdf/i.test(node.type || "") || isPdf(src)) pdfs.add(src);
    }
  }

  /**
   * Viewers (PDF.js, Canvas DocViewer and the like) draw each page as an image with a
   * transparent layer of absolutely positioned text pieces on top, often split mid-word.
   * Collect those pieces with their position in the scrolled content and the link drawn
   * over them (an empty <a href> box, PDF.js's annotation layer, or an enclosing <a>).
   */
  function collectPieces(root, scrollEl) {
    const pos = (n) => n && getComputedStyle(n).position === "absolute";
    let dx = 0;
    let dy = 0;
    if (scrollEl && scrollEl !== document.scrollingElement) {
      const r = scrollEl.getBoundingClientRect();
      dx = scrollEl.scrollLeft - r.left;
      dy = scrollEl.scrollTop - r.top;
    } else {
      dx = window.scrollX;
      dy = window.scrollY;
    }
    const boxes = [];
    for (const a of root.querySelectorAll("a[href]")) {
      if (!/^https?:/.test(a.href)) continue; // skips javascript: and in-document jumps
      const r = a.getBoundingClientRect();
      if (r.width > 0 && r.height > 0) boxes.push({ href: a.href, r, area: r.width * r.height });
    }
    boxes.sort((x, y) => x.area - y.area); // the smallest box that holds a point wins
    const linkAt = (x, y) => {
      const box = boxes.find((b) => x >= b.r.left && x <= b.r.right && y >= b.r.top && y <= b.r.bottom);
      return box ? box.href : null;
    };
    for (const node of root.querySelectorAll("span, div, a")) {
      if (node.childElementCount) continue;
      const text = node.textContent;
      if (!text || !text.trim()) continue;
      if (!pos(node) && !pos(node.parentElement)) continue;
      const r = node.getBoundingClientRect();
      if (r.width <= 0 || r.height <= 0) continue;
      const own = node.closest("a[href]");
      const href = own && /^https?:/.test(own.href) ? own.href : linkAt(r.left + r.width / 2, r.top + r.height / 2);
      const t = r.top + dy;
      const l = r.left + dx;
      const key = `${Math.round(t)}|${Math.round(l)}|${text}`;
      if (!pieces.has(key)) {
        pieces.set(key, { text, href, top: t, left: l, right: r.right + dx, h: r.height, mid: t + r.height / 2 });
      }
    }
  }

  /** Rebuild lines and paragraphs from the pieces; returns paragraphs (with links) as HTML. */
  function buildLayer(list) {
    // Text is kept as runs of {text, href}; a run continues while the link stays the same.
    const add = (runs, text, href) => {
      const last = runs[runs.length - 1];
      if (last && last.href === href) last.text += text;
      else runs.push({ text, href });
    };
    const endsWith = (runs, re) => runs.length > 0 && re.test(runs[runs.length - 1].text);
    // A space between two pieces of the same link stays inside the link.
    const gapHref = (runs, next) => (runs.length && runs[runs.length - 1].href === next ? next : null);

    // Lines: pieces whose vertical centres are within half a line height, left to right.
    list.sort((a, b) => a.mid - b.mid || a.left - b.left);
    const lines = [];
    for (const p of list) {
      const line = lines[lines.length - 1];
      if (line && Math.abs(p.mid - line.mid) < 0.5 * Math.min(p.h, line.h)) line.items.push(p);
      else lines.push({ mid: p.mid, h: p.h, items: [p] });
    }
    for (const line of lines) {
      line.items.sort((a, b) => a.left - b.left);
      const runs = [];
      let prev = null;
      for (const p of line.items) {
        const text = p.text.replace(/\s+/g, " ");
        // A visible gap between pieces is a word break; touching pieces are one word.
        if (prev && p.left - prev.right > 0.15 * p.h && !endsWith(runs, /\s$/) && !/^\s/.test(text)) {
          add(runs, " ", gapHref(runs, p.href));
        }
        add(runs, runs.length ? text : text.trimStart(), p.href);
        prev = p;
      }
      if (runs.length) runs[runs.length - 1].text = runs[runs.length - 1].text.trimEnd();
      line.runs = runs.filter((r) => r.text);
      line.left = line.items[0].left;
      line.right = line.items[line.items.length - 1].right;
      line.top = Math.min(...line.items.map((p) => p.top));
    }

    // Paragraphs: a new one after a large vertical gap or after a line that stops well
    // short of the right edge of the text (end of a paragraph or of a list entry).
    const steps = [];
    for (let i = 1; i < lines.length; i++) {
      const d = lines[i].top - lines[i - 1].top;
      if (d > 0) steps.push(d);
    }
    steps.sort((a, b) => a - b);
    const spacing = steps.length ? steps[Math.floor(steps.length / 2)] : 0;
    const textLeft = Math.min(...lines.map((l) => l.left));
    const textRight = Math.max(...lines.map((l) => l.right));
    const width = textRight - textLeft;

    const paras = [];
    let cur = [];
    let prevLine = null;
    for (const line of lines) {
      if (!line.runs.length) continue;
      const breakHere =
        prevLine &&
        (line.top - prevLine.top > 1.6 * spacing || prevLine.right < textRight - 0.25 * width);
      if (breakHere && cur.length) {
        paras.push(cur);
        cur = [];
      }
      // A line ending in a hyphen continues the word on the next line.
      if (cur.length && !endsWith(cur, /-$/)) add(cur, " ", gapHref(cur, line.runs[0].href));
      for (const r of line.runs) add(cur, r.text, r.href);
      prevLine = line;
    }
    if (cur.length) paras.push(cur);

    const out = document.createElement("div");
    for (const runs of paras) {
      const p = document.createElement("p");
      for (const r of runs) {
        if (!r.href) {
          p.append(r.text);
          continue;
        }
        // The server's HTML cleaner (trafilatura) drops a paragraph that is one link of
        // more than 100 characters as boilerplate, so a long link goes out as two links
        // to the same address, split at the space nearest the middle.
        let parts = [r.text];
        if (r.text.length > 100) {
          const mid = r.text.length / 2;
          let cut = -1;
          for (let i = r.text.indexOf(" "); i !== -1; i = r.text.indexOf(" ", i + 1)) {
            if (cut === -1 || Math.abs(i - mid) < Math.abs(cut - mid)) cut = i;
          }
          if (cut > 0) parts = [r.text.slice(0, cut), r.text.slice(cut + 1)];
        }
        parts.forEach((text, i) => {
          if (i) p.append(" ");
          const a = document.createElement("a");
          a.href = r.href;
          a.textContent = text;
          p.append(a);
        });
      }
      out.append(p);
    }
    return out.innerHTML;
  }
}

/**
 * Turn the per-frame results of `grabPage` into one HTML document: the top frame first,
 * then each embedded frame that has text, each in its own <section>. One frame is sent
 * as it is. Links and PDFs of all frames are merged.
 */
function combineFrames(results, selectionOnly, title) {
  let frames = results
    .filter((r) => r && r.result)
    .sort((a, b) => a.frameId - b.frameId)
    .map((r) => r.result);
  if (!frames.length) return { html: "", url: "", frames: 0, text: 0, links: [], pdfs: [] };
  if (selectionOnly && frames.some((f) => f.selection)) frames = frames.filter((f) => f.selection);
  const top = frames.find((f) => f.top) || frames[0];
  const withText = frames.filter((f) => f === top || f.text > 0);
  const links = [...new Set(withText.flatMap((f) => f.links || []))];
  const pdfs = [...new Set(frames.flatMap((f) => f.pdfs || []))];
  const text = withText.reduce((n, f) => n + (f.text || 0), 0);
  if (withText.length === 1) {
    return { html: withText[0].html, url: top.url, frames: 1, text, links, pdfs };
  }
  const doc = document.implementation.createHTMLDocument(title || "");
  for (const f of withText) {
    const section = doc.createElement("section");
    section.dataset.frameUrl = f.url;
    section.innerHTML = f.body; // inert document: nothing here runs
    doc.body.append(section);
  }
  return { html: doc.documentElement.outerHTML, url: top.url, frames: withText.length, text, links, pdfs };
}
