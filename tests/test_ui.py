"""Web UI: pages, fragments, token handling, the add flows and HTML safety.

Everything runs through the real app with the fake models. The browser side
(`app.js`, htmx) cannot run here; the manual click-through is deferred to the
laptop. These tests pin what the server sends.
"""

from __future__ import annotations

import re
from html import escape
from html.parser import HTMLParser
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from kb import service
from kb.ingest.fetch import FetchResult
from kb.render import render_markdown, render_snippet
from tests.conftest import AUTH, OpenApp
from tests.corpus import EBS, S3
from tests.helpers import load_corpus, upload
from tests.ops_helpers import EBS_EDITED, LINUX_QUERY, add_gap, audit_rows, backdate, get_doc, scalar
from tests.pdfs import make_text_pdf

HX = {"HX-Request": "true"}
HX_AUTH = {**HX, **AUTH}
DUP = {"KB_DUP_THRESHOLD": 0.8}
ROOT = Path(__file__).parent.parent

XSS_DOC = """# XSS probe

This paragraph is ordinary text that is long enough to be stored as a chunk of its own in the knowledge base.

<script>alert(1)</script>

<img src=x onerror=alert(1)>

[click me](javascript:alert(1)) and [fine](https://example.com/page) and [mail](mailto:someone@example.com).

![bad](javascript:alert(2)) ![good](https://example.com/pic.png)

| a | b |
| - | - |
| 1 | 2 |
"""


ROUTE53 = """# Route 53 hosted zones

Amazon Route 53 is a highly available DNS service. A hosted zone is a container for the records that say how traffic for a domain and its subdomains is routed. Public hosted zones answer queries from the internet, while private hosted zones answer only inside the VPCs they are associated with, which is useful for internal service names that should never be resolvable from outside.

## Routing policies

Simple routing returns one answer. Weighted routing splits traffic between several targets by percentage, latency routing sends users to the Region with the lowest round trip time, and failover routing switches to a standby target when a health check on the primary fails, so a broken endpoint stops receiving users within a minute or so.
"""


class Tags(HTMLParser):
    """Collects every start tag with its attributes."""

    def __init__(self) -> None:
        super().__init__()
        self.tags: list[tuple[str, dict[str, str | None]]] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        self.tags.append((tag, dict(attrs)))


def parse(html: str) -> list[tuple[str, dict[str, str | None]]]:
    parser = Tags()
    parser.feed(html)
    return parser.tags


def names(html: str) -> set[str]:
    return {tag for tag, _ in parse(html)}


def assert_csp_safe(html: str) -> None:
    """No inline script, inline handler, inline style or `javascript:` URL, no eval-style htmx."""
    for tag, attrs in parse(html):
        assert tag != "style", "inline <style>"
        if tag == "script":
            assert attrs.get("src"), "inline <script>"
        for name, value in attrs.items():
            assert not name.startswith("on"), f"inline handler {name} on <{tag}>"
            assert not name.startswith("hx-on"), f"{name} needs eval"
            assert name != "style", f"inline style on <{tag}>"
            if name in ("href", "src"):
                assert not (value or "").strip().lower().startswith(("javascript:", "data:")), value
            if name == "hx-vals":
                assert not (value or "").startswith(("js:", "javascript:")), value


# --- pages ---------------------------------------------------------------------------------


def test_every_page_and_asset_returns_200(open_app: OpenApp) -> None:
    with open_app() as client:
        doc_id = upload(client, EBS, ["storage"])
        paths = [
            "/", "/?q=ebs", "/add", "/maintenance", "/maintenance?days=0", f"/docs/{doc_id}",
            "/ui/search?q=ebs", "/ui/search?q=",
            "/static/app.css", "/static/app.js", "/static/htmx-2.0.4.min.js",
        ]
        for path in paths:
            resp = client.get(path)
            assert resp.status_code == 200, path
        for path in paths[:8]:
            assert_csp_safe(client.get(path).text)


