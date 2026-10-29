"""Settings and write-token loading (no real AWS)."""

from __future__ import annotations

import logging

import boto3
import pytest
from moto import mock_aws

from kb import config


@pytest.fixture(autouse=True)
def _clean(monkeypatch: pytest.MonkeyPatch):
    for var in (
        "KB_WRITE_TOKENS", "KB_TOKENS_PARAM", "KB_TOKENS_SOURCE", "KB_TOKENS_KEY", "KB_BUCKET",
        "AWS_REGION", "KB_DB", "KB_MODELS", "KB_DATABASE_URL", "KB_DATABASE_URL_SOURCE",
        "KB_DATABASE_URL_KEY",
    ):
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setenv("AWS_ACCESS_KEY_ID", "testing")
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "testing")
    config.clear_token_cache()
    config.clear_database_url_cache()
    yield
    config.clear_token_cache()
    config.clear_database_url_cache()


def test_defaults() -> None:
    s = config.Settings.from_env({})
    assert s.db_path == "/data/kb.db"
    assert s.models_on is True
    assert s.model_dir == "/models"
    assert s.bucket is None
    assert s.tokens_param == "/kb/write-tokens"
    assert (s.tokens_source, s.tokens_key) == ("ssm", "config/write-tokens")
    assert s.aws_region == "us-east-1"
    assert (s.conf_high, s.conf_low, s.dup_threshold, s.search_timeout) == (0.70, 0.02, 0.96, 5.0)
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


def test_tokens_source_is_validated() -> None:
    assert config.Settings.from_env({"KB_TOKENS_SOURCE": " S3 "}).tokens_source == "s3"
    with pytest.raises(ValueError):
        config.Settings.from_env({"KB_TOKENS_SOURCE": "vault"})


@mock_aws
def test_s3_branch_reads_the_bucket_object_and_caches(monkeypatch: pytest.MonkeyPatch) -> None:
    s3 = boto3.client("s3", region_name="us-east-1")
    s3.create_bucket(Bucket="kb-data")
    s3.put_object(Bucket="kb-data", Key="config/write-tokens", Body=b"alice:one,bob:two\n")
    monkeypatch.setenv("KB_TOKENS_SOURCE", "s3")
    monkeypatch.setenv("KB_BUCKET", "kb-data")

    def no_ssm(service: str, *a, **k):
        assert service == "s3", "the s3 source must not call SSM"
        return real_client(service, *a, **k)

    real_client = boto3.client
    monkeypatch.setattr(boto3, "client", no_ssm)
    assert config.load_write_tokens() == {"alice": "one", "bob": "two"}

    # Cached: deleting the object must not matter on the second call.
    s3.delete_object(Bucket="kb-data", Key="config/write-tokens")
    assert config.load_write_tokens() == {"alice": "one", "bob": "two"}


@mock_aws
def test_s3_branch_custom_key(monkeypatch: pytest.MonkeyPatch) -> None:
    s3 = boto3.client("s3", region_name="us-east-1")
    s3.create_bucket(Bucket="kb-data")
    s3.put_object(Bucket="kb-data", Key="other/tokens", Body=b"c:3")
    monkeypatch.setenv("KB_TOKENS_SOURCE", "s3")
    monkeypatch.setenv("KB_BUCKET", "kb-data")
    monkeypatch.setenv("KB_TOKENS_KEY", "other/tokens")
    assert config.load_write_tokens() == {"c": "3"}


