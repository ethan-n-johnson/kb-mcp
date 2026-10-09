"""Settings and write-token loading (no real AWS)."""

from __future__ import annotations

import logging

import boto3
import pytest
from moto import mock_aws

from kb import config


@pytest.fixture(autouse=True)
def _clean(monkeypatch: pytest.MonkeyPatch):
    for var in ("KB_WRITE_TOKENS", "KB_TOKENS_PARAM", "AWS_REGION", "KB_DB", "KB_MODELS"):
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setenv("AWS_ACCESS_KEY_ID", "testing")
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "testing")
    config.clear_token_cache()
    yield
    config.clear_token_cache()


def test_defaults() -> None:
    s = config.Settings.from_env({})
    assert s.db_path == "/data/kb.db"
    assert s.models_on is True
    assert s.model_dir == "/models"
    assert s.bucket is None
    assert s.tokens_param == "/kb/write-tokens"
    assert s.aws_region == "us-east-1"
    assert (s.conf_high, s.conf_low, s.dup_threshold, s.search_timeout) == (0.70, 0.30, 0.92, 3.0)
    assert s.extension_origin is None


def test_overrides_and_frozen() -> None:
    s = config.Settings.from_env(
        {"KB_DB": "/x.db", "KB_MODELS": "off", "KB_CONF_HIGH": "0.8", "KB_BUCKET": "b"}
    )
    assert (s.db_path, s.models_on, s.conf_high, s.bucket) == ("/x.db", False, 0.8, "b")
    with pytest.raises(Exception):
        s.db_path = "/y"  # type: ignore[misc]


def test_parse_tokens() -> None:
    assert config.parse_tokens(" a:1 , b:x:y ,") == {"a": "1", "b": "x:y"}
    with pytest.raises(ValueError) as exc:
        config.parse_tokens("good:1,nosecret")
    assert "nosecret" not in str(exc.value)


def test_env_tokens_never_call_aws(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("KB_WRITE_TOKENS", "alice:s3cret,bob:hunter2")

    def boom(*a, **k):
        raise AssertionError("AWS must not be contacted")

    monkeypatch.setattr(boto3, "client", boom)
    assert config.load_write_tokens() == {"alice": "s3cret", "bob": "hunter2"}


def test_logs_names_not_values(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    monkeypatch.setenv("KB_WRITE_TOKENS", "alice:s3cret")
    with caplog.at_level(logging.INFO, logger="kb.config"):
        config.load_write_tokens()
    text = " ".join(f"{r.getMessage()} {r.__dict__}" for r in caplog.records)
    assert "alice" in text
    assert "s3cret" not in text


@mock_aws
def test_ssm_branch_decrypts_and_caches(monkeypatch: pytest.MonkeyPatch) -> None:
    ssm = boto3.client("ssm", region_name="us-east-1")
    ssm.put_parameter(Name="/kb/write-tokens", Value="alice:one,bob:two", Type="SecureString")

    assert config.load_write_tokens() == {"alice": "one", "bob": "two"}

    # Cached: deleting the parameter must not matter on the second call.
    ssm.delete_parameter(Name="/kb/write-tokens")
    assert config.load_write_tokens() == {"alice": "one", "bob": "two"}


@mock_aws
def test_ssm_custom_param_name(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("KB_TOKENS_PARAM", "/other/tokens")
    boto3.client("ssm", region_name="us-east-1").put_parameter(
        Name="/other/tokens", Value="c:3", Type="SecureString"
    )
    assert config.load_write_tokens() == {"c": "3"}
