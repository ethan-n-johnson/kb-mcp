"""Builders for small PDF fixtures, generated with pymupdf at test time."""

from __future__ import annotations

import pymupdf

_LB_BODY = (
    "An Application Load Balancer distributes incoming traffic across targets in "
    "multiple Availability Zones. It routes requests at layer seven using listener "
    "rules, and it performs health checks on every registered target. "
)
_HC_BODY = (
    "A target group health check sends periodic requests to each target. Unhealthy "
    "targets stop receiving traffic until they pass the configured number of "
    "consecutive checks again. "
)


def _body(page: pymupdf.Page, top: float, text: str) -> None:
    page.insert_textbox(
        pymupdf.Rect(72, top, 523, top + 180), text, fontsize=11, fontname="helv"
    )


def make_text_pdf(*, metadata_title: str = "") -> bytes:
    """One page with two headed sections ("Elastic Load Balancing", "Health checks")."""
    doc = pymupdf.open()
    page = doc.new_page()
    page.insert_text((72, 72), "Elastic Load Balancing", fontsize=24, fontname="hebo")
    _body(page, 90, _LB_BODY * 2)
    page.insert_text((72, 300), "Health checks", fontsize=18, fontname="hebo")
    _body(page, 320, _HC_BODY * 2)
    doc.set_metadata({"title": metadata_title})
    return doc.tobytes()


def make_image_only_pdf(pages: int = 1) -> bytes:
    """Pages containing only a drawn rectangle and a raster image: no text layer."""
    doc = pymupdf.open()
    pixmap = pymupdf.Pixmap(pymupdf.csRGB, pymupdf.IRect(0, 0, 60, 60), False)
    pixmap.clear_with(40)
    for _ in range(pages):
        page = doc.new_page()
        page.draw_rect(pymupdf.Rect(100, 100, 300, 300), fill=(0, 0, 0))
        page.insert_image(pymupdf.Rect(320, 100, 420, 200), pixmap=pixmap)
    return doc.tobytes()


def make_blank_pdf(pages: int) -> bytes:
    """A PDF with `pages` empty pages (used for the page-limit check)."""
    doc = pymupdf.open()
    for _ in range(pages):
        doc.new_page()
    return doc.tobytes()
