# KB Capture (Chrome extension)

Sends the page you are looking at, or just your selection, to your kb-mcp server (`POST /api/docs`). It captures one page when you click; it never crawls and has no background activity. Useful for pages behind a login that the server's `ingest_url` cannot fetch.

Plain JavaScript, no build step, loaded unpacked. It is not on the Web Store.

## Load it

1. Open `chrome://extensions` and switch on **Developer mode** (top right).
2. Click **Load unpacked** and choose this `extension/` folder.
3. The card "KB Capture" should appear with no errors. Copy its **ID** (a 32-letter string); note it in `docs/PROGRESS.md`. Phase 16 uses it as `KB_EXTENSION_ORIGIN=chrome-extension://<id>`.
4. Pin the extension from the puzzle-piece menu so the icon is one click away.

## Configure it

1. Click the icon, then **Open settings** (or right-click the icon, **Options**).
2. **Server URL:** your CloudFront address, for example `https://d1234abcd.cloudfront.net`, or `http://localhost:8000` for a local server. Any path is ignored; only the origin is kept. The ALB address is not accepted because it is plain HTTP and the token would travel unencrypted.
3. **Write token:** the secret part of one entry in `/kb/write-tokens` (for local runs, `devtoken` when started with `KB_WRITE_TOKENS=dev:devtoken`).
4. Click **Save**. Chrome shows a prompt such as "Allow KB Capture to read and change your data on d1234abcd.cloudfront.net?" Click **Allow**. This grants access to that one origin only, nothing else. Saving a new URL removes the grant for the old one.
5. Click **Test**. It calls `GET /healthz` and shows the mode (`semantic` or `keyword`) and the chunk count. Test does not check the token; a wrong token shows up on the first capture.

When `kb-app` is recreated the CloudFront domain changes: open settings, enter the new URL, Save, Allow.

## Capture

1. Open the page, click the icon. The title is prefilled from the tab; edit it if you like and add comma-separated tags.
2. If text is selected on the page, a **Selection only** box appears; tick it to send just the selection.
3. Click **Capture**. Results:
   - **Saved:** link to the new doc in the web UI.
   - **Already saved:** the exact same content exists; the link points to it.
   - **Possible duplicate:** a list of similar docs with a similarity percentage. **Update existing** replaces that doc's content with this capture (the same page, newer). **Save anyway** stores it as a new doc.
   - **Error:** the server's message. A rejected token points you to settings.

The server cleans the HTML with the same pipeline as `ingest_url` (trafilatura) and uses the page URL as the source URL. The `#fragment` is dropped.

## Limits

- Browser pages (`chrome://`, the Web Store) cannot be captured; the popup says so.
- The server accepts up to 20 MB per request; very large pages fail with `too_large`.
- Only the top frame is captured, not content inside iframes.
- The page is captured as rendered right now, including content loaded by scripts.

## Permissions

| Permission | Why |
| --- | --- |
| `activeTab` | Read the page you click the icon on, only at that moment. |
| `scripting` | Run the small capture function in that tab. |
| `storage` | Keep the server URL and token in this browser profile. |
| optional host: `https://*.cloudfront.net/*`, `http://localhost:8000/*` | Allow talking to your server. Chrome only grants the exact origin you save in settings. |

No `<all_urls>`, no remote code, no inline scripts.