def test_pages_have_the_header_with_nav_token_box_and_mode_badge(open_app: OpenApp) -> None:
    with open_app() as client:
        html = client.get("/").text
    assert 'href="/add"' in html and 'href="/maintenance"' in html
    assert 'id="token-input"' in html and 'id="token-status"' in html and ">read only<" in html
    assert 'id="mode-badge"' in html and "semantic search" in html
    assert "/static/htmx-2.0.4.min.js" in html and "/static/app.js" in html
    assert "unpkg" not in html and "cdn" not in html.lower()


def test_header_badge_says_keyword_without_models(open_app: OpenApp) -> None:
    with open_app(models=False) as client:
        assert "keyword search" in client.get("/").text


def test_unknown_doc_is_an_html_404(open_app: OpenApp) -> None:
    with open_app() as client:
        resp = client.get("/docs/999")
        fragment = client.get("/docs/999", headers=HX)
    assert resp.status_code == 404 and resp.headers["content-type"].startswith("text/html")
    assert "not_found" in resp.text and "<header" in resp.text
    assert fragment.status_code == 404 and "<header" not in fragment.text


def test_static_files_are_cacheable_and_htmx_is_pinned(open_app: OpenApp) -> None:
    with open_app() as client:
        resp = client.get("/static/app.css")
        js = client.get("/static/app.js").text
        htmx = client.get("/static/htmx-2.0.4.min.js")
    assert "max-age" in resp.headers["cache-control"]
    assert htmx.text.startswith("var htmx=function()")
    # Token handling: sessionStorage key, configRequest listener, bearer header on non-GET.
    assert "kbToken" in js and "sessionStorage" in js
    assert "htmx:configRequest" in js and "Bearer " in js and "write token set" in js
    assert "localStorage" not in js and "document.cookie" not in js


def test_no_em_or_en_dashes_in_ui_files() -> None:
    files = [
        *(ROOT / "kb" / "templates").glob("*"),
        ROOT / "kb" / "static" / "app.js", ROOT / "kb" / "static" / "app.css",
        ROOT / "kb" / "ui.py", ROOT / "kb" / "ui_add.py", ROOT / "kb" / "ui_common.py",
        ROOT / "kb" / "render.py",
    ]
    assert files
    for path in files:
        text = path.read_text(encoding="utf-8")
        assert chr(0x2014) not in text and chr(0x2013) not in text, path.name


# --- search ----------------------------------------------------------------------------------


def test_search_fragment_shows_badges_title_path_snippet_and_score(open_app: OpenApp) -> None:
    with open_app() as client:
        ids = load_corpus(client)
        api = client.get("/api/search", params={"q": LINUX_QUERY}).json()
        resp = client.get("/ui/search", params={"q": LINUX_QUERY}, headers=HX)
    assert resp.status_code == 200
    html = resp.text
    top = api["results"][0]
    assert f"badge-conf-{api['confidence']}" in html and f"confidence: {api['confidence']}" in html
    assert "badge-mode-semantic" in html and "mode: semantic" in html
    assert f'href="/docs/{top["doc_id"]}"' in html and top["doc_id"] in ids.values()
    assert top["title"] in html and escape(top["heading_path"]) in html
    assert f"score {top['score']:.2f}" in html
    assert "<header" not in html  # a fragment, not a page


@pytest.mark.parametrize("confidence", ["high", "medium", "low"])
def test_each_confidence_gets_its_own_badge_class(confidence: str) -> None:
    from kb.schemas import SearchHit, SearchResult
    from kb.ui_common import templates

    hit = SearchHit(doc_id=1, chunk_id=1, title="T", heading_path="T > H", snippet="text", score=0.5)
    result = SearchResult(mode="semantic", confidence=confidence, results=[hit])  # type: ignore[arg-type]
    html = templates.get_template("_results.html").render(result=result, q="x")
    assert f"badge-conf-{confidence}" in html
    assert ("No good answer in the knowledge base" in html) == (confidence == "low")


