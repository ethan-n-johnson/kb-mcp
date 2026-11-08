# KB Capture (Chrome extension)

Sends the page you are looking at, or just your selection, to your kb-mcp server (`POST /api/docs`). It captures one page when you click. The Canvas modules mode (below) works through a course's modules, but only while its page is open; the extension has no background activity. Useful for pages behind a login that the server's `ingest_url` cannot fetch.

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
4. Optional: tick **Capture embedded frames** to also capture frames inside the page (for example a course tool or document viewer embedded in Canvas). This needs access to all https sites, so Chrome's prompt on Save asks for that as well. Untick it and Save to give that access back.
5. Click **Save**. Chrome shows a prompt such as "Allow KB Capture to read and change your data on d1234abcd.cloudfront.net?" Click **Allow**. This grants access to that one origin only, nothing else. Saving a new URL removes the grant for the old one.
6. Click **Test**. It calls `GET /healthz` and shows the mode (`semantic` or `keyword`) and the chunk count. Test does not check the token; a wrong token shows up on the first capture.

When `kb-app` is recreated the CloudFront domain changes: open settings, enter the new URL, Save, Allow.

## Capture

1. Open the page, click the icon. The title is prefilled from the tab; edit it if you like and add comma-separated tags.
2. If text is selected on the page, a **Selection only** box appears; tick it to send just the selection. With embedded frames on, a selection inside a frame counts too.
   Without embedded frames, the popup says how many frames the page has that will not be captured.
3. Click **Capture**. Results:
   - **Saved:** link to the new doc in the web UI.
   - **Already saved:** the exact same content exists; the link points to it.
   - **Possible duplicate:** a list of similar docs with a similarity percentage. **Update existing** replaces that doc's content with this capture (the same page, newer). **Save anyway** stores it as a new doc.
   - **Error:** the server's message. A rejected token points you to settings.

The server cleans the HTML with the same pipeline as `ingest_url` (trafilatura) and uses the page URL as the source URL. The `#fragment` is dropped.

## Canvas modules mode

On any page of a Canvas course (`https://<canvas>/courses/<id>/...`), the popup shows **Capture Canvas modules**. It opens a page in a new tab that:

1. Reads the course and its modules through the Canvas API, inside your Canvas tab, so it uses your signed-in session. Keep that tab open on the course.
2. Lists every module and item. Tick whole modules or single items. Supported: **Page**, **File** (PDF, HTML, Markdown, text), **ExternalUrl** and **ExternalTool**; quizzes, assignments and discussions are listed but greyed out. External tools whose title looks like a lab, quiz, exam or assessment start unticked, because launching them may start a lab session or an attempt.
3. On **Start**, stores each ticked item, one at a time:
   - **Page:** the page body as HTML, with the page title as heading.
   - **File:** downloaded and uploaded (PDFs as base64 JSON up to 14 MB so the source URL is kept, larger ones as multipart; 20 MB at most).
   - **ExternalUrl:** fetched by the server (`POST /api/ingest`). If the server cannot fetch it (a login page, for example), it is opened and captured in the browser instead.
   - **ExternalTool:** launched through Canvas's sessionless launch URL (falling back to the module item page) in a separate, unfocused window that is reused for every tool and closed at the end. After the page settles, every frame is scrolled from top to bottom while viewer text is collected, because viewers only draw the pages near the screen and drop the ones far away. Then it is captured like a normal capture with embedded frames, links included. A launch that ends on a PDF stores the PDF.
4. Queues what the captured content points to: embedded or linked **PDFs** and **Canvas files** always; with **Also store the web pages linked from captured content**, every other web link too (at most 300 per run; Canvas's own pages and the knowledge base are skipped), each fetched by the server.
5. Retries a write when its answer is lost on the way (HTTP 502, 503 or 504 from CloudFront or the load balancer, a timeout, or a dropped connection), after 20, 40 and 60 seconds. A slow upload can outlive the 60 second proxy timeout while the server still stores it, so a retry that comes back as a duplicate is reported as saved. After a retry the run pauses 15 seconds before the next item. The popup's Capture and Update existing retry the same way.
6. Shows a log row per item with a link to the stored doc. Exact duplicates show as "already saved"; near-duplicates are not stored (open the doc and use the popup's **Update existing** if you want the new copy).

Tags: by default `canvas` and the course code, plus the module name (untick to leave it out). **Stop** ends the run after the current item. Opening a tool counts as viewing it in Canvas, the same as clicking it yourself.

The mode needs the same https-wide access as embedded frames; the page asks for it if it is missing.

## Limits

- Browser pages (`chrome://`, the Web Store) cannot be captured; the popup says so.
- The server accepts up to 20 MB per request; very large pages fail with `too_large`.
- Only the top frame is captured unless **Capture embedded frames** is on. With it on, every frame Chrome lets the extension read is captured: the top page first, then each frame that has text, each in its own section of one document. Frames Chrome will not script (Chrome's built-in PDF viewer, `chrome://` and other extensions' pages, plain `http://` frames) are skipped; for a PDF, download it and upload the file (`upload_file` in Claude Code, or the web UI).
- **Document viewers** (PDF.js, Canvas DocViewer and similar) draw each page as an image with a transparent layer of positioned text pieces, often split mid-word. When most of a frame's text is in such pieces, the extension rebuilds lines from their positions on screen (touching pieces join into one word, a visible gap is a space, a line ending in a hyphen continues on the next) and paragraphs (a large vertical gap, or a line that stops well short of the right edge, ends one). Links are kept: each text piece takes the address of the link box drawn over it (or of an `<a>` around it), and neighbouring pieces with the same address become one link, also across a line break, so the stored doc has Markdown links. A link longer than 100 characters is sent as two links to the same address, because the server's HTML cleaner drops a paragraph that is one long link. Only `http(s)` links count; viewers that open links with script instead of `href` lose them. Viewers often only draw the pages near the screen: scroll through the whole document before capturing, or the pages not yet drawn are missing.
- An LMS tool that says it "needs to be loaded in a new browser window" is not on the page at all: open it in its window and capture there.
- The page is captured as rendered right now, including content loaded by scripts.

## Permissions

| Permission | Why |
| --- | --- |
| `activeTab` | Read the page you click the icon on, only at that moment. |
| `scripting` | Run the small capture function in that tab. |
| `storage` | Keep the server URL and token in this browser profile. |
| optional host: `https://*.cloudfront.net/*`, `http://localhost:8000/*` | Allow talking to your server. Chrome only grants the exact origin you save in settings. |
| optional host: `https://*/*` | Only when **Capture embedded frames** is ticked or the Canvas modules mode is used: read frames from other sites inside the page you capture, call the Canvas API in your Canvas tab, open and read course tools in the capture window, and download course files. The extension still runs only when you click Capture or Start; it has no content scripts or background page. |

No `<all_urls>`, no remote code, no inline scripts. The https-wide grant is optional and off until you tick embedded frames.
