"use strict";

/**
 * Helpers shared by the popup and the options page (plain script, no build).
 * Exposes one global, `KB`.
 */
const KB = (() => {
  const LOCALHOST = "http://localhost:8000";

  /**
   * Return the origin of a server URL if the manifest allows it, else null.
   * Allowed: https://<name>.cloudfront.net and http://localhost:8000.
   */
  function normalizeOrigin(raw) {
    let url;
    try {
      url = new URL(String(raw || "").trim());
    } catch {
      return null;
    }
    const cloudfront = url.protocol === "https:" && url.hostname.endsWith(".cloudfront.net");
    return cloudfront || url.origin === LOCALHOST ? url.origin : null;
  }

  /** Read `{serverUrl, token}` from chrome.storage.local (empty strings when unset). */
  async function getSettings() {
    const saved = await chrome.storage.local.get(["serverUrl", "token"]);
    return { serverUrl: saved.serverUrl || "", token: saved.token || "" };
  }

  /** True when the extension holds the host permission for exactly this origin. */
  function hasAccess(origin) {
    return chrome.permissions.contains({ origins: [origin + "/*"] });
  }

  /**
   * Optional host permission that lets a capture read embedded frames from other
   * sites (for example a course tool inside Canvas). Granted only when the user ticks
   * "Capture embedded frames" in settings.
   */
  const FRAMES = "https://*/*";

  /** True when the user has allowed capturing embedded frames. */
  function hasFrameAccess() {
    return chrome.permissions.contains({ origins: [FRAMES] });
  }

  /**
   * Call the server. Resolves to `{ok, status, data}`; `data` is the parsed JSON body
   * (`{error, detail}` on API errors) or `{}` when the body is not JSON.
   * Network failures and timeouts reject.
   */
  async function api(settings, method, path, body, timeoutMs = 90000) {
    const headers = { Accept: "application/json" };
    if (body !== undefined) headers["Content-Type"] = "application/json";
    if (settings.token) headers.Authorization = "Bearer " + settings.token;
    const res = await fetch(settings.serverUrl + path, {
      method,
      headers,
      body: body === undefined ? undefined : JSON.stringify(body),
      signal: AbortSignal.timeout(timeoutMs),
    });
    let data = {};
    try {
      data = await res.json();
    } catch {
      /* not JSON (for example a gateway error page) */
    }
    return { ok: res.ok, status: res.status, data };
  }

  const RETRY_STATUS = new Set([502, 503, 504]);
  const RETRY_WAITS = [20000, 40000, 60000];

  /**
   * Send a write, retrying when the answer was lost on the way: a 502/503/504 from
   * CloudFront or the load balancer, a timeout, or a dropped connection. The server
   * may well have finished the write (a slow upload outlives the 60 s proxy timeout),
   * so a retry that comes back as a duplicate means the first attempt was stored.
   * `send()` returns `{ok, status, data}` like `api`. Resolves to that plus
   * `retried` (number of retries) and `savedEarlier` (true when a retry found the
   * write already stored). `onRetry(attempt, waitMs, reason)` is told before each wait.
   */
  async function sendWithRetry(send, onRetry) {
    let retried = 0;
    for (;;) {
      let res = null;
      let reason = "";
      try {
        res = await send();
        if (!RETRY_STATUS.has(res.status)) {
          const savedEarlier = retried > 0 && res.status === 409 && res.data && res.data.error === "duplicate";
          return { ...res, retried, savedEarlier };
        }
        reason = `HTTP ${res.status}`;
      } catch (err) {
        reason = err && err.name === "TimeoutError" ? "timed out" : "no answer";
      }
      if (retried >= RETRY_WAITS.length) {
        if (res) return { ...res, retried, savedEarlier: false };
        const err = new Error(`The server did not answer (${reason}) after ${retried} retries.`);
        err.name = "RetriesExhausted";
        throw err;
      }
      const wait = RETRY_WAITS[retried];
      retried += 1;
      if (onRetry) onRetry(retried, wait, reason);
      await new Promise((resolve) => setTimeout(resolve, wait));
    }
  }

  /** Build a DOM element; children are nodes or strings (always inserted as text). */
  function el(tag, props, ...children) {
    const node = document.createElement(tag);
    Object.assign(node, props || {});
    node.append(...children);
    return node;
  }

  return { normalizeOrigin, getSettings, hasAccess, hasFrameAccess, FRAMES, api, sendWithRetry, el };
})();