def test_low_confidence_shows_the_no_good_answer_note(open_app: OpenApp) -> None:
    with open_app(models=False) as client:
        load_corpus(client)
        html = client.get("/ui/search", params={"q": "zyxwvu qqqq"}, headers=HX).text
    assert "No good answer in the knowledge base" in html
    assert "badge-conf-low" in html and "mode: keyword" in html


def test_keyword_snippets_keep_only_b_highlights(open_app: OpenApp) -> None:
    doc = (
        "# Highlight probe\n\nThe volume text mentions <script>alert(7)</script> and <i onclick=x>italics</i>"
        " and the word volume again, enough words to make this a proper stored chunk of text.\n"
    )
    with open_app(models=False) as client:
        upload(client, doc)
        html = client.get("/ui/search", params={"q": "volume"}, headers=HX).text
    assert "<b>volume</b>" in html
    tags = names(html)
    assert "script" not in tags and "i" not in tags
    assert_csp_safe(html)


def test_blank_query_shows_the_hint_and_the_page_runs_a_query_from_the_url(open_app: OpenApp) -> None:
    with open_app() as client:
        load_corpus(client)
        blank = client.get("/ui/search", params={"q": "   "}, headers=HX)
        page = client.get("/", params={"q": LINUX_QUERY})
    assert blank.status_code == 200 and "Search the knowledge base" in blank.text
    assert 'class="hit"' in page.text and LINUX_QUERY in page.text


def test_search_form_is_submit_triggered_not_keyup(open_app: OpenApp) -> None:
    with open_app() as client:
        html = client.get("/").text
    form = next(a for t, a in parse(html) if t == "form" and a.get("hx-get"))
    assert form["hx-get"] == "/ui/search" and form["hx-trigger"] == "submit"
    assert "keyup" not in html


# --- rendering safety ----------------------------------------------------------------------


def test_uploaded_markdown_with_script_handler_and_javascript_link_renders_safely(
    open_app: OpenApp,
) -> None:
    with open_app() as client:
        doc_id = upload(client, XSS_DOC)
        html = client.get(f"/docs/{doc_id}").text
    body = html[html.index('class="markdown"'):]
    for tag, attrs in parse(body):
        assert tag != "script" or attrs.get("src") in (None, ""), tag  # none inside the doc body
        assert not any(name.startswith("on") for name in attrs)
        for name in ("href", "src"):
            assert "javascript:" not in (attrs.get(name) or "").lower()
    assert "<img src=x" not in html and "<script>alert" not in html
    assert 'src="x"' not in html
    tags = parse(body)
    assert ("a", {"href": "https://example.com/page", "rel": "noopener noreferrer"}) in tags
    assert ("a", {"href": "mailto:someone@example.com", "rel": "noopener noreferrer"}) in tags
    assert ("img", {"src": "https://example.com/pic.png", "alt": "good"}) in tags
    assert "table" in {t for t, _ in tags}
    assert_csp_safe(html)


def test_render_markdown_allow_list() -> None:
    out = str(render_markdown(
        "# H\n\n**b** *i* `c`\n\n- x\n\n> q\n\n---\n\n```\ncode\n```\n\n"
        "[l](https://a.example) [d](data:text/html,x) [f](ftp://a.example) [js](JaVaScRiPt:alert(1))\n\n"
        '<div style="x" onclick="y">raw</div>\n'
    ))
    for tag in ("<h1>", "<strong>", "<em>", "<code>", "<ul>", "<blockquote>", "<hr>", "<pre>"):
        assert tag in out, tag
    assert 'rel="noopener noreferrer"' in out and 'href="https://a.example"' in out
    hrefs = [a.get("href") for t, a in parse(out) if t == "a"]
    # data: and javascript: never become links; the ftp: link loses its href.
    assert [h for h in hrefs if h] == ["https://a.example"]
    assert "div" not in names(out)
    assert not any(n in ("onclick", "style") for _, attrs in parse(out) for n in attrs)
    assert "&lt;div" in out  # raw HTML is shown as text


