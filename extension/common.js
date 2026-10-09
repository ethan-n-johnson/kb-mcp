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

  /** Build a DOM element; children are nodes or strings (always inserted as text). */
  function el(tag, props, ...children) {
    const node = document.createElement(tag);
    Object.assign(node, props || {});
    node.append(...children);
    return node;
  }

  return { normalizeOrigin, getSettings, hasAccess, api, el };
})();
