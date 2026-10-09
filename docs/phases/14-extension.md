# Phase 14: Chrome extension

**Day:** 6 (Mon Oct 12) · **Depends on:** 13 (uses the same API) · **Where:** local · **PLAN.md sections:** Browser extension; Auth and security (XSS and CORS bullet)

**Cut line 2:** selection-only mode is optional; build it last.

## Goal

A Manifest V3 extension that captures the current page's HTML (or the selection) and posts it to `POST /api/docs`, with duplicate handling and a settings page. Loaded unpacked; no Web Store.

## Read first

- `CLAUDE.md`, `docs/PROGRESS.md` (CloudFront domain)
- `docs/PLAN.md`: "Browser extension"

## Tasks

1. `extension/manifest.json` (MV3): name "KB Capture", `permissions: ["activeTab", "scripting", "storage"]`, `optional_host_permissions: ["https://*.cloudfront.net/*", "http://localhost:8000/*"]`, `action.default_popup: popup.html`, `options_page: options.html`. Because the CloudFront domain changes whenever `kb-app` is recreated, the options page requests permission for the **exact** configured origin with `chrome.permissions.request` when settings are saved. That keeps the granted host permission to "your server URL only", as PLAN.md requires.
2. `options.html` / `options.js`: server URL and write token in `chrome.storage.local`; a Test button that calls `GET <server>/healthz` and shows the mode and chunk count.
3. `popup.html` / `popup.js`:
   - Title prefilled from the active tab; tags input (comma-separated); Capture button.
   - On open, run `chrome.scripting.executeScript` to check for a non-empty selection; if there is one, show a "Selection only" checkbox.
   - Capture runs `executeScript` returning `{html, url}`: `document.documentElement.outerHTML`, or for a selection, the HTML of `getSelection().getRangeAt(0).cloneContents()` wrapped in a div.
   - `POST <server>/api/docs` with JSON `{title, content: html, format: "html", source_url: url, tags, force: false}` and `Authorization: Bearer <token>`.
   - Results: `created` shows a link to `<server>/docs/<id>`; `duplicate` links the existing doc; `possible_duplicate` lists candidates with **Update existing** (`PATCH /api/docs/<id>` with the captured HTML; make sure the PATCH route accepts `format` with content) and **Save anyway** (resend with `force: true`); errors show the message. Missing settings show a link to the options page.
4. Plain JavaScript, no build step, no remote code. Keep the popup under about 200 lines of JS.
5. Server side: confirm `upload` with `format: "html"` passes `source_url` to trafilatura as the `url` hint and stores it. Add the extension ID origin to `KB_EXTENSION_ORIGIN` handling only as a config value now; phase 16 wires CORS.
6. `extension/README.md`: how to load unpacked, set the server URL and token, and the permission prompt to expect.

## Do not

- Request `<all_urls>` or broad host permissions at install time.
- Add crawling or background capture.

## Acceptance

- [ ] **HUMAN:** load unpacked at `chrome://extensions` with no manifest errors; record the extension ID in PROGRESS.md.
- [ ] **HUMAN:** set the server URL (CloudFront, or localhost) and token; Test shows the health info.
- [ ] **HUMAN:** capture a docs page; then search for a phrase from it in the web UI and it is found with the page URL as `source_url`.
- [ ] **HUMAN:** capturing the same page again returns `duplicate`; capturing a near copy shows the duplicate buttons.
- [ ] Phase log updated; commit `phase 14: Chrome extension`.