def test_render_snippet_escapes_everything_but_b() -> None:
    out = str(render_snippet('a <b>hit</b> <img src=x onerror=1> & "q" <b onclick=1>x</b> <b>open'))
    assert "<b>hit</b>" in out
    assert "&lt;img src=x onerror=1&gt;" in out and "&amp;" in out
    assert "<img" not in out and "onclick" not in out.replace("&lt;b onclick=1&gt;", "")
    assert out.count("<b>") == out.count("</b>")  # the unpaired tag was closed


# --- verify and tokens ---------------------------------------------------------------------


def test_write_buttons_without_a_token_get_401_with_a_message(open_app: OpenApp) -> None:
    with open_app() as client:
        doc_id = upload(client, EBS)
        before = get_doc(client, doc_id)
        calls = [
            client.post(f"/ui/docs/{doc_id}/verify", headers=HX),
            client.post("/ui/admin/fallback", data={"on": "true"}, headers=HX),
            client.post("/ui/add/paste", data={"content": "# T\n\nbody text"}, headers=HX),
            client.post("/ui/add/url", data={"url": "https://example.com/"}, headers=HX),
            client.post("/ui/add/file", files={"file": ("a.md", b"# T\n\ntext")}, headers=HX),
        ]
        wrong = client.post(f"/ui/docs/{doc_id}/verify", headers={**HX, "Authorization": "Bearer nope"})
        api = client.post(f"/api/docs/{doc_id}/verify")
        after = get_doc(client, doc_id)
        health = client.get("/healthz").json()
    for resp in [*calls, wrong]:
        assert resp.status_code == 401
        assert resp.headers["content-type"].startswith("text/html")
        assert "unauthorized" in resp.text and "valid bearer token" in resp.text
        assert "write token in the header" in resp.text
    assert api.status_code == 401 and api.json()["error"] == "unauthorized"
    assert after == before and health["fallback_toggle"] is False and health["chunks"] == before["chunk_count"]


def test_verify_button_swaps_in_the_new_verified_date(open_app: OpenApp) -> None:
    with open_app() as client:
        doc_id = upload(client, EBS)
        page = client.get(f"/docs/{doc_id}").text
        resp = client.post(f"/ui/docs/{doc_id}/verify", headers=HX_AUTH)
        after = get_doc(client, doc_id)
        page_after = client.get(f"/docs/{doc_id}").text
    assert "Not verified yet" in page and f'hx-post="/ui/docs/{doc_id}/verify"' in page
    assert resp.status_code == 200 and 'id="verify-box"' in resp.text
    assert after["verified_by"] == "test" and after["verified_at"][:10] in resp.text
    assert "by test" in resp.text and "by test" in page_after


def test_verify_unknown_doc_is_a_404_notice(open_app: OpenApp) -> None:
    with open_app() as client:
        resp = client.post("/ui/docs/999/verify", headers=HX_AUTH)
    assert resp.status_code == 404 and "not_found" in resp.text


# --- add page --------------------------------------------------------------------------------


def paste(client: TestClient, md: str, **extra: str) -> object:
    return client.post("/ui/add/paste", data={"content": md, "tags": "storage, ec2", **extra}, headers=HX_AUTH)


def test_add_page_has_three_forms_that_post_to_ui_routes(open_app: OpenApp) -> None:
    with open_app() as client:
        html = client.get("/add").text
    forms = {a["id"]: a for t, a in parse(html) if t == "form" and "id" in a and a.get("hx-post")}
    assert set(forms) == {"paste-form", "file-form", "url-form"}
    assert forms["file-form"]["hx-encoding"] == "multipart/form-data"
    assert {f["hx-post"] for f in forms.values()} == {"/ui/add/paste", "/ui/add/file", "/ui/add/url"}


