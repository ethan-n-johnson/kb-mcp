"""Settings built from the environment, plus write-token loading."""

from __future__ import annotations

import logging
import os
import threading
from dataclasses import dataclass
from typing import Mapping

log = logging.getLogger(__name__)


def _int(env: Mapping[str, str], name: str, default: int) -> int:
    raw = env.get(name)
    return int(raw) if raw not in (None, "") else default


def _float(env: Mapping[str, str], name: str, default: float) -> float:
    raw = env.get(name)
    return float(raw) if raw not in (None, "") else default


@dataclass(frozen=True)
class Settings:
    """Frozen application settings; defaults match CLAUDE.md."""

    db_path: str = "/data/kb.db"
    models_on: bool = True
    rerank_on: bool = True
    model_dir: str = "/models"
    bucket: str | None = None
    tokens_param: str = "/kb/write-tokens"
    tokens_source: str = "ssm"
    tokens_key: str = "config/write-tokens"
    aws_region: str = "us-east-1"
    conf_high: float = 0.70
    conf_low: float = 0.30
    cosine_conf_high: float = 0.75
    cosine_conf_low: float = 0.55
    dup_threshold: float = 0.92
    search_timeout: float = 3.0
    extension_origin: str | None = None
    search_rate: str = "30/minute"
    proxy_hops: int = 0
    snapshots: bool = True
    snapshot_interval: float = 5.0

    @property
    def snapshots_enabled(self) -> bool:
        """True when committed writes are uploaded to S3 as snapshots.

        Needs `KB_BUCKET` and `KB_SNAPSHOTS` not set to `off`.
        """
        return self.snapshots and bool(self.bucket)

    @classmethod
    def from_env(cls, env: Mapping[str, str] | None = None) -> "Settings":
        """Build settings from `env` (default `os.environ`).

        Secrets (`KB_WRITE_TOKENS`, `KB_TOKEN`) are deliberately not stored here.
        """
        e = os.environ if env is None else env
        interval = _float(e, "KB_SNAPSHOT_INTERVAL", cls.snapshot_interval)
        if not interval > 0:
            raise ValueError("KB_SNAPSHOT_INTERVAL must be greater than 0 seconds")
        tokens_source = (e.get("KB_TOKENS_SOURCE") or cls.tokens_source).strip().lower()
        if tokens_source not in ("ssm", "s3"):
            raise ValueError("KB_TOKENS_SOURCE must be ssm or s3")
        return cls(
            db_path=e.get("KB_DB") or cls.db_path,
            models_on=(e.get("KB_MODELS") or "on").strip().lower() != "off",
            rerank_on=(e.get("KB_RERANK") or "on").strip().lower() != "off",
            model_dir=e.get("KB_MODEL_DIR") or cls.model_dir,
            bucket=e.get("KB_BUCKET") or None,
            tokens_param=e.get("KB_TOKENS_PARAM") or cls.tokens_param,
            tokens_source=tokens_source,
            tokens_key=e.get("KB_TOKENS_KEY") or cls.tokens_key,
            aws_region=e.get("AWS_REGION") or cls.aws_region,
            conf_high=_float(e, "KB_CONF_HIGH", cls.conf_high),
            conf_low=_float(e, "KB_CONF_LOW", cls.conf_low),
            cosine_conf_high=_float(e, "KB_COSINE_CONF_HIGH", cls.cosine_conf_high),
            cosine_conf_low=_float(e, "KB_COSINE_CONF_LOW", cls.cosine_conf_low),
            dup_threshold=_float(e, "KB_DUP_THRESHOLD", cls.dup_threshold),
            search_timeout=_float(e, "KB_SEARCH_TIMEOUT", cls.search_timeout),
            extension_origin=e.get("KB_EXTENSION_ORIGIN") or None,
            search_rate=(e.get("KB_SEARCH_RATE") or cls.search_rate).strip(),
            proxy_hops=_int(e, "KB_PROXY_HOPS", cls.proxy_hops),
            snapshots=(e.get("KB_SNAPSHOTS") or "on").strip().lower() != "off",
            snapshot_interval=interval,
        )


def get_settings() -> Settings:
    """Return settings built from the current process environment."""
    return Settings.from_env()


def parse_tokens(raw: str) -> dict[str, str]:
    """Parse `name:secret,name:secret` into a dict.

    Whitespace around entries is ignored. A secret may itself contain colons
    (only the first colon splits). Malformed entries raise ValueError without
    echoing the entry, so secrets never reach logs or tracebacks.
    """
    tokens: dict[str, str] = {}
    for index, part in enumerate(raw.split(","), start=1):
        part = part.strip()
        if not part:
            continue
        name, sep, secret = part.partition(":")
        name, secret = name.strip(), secret.strip()
        if not sep or not name or not secret:
            raise ValueError(f"malformed write token entry #{index}: expected name:secret")
        tokens[name] = secret
    return tokens


_cache_lock = threading.Lock()
_cached_tokens: dict[str, str] | None = None


def clear_token_cache() -> None:
    """Forget cached tokens (used by tests)."""
    global _cached_tokens
    with _cache_lock:
        _cached_tokens = None


def load_write_tokens() -> dict[str, str]:
    """Return write tokens as `{name: secret}`.

    If `KB_WRITE_TOKENS` is set it is parsed and AWS is never contacted.
    Otherwise the tokens are read once and cached for the life of the process,
    from the source named by `KB_TOKENS_SOURCE`:

    - `ssm` (default): the SecureString named by `KB_TOKENS_PARAM`
      (`WithDecryption=True`).
    - `s3`: the object `KB_TOKENS_KEY` in `KB_BUCKET`, for sandboxes whose
      instance role cannot read SSM (deviation 8 in docs/PROGRESS.md).

    Only token names are logged, never values.
    """
    global _cached_tokens
    with _cache_lock:
        if _cached_tokens is not None:
            return dict(_cached_tokens)
        override = os.environ.get("KB_WRITE_TOKENS")
        if override:
            tokens = parse_tokens(override)
            source = "env"
        else:
            import boto3  # imported lazily so the env path never touches AWS

            settings = get_settings()
            if settings.tokens_source == "s3":
                if not settings.bucket:
                    raise ValueError("KB_TOKENS_SOURCE=s3 needs KB_BUCKET")
                client = boto3.client("s3", region_name=settings.aws_region)
                resp = client.get_object(Bucket=settings.bucket, Key=settings.tokens_key)
                tokens = parse_tokens(resp["Body"].read().decode("utf-8"))
                source = "s3"
            else:
                client = boto3.client("ssm", region_name=settings.aws_region)
                resp = client.get_parameter(Name=settings.tokens_param, WithDecryption=True)
                tokens = parse_tokens(resp["Parameter"]["Value"])
                source = "ssm"
        _cached_tokens = tokens
        log.info(
            "write tokens loaded",
            extra={"token_source": source, "token_names": sorted(tokens)},
        )
        return dict(tokens)
