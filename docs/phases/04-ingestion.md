# Phase 04: Ingestion pipeline

**Day:** 2 (Thu Oct 8) · **Depends on:** 02 · **Where:** local · **PLAN.md sections:** Ingestion and data model (steps 1 to 4); Auth and security (SSRF guard bullet)

## Goal

Pure functions that turn a URL, HTML, PDF or Markdown into clean Markdown and then into heading-based chunks. No database, models or S3 in this phase. The URL fetcher is built with its SSRF guard from the start.

## Read first

- `CLAUDE.md`
- `docs/PLAN.md`: "Ingestion and data model" steps 1 to 4; the SSRF guard bullet in "Auth and security"

## Tasks

1. `kb/ingest/fetch.py`: `fetch_url(url: str) -> FetchResult(final_url, content_type, body: bytes)`.
   - Only `http` and `https`.
   - Resolve the host with `socket.getaddrinfo`; reject if **any** resolved address is private, loopback, link-local, multicast, reserved or unspecified (`ipaddress` module). Handle IPv6 and IPv4-mapped IPv6 (`::ffff:127.0.0.1`). This blocks `169.254.169.254`.
   - Follow redirects manually (max 5), re-running the full check on every hop.
   - Stream the body and abort past 20 MB. Timeout 15 s. Send a descriptive User-Agent.
   - Raise a typed `IngestError(code, message)` for every refusal (`blocked_address`, `bad_scheme`, `too_large`, `fetch_failed`).
   - Note the DNS-rebinding limitation (check then connect) in a code comment; do not solve it.
2. `kb/ingest/convert.py`:
   - `detect_format(*, declared=None, filename=None, content_type=None, url=None) -> "html" | "pdf" | "markdown"`. Declared wins, then extension, then content type. URLs ending in `.md` or served as `text/markdown` or `text/plain` are Markdown.
   - `to_markdown(data: bytes | str, fmt, *, url=None) -> Converted(title, markdown, tags, source_type)`.
     - HTML: `trafilatura.extract(html, output_format="markdown", include_tables=True, include_links=True, url=url)`; get the title from trafilatura metadata, else the first H1, else the URL path. Empty result raises `IngestError("empty_content")`.
     - PDF: open with `pymupdf`, reject over 300 pages (`too_many_pages`), convert with `pymupdf4llm.to_markdown(doc)`. If the text is nearly empty (for example under 100 non-whitespace characters, or under 20 per page), raise `IngestError("no_text_layer", "PDF has no text layer; OCR is not supported")`. Title from PDF metadata, else first heading.
     - Markdown: decode UTF-8; parse YAML front matter for `title` and `tags`, strip it from the body.
   - `clean_markdown(md) -> str`: collapse 3+ blank lines to 1, drop headings with no text, drop lines matching a small regex list (`skip to (main )?content`, `edit this page`, `was this page helpful`, `table of contents`, cookie banners). Keep the list in one constant so phase 11 can extend it.
3. `kb/ingest/chunk.py`: `chunk_markdown(md: str, title: str) -> list[Chunk(ord, heading_path, text, word_count)]`.
   - Split at H1, H2 and H3. **Ignore `#` lines inside fenced code blocks.**
   - `heading_path` joins the active H1 > H2 > H3 titles with `" > "`; text before the first heading gets the doc title as its path.
   - Merge sections under 50 words into the next section (the last one merges into the previous).
   - Split sections over 350 words at paragraph breaks, never inside a code fence; each piece keeps the same `heading_path`.
   - `ord` is 0-based and contiguous.
4. Debug command: `python -m kb.cli convert <path-or-url> [--format F]` prints the title, tags, then each chunk's `ord`, `heading_path` and word count, and the first 80 characters.
5. Tests with fixtures in `tests/fixtures/`:
   - `aws_like.html`: hand-written page with nav, sidebar, cookie banner, footer, a table and two H2 sections. Assert the boilerplate is gone and the table survives.
   - `frontmatter.md`: front matter with title and tags, a fenced code block containing `# not a heading`.
   - A small PDF generated in a fixture with `pymupdf` (two headed sections), plus an image-only PDF that must raise `no_text_layer`.
   - Long section (over 350 words) and tiny sections (under 50 words) to exercise split and merge.
   - SSRF: monkeypatch `getaddrinfo` to return `127.0.0.1`, `10.0.0.5`, `169.254.169.254`, `::1`, `::ffff:127.0.0.1`; all refused. Use `httpx.MockTransport` for a public host that redirects to a private one (refused) and for a body over 20 MB (refused).

## Do not

- Touch the database, embeddings or S3.
- Add a crawler. One URL, one page.

## Acceptance

- [ ] `pytest -q` passes, including all new tests.
- [ ] `python -m kb.cli convert` on any live AWS user guide page (for example an EC2 or EBS page) shows clean chunks with real heading paths and no navigation text. Paste the output summary into the phase log notes.
- [ ] `python -m kb.cli convert http://169.254.169.254/latest/meta-data/` exits with `blocked_address`.
- [ ] Phase log updated; commit `phase 04: ingestion pipeline`.