def test_paste_creates_then_reports_the_exact_duplicate(open_app: OpenApp) -> None:
    with open_app() as client:
        first = paste(client, EBS)
        second = paste(client, EBS)
        stored = get_doc(client, 1)
    assert first.status_code == 200 and 'href="/docs/1"' in first.text and "Saved as" in first.text
    assert stored["tags"] == ["ec2", "storage"]
    assert second.status_code == 200 and "already stored" in second.text and 'href="/docs/1"' in second.text
    assert_csp_safe(first.text + second.text)


def test_paste_errors_are_notices(open_app: OpenApp) -> None:
    with open_app() as client:
        empty = client.post("/ui/add/paste", data={"content": "  "}, headers=HX_AUTH)
        no_file = client.post("/ui/add/file", data={"title": "x"}, headers=HX_AUTH)
        no_url = client.post("/ui/add/url", data={"url": ""}, headers=HX_AUTH)
        blocked = client.post("/ui/add/url", data={"url": "http://169.254.169.254/latest"}, headers=HX_AUTH)
        bad_action = client.post("/ui/add/paste", data={"content": EBS, "action": "update"}, headers=HX_AUTH)
        too_short = client.post("/ui/add/paste", data={"content": "#"}, headers=HX_AUTH)
    assert empty.status_code == 422 and "paste some Markdown" in empty.text
    assert no_file.status_code == 422 and "choose a file" in no_file.text
    assert no_url.status_code == 422 and "enter a URL" in no_url.text
    assert blocked.status_code == 400 and "blocked_address" in blocked.text
    assert bad_action.status_code == 400 and "choose the document" in bad_action.text
    assert too_short.status_code == 422 and 'role="alert"' in too_short.text


def test_possible_duplicate_lists_candidates_with_the_three_buttons(open_app: OpenApp) -> None:
    with open_app(**DUP) as client:
        ids = load_corpus(client)
        docs_before = scalar(client, "SELECT count(*) FROM docs")
        resp = paste(client, EBS_EDITED)
        docs_after = scalar(client, "SELECT count(*) FROM docs")
    assert resp.status_code == 200 and docs_after == docs_before
    html = resp.text
    assert "Amazon EBS volumes" in html and f'href="/docs/{ids["ebs"]}"' in html
    assert re.search(r"similarity \d\.\d\d", html)
    for label in ("Update existing", "Merge", "Save anyway"):
        assert label in html
    buttons = [a for t, a in parse(html) if t == "button"]
    assert len(buttons) == 3
    assert {b["hx-post"] for b in buttons} == {"/ui/add/paste"}
    assert {b["hx-include"] for b in buttons} == {"#paste-form"}
    assert {b["hx-target"] for b in buttons} == {"#paste-result"}
    vals = sorted(b["hx-vals"] or "" for b in buttons)
    assert any('"action": "update"' in v and f'"candidate": {ids["ebs"]}' in v for v in vals)
    assert any('"action": "merge"' in v for v in vals) and any('"action": "force"' in v for v in vals)
    assert_csp_safe(html)


def test_update_existing_replaces_the_candidates_content(open_app: OpenApp) -> None:
    with open_app(**DUP) as client:
        ids = load_corpus(client)
        docs_before = scalar(client, "SELECT count(*) FROM docs")
        resp = paste(client, EBS_EDITED, action="update", candidate=str(ids["ebs"]))
        doc = get_doc(client, ids["ebs"])
        docs_after = scalar(client, "SELECT count(*) FROM docs")
    assert resp.status_code == 200 and "Updated" in resp.text and f'href="/docs/{ids["ebs"]}"' in resp.text
    assert "the cost stays low" in doc["content_md"] and docs_after == docs_before


def test_update_with_identical_content_says_no_change(open_app: OpenApp) -> None:
    with open_app(**DUP) as client:
        ids = load_corpus(client)
        resp = paste(client, EBS, action="update", candidate=str(ids["ebs"]))
    assert "No change" in resp.text


