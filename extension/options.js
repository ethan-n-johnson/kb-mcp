"use strict";

const $ = (id) => document.getElementById(id);
const BAD_URL =
  "Use your CloudFront address (https://<name>.cloudfront.net) or http://localhost:8000.";

function say(kind, text) {
  $("status").className = kind;
  $("status").textContent = text;
}

/** Drop host permissions for any origin other than the one now in use (and the frames grant, if kept). */
async function dropOtherOrigins(origin, keepFrames) {
  try {
    const { origins = [] } = await chrome.permissions.getAll();
    const keep = new Set([origin + "/*"]);
    if (keepFrames) keep.add(KB.FRAMES);
    const old = origins.filter((o) => !keep.has(o));
    if (old.length) await chrome.permissions.remove({ origins: old });
  } catch {
    /* best effort: a leftover grant is harmless, it only covers an old address */
  }
}

async function save(event) {
  event.preventDefault();
  const origin = KB.normalizeOrigin($("url").value);
  if (!origin) return say("error", BAD_URL);
  const token = $("token").value.trim();
  const frames = $("frames").checked;

  // Ask first: permissions.request needs the click's user gesture, so no other await before it.
  // One request covers both, so Chrome shows a single prompt.
  const wanted = frames ? [origin + "/*", KB.FRAMES] : [origin + "/*"];
  const granted = await chrome.permissions.request({ origins: wanted });
  await chrome.storage.local.set({ serverUrl: origin, token });
  $("url").value = origin;
  if (!granted) {
    $("frames").checked = await KB.hasFrameAccess();
    return say("warn", "Saved, but Chrome access was not granted. Captures will fail until you save again and allow it.");
  }
  await dropOtherOrigins(origin, frames);
  // Unticking frames removes the https-wide grant; if Chrome counted the server under it,
  // the server's own grant may go with it, and a second Save asks for it alone.
  if (!(await KB.hasAccess(origin))) {
    return say("warn", "Saved. Click Save once more so Chrome can grant access to the server address.");
  }
  say("ok", frames
    ? `Saved. Chrome access: ${origin}, plus https pages when you capture embedded frames.`
    : `Saved. Chrome access is limited to ${origin}.`);
}

async function test() {
  const origin = KB.normalizeOrigin($("url").value);
  if (!origin) return say("error", BAD_URL);
  if (!(await KB.hasAccess(origin))) {
    return say("warn", "Chrome has not granted access to this address yet. Click Save first.");
  }
  say("note", "Testing...");
  try {
    const res = await KB.api({ serverUrl: origin, token: "" }, "GET", "/healthz", undefined, 15000);
    const h = res.data;
    if (!res.ok || h.status !== "ok") {
      return say("error", `The server answered HTTP ${res.status}${h.status ? " (" + h.status + ")" : ""}.`);
    }
    const toggle = h.fallback_toggle ? " Keyword fallback is forced on." : "";
    say("ok", `Server is up. Mode: ${h.mode}. Chunks: ${h.chunks}.${toggle}`);
  } catch {
    say("error", "Could not reach the server. Check the address and that the server is running.");
  }
}

async function init() {
  const saved = await KB.getSettings();
  $("url").value = saved.serverUrl;
  $("token").value = saved.token;
  $("frames").checked = await KB.hasFrameAccess();
  $("form").addEventListener("submit", save);
  $("test").addEventListener("click", test);
}

init();
