"""Saves uploaded original files (PDF and HTML only) to S3."""

from __future__ import annotations

import logging
import re

from kb.config import get_settings

log = logging.getLogger(__name__)

SAVED_CONTENT_TYPES = frozenset({"application/pdf", "text/html", "application/xhtml+xml"})
_UNSAFE_RE = re.compile(r"[^A-Za-z0-9._-]+")


def safe_filename(name: str | None, default: str) -> str:
    """Reduce `name` to a safe S3 key segment (basename, limited characters)."""
    base = (name or "").replace("\\", "/").rsplit("/", 1)[-1]
    cleaned = _UNSAFE_RE.sub("_", base).strip("._")[:100]
    return cleaned or default


def original_key(doc_id: int, filename: str) -> str:
    """S3 object key for a doc's original: `originals/<doc_id>/<filename>`."""
    return f"originals/{doc_id}/{filename}"


def save_original(doc_id: int, filename: str, data: bytes, content_type: str) -> str | None:
    """Store `data` at `originals/<doc_id>/<filename>` in `KB_BUCKET`.

    Returns the object key, or None without contacting AWS when `KB_BUCKET` is
    unset or `content_type` is not PDF or HTML. The bucket applies SSE-S3 and
    blocks public access; the upload also asks for SSE-S3 explicitly. Errors
    from boto3 propagate to the caller.
    """
    settings = get_settings()
    media_type = content_type.split(";", 1)[0].strip().lower()
    if not settings.bucket or media_type not in SAVED_CONTENT_TYPES:
        return None
    import boto3  # lazy: keeps the no-bucket path free of AWS imports

    key = original_key(doc_id, safe_filename(filename, "original"))
    boto3.client("s3", region_name=settings.aws_region).put_object(
        Bucket=settings.bucket,
        Key=key,
        Body=data,
        ContentType=media_type,
        ServerSideEncryption="AES256",
    )
    return key