def test_merge_saves_then_folds_the_new_doc_into_the_candidate(open_app: OpenApp) -> None:
    with open_app(**DUP) as client:
        ids = load_corpus(client)
        docs_before = scalar(client, "SELECT count(*) FROM docs")
        resp = paste(client, EBS_EDITED, action="merge", candidate=str(ids["ebs"]))
        doc = get_doc(client, ids["ebs"])
        docs_after = scalar(client, "SELECT count(*) FROM docs")
        actions = [a for _, a, _ in audit_rows(client)]
    assert resp.status_code == 200 and "Merged into" in resp.text
    assert docs_after == docs_before  # the new doc was folded in and removed
    assert "the cost stays low" in doc["content_md"] and "Only blocks that changed" in doc["content_md"]
    assert actions[-3:] == ["upload", "merge", "merge_source"]


def test_save_anyway_stores_a_second_doc(open_app: OpenApp) -> None:
    with open_app(**DUP) as client:
        load_corpus(client)
        docs_before = scalar(client, "SELECT count(*) FROM docs")
        resp = paste(client, EBS_EDITED, action="force")
        docs_after = scalar(client, "SELECT count(*) FROM docs")
    assert "Saved as" in resp.text and docs_after == docs_before + 1


def test_merge_into_a_missing_candidate_reports_the_saved_doc(open_app: OpenApp) -> None:
    with open_app(**DUP) as client:
        load_corpus(client)
        resp = paste(client, EBS_EDITED, action="merge", candidate="999")
    assert resp.status_code == 404 and "but the merge failed" in resp.text


def test_file_upload_creates_and_the_buttons_send_the_file_form_again(open_app: OpenApp) -> None:
    with open_app(**DUP) as client:
        ids = load_corpus(client)
        created = client.post(
            "/ui/add/file", files={"file": ("s3-notes.md", ROUTE53.encode(), "text/markdown")},
            data={"tags": "extra"}, headers=HX_AUTH,
        )
        dup = client.post(
            "/ui/add/file", files={"file": ("ebs.md", EBS_EDITED.encode(), "text/markdown")}, headers=HX_AUTH,
        )
        updated = client.post(
            "/ui/add/file", files={"file": ("ebs.md", EBS_EDITED.encode(), "text/markdown")},
            data={"action": "update", "candidate": str(ids["ebs"])}, headers=HX_AUTH,
        )
        doc = get_doc(client, ids["ebs"])
    assert created.status_code == 200 and "Saved as" in created.text
    buttons = [a for t, a in parse(dup.text) if t == "button"]
    assert buttons and all(b["hx-encoding"] == "multipart/form-data" for b in buttons)
    assert all(b["hx-include"] == "#file-form" and b["hx-target"] == "#file-result" for b in buttons)
    assert "Updated" in updated.text and "the cost stays low" in doc["content_md"]


def test_pdf_file_goes_through_the_normal_conversion(open_app: OpenApp) -> None:
    with open_app() as client:
        resp = client.post(
            "/ui/add/file", files={"file": ("guide.pdf", make_text_pdf(), "application/pdf")}, headers=HX_AUTH
        )
        doc = get_doc(client, 1)
    assert "Saved as" in resp.text and doc["source_type"] == "pdf"


def test_url_add_and_update_use_the_fetched_page(open_app: OpenApp, monkeypatch: pytest.MonkeyPatch) -> None:
    page = FetchResult(
        final_url="https://docs.example.com/ebs.md",
        content_type="text/markdown",
        body=EBS_EDITED.encode(),
    )
    monkeypatch.setattr(service, "fetch_url", lambda url, transport=None: page)
    with open_app(**DUP) as client:
        ids = load_corpus(client)
        dup = client.post("/ui/add/url", data={"url": page.final_url}, headers=HX_AUTH)
        buttons = [a for t, a in parse(dup.text) if t == "button"]
        updated = client.post(
            "/ui/add/url",
            data={"url": page.final_url, "action": "update", "candidate": str(ids["ebs"])},
            headers=HX_AUTH,
        )
        doc = get_doc(client, ids["ebs"])
    assert "Save anyway" in dup.text and {b["hx-include"] for b in buttons} == {"#url-form"}
    assert "Updated" in updated.text and "the cost stays low" in doc["content_md"]