def test_s3_branch_needs_a_bucket(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("KB_TOKENS_SOURCE", "s3")
    with pytest.raises(ValueError, match="KB_BUCKET"):
        config.load_write_tokens()


@mock_aws
def test_s3_branch_logs_names_not_values(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    s3 = boto3.client("s3", region_name="us-east-1")
    s3.create_bucket(Bucket="kb-data")
    s3.put_object(Bucket="kb-data", Key="config/write-tokens", Body=b"alice:s3cret")
    monkeypatch.setenv("KB_TOKENS_SOURCE", "s3")
    monkeypatch.setenv("KB_BUCKET", "kb-data")
    with caplog.at_level(logging.INFO, logger="kb.config"):
        config.load_write_tokens()
    text = " ".join(f"{r.getMessage()} {r.__dict__}" for r in caplog.records)
    assert "alice" in text and "'s3'" in text
    assert "s3cret" not in text


def test_rerank_settings_are_read_and_validated() -> None:
    Settings = config.Settings
    s = Settings.from_env({"KB_RERANK_CANDIDATES": "8", "KB_RERANK_MAX_CHARS": "600"})
    assert (s.rerank_candidates, s.rerank_max_chars) == (8, 600)
    assert (Settings().rerank_candidates, Settings().rerank_max_chars) == (15, 1000)
    for bad in ({"KB_RERANK_CANDIDATES": "0"}, {"KB_RERANK_MAX_CHARS": "50"}):
        with pytest.raises(ValueError, match="KB_RERANK"):
            Settings.from_env(bad)


# --- database URL (PostgreSQL mode) ---------------------------------------------------

URL = "postgresql://kbadmin:s3cretpw@db.example:5432/kb?sslmode=require"


def test_no_database_url_means_sqlite() -> None:
    s = config.Settings.from_env({})
    assert (s.database_url, s.dialect) == (None, "sqlite")


def test_database_url_selects_postgres_and_stays_out_of_the_repr() -> None:
    s = config.Settings.from_env({"KB_DATABASE_URL": URL, "KB_BUCKET": "b"})
    assert (s.database_url, s.dialect) == (URL, "postgres")
    assert "s3cretpw" not in repr(s)
    assert s.snapshots_enabled is False  # snapshots copy the SQLite file


def test_a_database_url_that_is_not_postgres_is_refused_without_echoing_it() -> None:
    with pytest.raises(ValueError) as err:
        config.Settings.from_env({"KB_DATABASE_URL": "mysql://u:s3cretpw@h/db"})
    assert "s3cretpw" not in str(err.value)


def test_unknown_database_url_source_is_refused() -> None:
    with pytest.raises(ValueError):
        config.Settings.from_env({"KB_DATABASE_URL_SOURCE": "ssm"})


@mock_aws
def test_database_url_is_read_from_s3_once_and_cached() -> None:
    s3 = boto3.client("s3", region_name="us-east-1")
    s3.create_bucket(Bucket="kb-x")
    s3.put_object(Bucket="kb-x", Key="config/database-url", Body=URL.encode() + b"\n")
    env = {"KB_DATABASE_URL_SOURCE": "s3", "KB_BUCKET": "kb-x"}
    assert config.Settings.from_env(env).database_url == URL
    s3.put_object(Bucket="kb-x", Key="config/database-url", Body=b"postgresql://other/db")
    assert config.Settings.from_env(env).database_url == URL  # cached for the life of the process


@mock_aws
def test_database_url_key_can_be_chosen() -> None:
    s3 = boto3.client("s3", region_name="us-east-1")
    s3.create_bucket(Bucket="kb-x")
    s3.put_object(Bucket="kb-x", Key="elsewhere/url", Body=URL.encode())
    env = {"KB_DATABASE_URL_SOURCE": "s3", "KB_BUCKET": "kb-x", "KB_DATABASE_URL_KEY": "elsewhere/url"}
    assert config.Settings.from_env(env).database_url == URL


def test_the_env_url_wins_and_s3_is_not_called() -> None:
    env = {"KB_DATABASE_URL": URL, "KB_DATABASE_URL_SOURCE": "s3"}  # no bucket, no AWS: must not matter
    assert config.Settings.from_env(env).database_url == URL


def test_s3_source_without_a_bucket_is_refused() -> None:
    with pytest.raises(ValueError, match="KB_BUCKET"):
        config.Settings.from_env({"KB_DATABASE_URL_SOURCE": "s3"})
