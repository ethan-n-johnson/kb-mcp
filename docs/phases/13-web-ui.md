# Phase 13: Web UI

**Day:** 6 (Mon Oct 12) · **Depends on:** 08 · **Where:** local, then a redeploy · **PLAN.md sections:** Interfaces (Web UI); Auth and security (XSS bullet)

**Cut line 3:** the maintenance page is built last; drop it if behind (the MCP tools still cover stale, gaps and the toggle).

## Goal

Server-rendered Jinja2 pages with htmx and no build step: search, doc, add, and maintenance. Markdown is sanitized with nh3. Pages call `service.py` directly; writes go to the existing `/api` routes with the token header.

## Read first

- `CLAUDE.md`, `docs/PROGRESS.md`
- `docs/PLAN.md`: "Web UI" bullets under "Interfaces", the XSS bullet in "Auth and security"

## Tasks

1. Static assets in `kb/static/` served at `/static/*` (CloudFront caches this path): vendor a pinned `htmx.min.js` (download a specific 2.x release once and commit it, with the version in the filename), one `app.css`, one `app.js`. **No inline scripts or inline event handlers**, so phase 16 can set a strict Content-Security-Policy.
2. `kb/ui.py` router and `kb/templates/` with a `base.html` (header with nav, a token box, mode badge from health state).
3. Token handling in `app.js`: the token box saves to `sessionStorage` key `kbToken`; an `htmx:configRequest` listener adds `Authorization: Bearer <token>` to every non-GET request. Show "write token set" or "read only" in the header.
4. **Search page** `/`: query box with `hx-get="/ui/search"` (debounced on submit, not keyup); results partial shows title (link to doc page), heading path, snippet, score, and badges for `confidence` (high, medium, low with distinct colors) and `mode`. Low confidence shows a short "No good answer in the knowledge base" note.
5. **Doc page** `/docs/{id}`: title, tags, source link, created, updated, verified date and by whom; rendered Markdown; a Verify button (`hx-post` to `/api/docs/{id}/verify`) that swaps in the new verified date.
6. Markdown rendering helper: `markdown-it-py` (`commonmark` preset plus tables) with raw HTML disabled, then `nh3.clean` with an explicit allow-list (headings, p, a, ul, ol, li, code, pre, blockquote, table elements, em, strong, hr, br, img with `src` and `alt` only). Links get `rel="noopener noreferrer"`. Only `http`, `https` and `mailto` URL schemes.
7. **Add page** `/add` with three forms: paste Markdown (title, tags), upload a file (`/api/docs/file`), or a URL (`/api/ingest`). Render the result partial:
   - `created`: link to the doc.
   - `duplicate`: link to the existing doc.
   - `possible_duplicate`: list candidates with similarity and three buttons: **Update existing** (`PATCH /api/docs/{candidate}` with the new content), **Merge** (save with `force=true`, then `POST /api/docs/merge` with target = candidate, source = new doc), **Save anyway** (resend with `force=true`).
   - errors: the `ErrorOut` message.
8. **Maintenance page** `/maintenance`: stale docs (with Verify buttons), top gaps, the last 20 audit rows (add a read-only `GET /api/audit?limit=20` to `service.py` and REST; it shows token names, never secrets), and the fallback toggle (`POST /api/admin/fallback`).
9. Tests: every page returns 200; search partial renders badges; an uploaded Markdown doc containing `<script>alert(1)</script>`, `<img src=x onerror=alert(1)>` and a `javascript:` link renders with none of them in the HTML; write buttons without a token get 401 from the API (the UI shows the message).

## Do not

- Add a JavaScript framework, bundler or CDN script tags.
- Store the token anywhere except `sessionStorage`.

## Acceptance

- [ ] `pytest -q` passes.
- [ ] **HUMAN:** click through all four pages locally against the seeded `.localdata/kb.db` (or a fresh seed) and confirm search, verify, add (all three inputs), the duplicate flow with the planted file, and the toggle.
- [ ] Redeploy; `https://<cloudfront>/` loads; a second request for `/static/app.css` shows `x-cache: Hit from cloudfront`.
- [ ] Phase log updated; commit `phase 13: web UI`.