def test_url_create(open_app: OpenApp, monkeypatch: pytest.MonkeyPatch) -> None:
    page = FetchResult("https://docs.example.com/s3.md", "text/markdown", S3.encode())
    monkeypatch.setattr(service, "fetch_url", lambda url, transport=None: page)
    with open_app() as client:
        resp = client.post("/ui/add/url", data={"url": page.final_url, "tags": "s3"}, headers=HX_AUTH)
        doc = get_doc(client, 1)
    assert "Saved as" in resp.text and doc["source_url"] == page.final_url and doc["tags"] == ["s3"]


# --- maintenance -----------------------------------------------------------------------------


def test_maintenance_lists_stale_docs_gaps_and_audit_rows_escaped(open_app: OpenApp) -> None:
    evil = "<img src=x onerror=alert(1)>"
    with open_app() as client:
        old = upload(client, EBS)
        fresh = upload(client, S3)
        backdate(client, old, 120)
        add_gap(client, "how do I <b>reset</b> a root password", at="2026-10-02T10:00:00Z", top_score=0.12)
        note = client.post(f"/api/docs/{fresh}/verify", json={"note": evil}, headers=AUTH)
        html = client.get("/maintenance").text
    assert note.status_code == 200
    assert "Amazon EBS volumes" in html and f'hx-post="/ui/docs/{old}/verify"' in html
    assert f'href="/docs/{old}"' in html
    assert "Amazon S3 storage classes" not in html.split("Top gaps")[0].split("Stale documents")[1]
    assert "reset" in html and "0.12" in html
    assert "verify" in html and "upload" in html
    tags = names(html)
    assert "img" not in tags and "b" not in tags  # note text and gap text were escaped
    assert "&lt;img src=x onerror=alert(1)&gt;" in html
    assert_csp_safe(html)


def test_stale_verify_button_returns_the_compact_cell(open_app: OpenApp) -> None:
    with open_app() as client:
        doc_id = upload(client, EBS)
        backdate(client, doc_id, 120)
        resp = client.post(f"/ui/docs/{doc_id}/verify", data={"view": "cell"}, headers=HX_AUTH)
        html = client.get("/maintenance").text
    assert resp.status_code == 200 and "verified-now" in resp.text and "by test" in resp.text
    assert "Nothing is stale" in html


def test_fallback_toggle_through_the_ui(open_app: OpenApp) -> None:
    with open_app() as client:
        before = client.get("/maintenance").text
        on = client.post("/ui/admin/fallback", data={"on": "true"}, headers=HX_AUTH)
        health_on = client.get("/healthz").json()
        page_on = client.get("/").text
        off = client.post("/ui/admin/fallback", data={"on": "false"}, headers=HX_AUTH)
        health_off = client.get("/healthz").json()
        actions = [a for _, a, _ in audit_rows(client)]
    assert "Force keyword fallback" in before and "semantic search" in before
    assert on.status_code == 200 and "Turn keyword fallback off" in on.text
    assert 'hx-swap-oob="true"' in on.text and "keyword search (forced)" in on.text
    assert health_on["fallback_toggle"] is True and health_on["mode"] == "keyword"
    assert "keyword search (forced)" in page_on
    assert "Force keyword fallback" in off.text and health_off["fallback_toggle"] is False
    assert actions == ["fallback_on", "fallback_off"]
    toggle = [a for t, a in parse(on.text) if t == "button"][0]
    assert toggle["hx-vals"] == '{"on": "false"}'
