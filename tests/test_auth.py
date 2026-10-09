"""Bearer-token checking and the 401 behaviour of write routes."""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from kb import config
from kb.auth import bearer_secret, check_token

BODY = {"title": "T", "content": "# T\n\nSome words about elastic load balancing here.", "format": "markdown"}


@pytest.fixture
def tokens(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("KB_WRITE_TOKENS", "alice:s3cret-a,bob:pa:ss")
    config.clear_token_cache()
    yield
    config.clear_token_cache()


def test_check_token_returns_the_name(tokens: None) -> None:
    assert check_token("s3cret-a") == "alice"
    assert check_token("pa:ss") == "bob"


@pytest.mark.parametrize("raw", [None, "", "nope", "s3cret-", "S3CRET-A", "s3cret-a "])
def test_check_token_rejects_everything_else(tokens: None, raw: str | None) -> None:
    assert check_token(raw) is None


def test_check_token_handles_non_ascii(tokens: None) -> None:
    assert check_token("sécret") is None


def test_check_token_compares_every_token(tokens: None, monkeypatch: pytest.MonkeyPatch) -> None:
    """No early exit: compare_digest runs once per configured token."""
    import hmac

    calls: list[int] = []
    real = hmac.compare_digest

    def counting(a: bytes, b: bytes) -> bool:
        calls.append(1)
        return real(a, b)

    monkeypatch.setattr("kb.auth.hmac.compare_digest", counting)
    assert check_token("s3cret-a") == "alice"  # the first token matches
    assert len(calls) == 2


@pytest.mark.parametrize(
    ("header", "secret"),
    [
        ("Bearer abc", "abc"),
        ("bearer abc", "abc"),
        ("BEARER  abc ", "abc"),
        ("Basic abc", None),
        ("Bearer", None),
        ("Bearer ", None),
        ("abc", None),
        (None, None),
    ],
)
def test_bearer_secret_parsing(header: str | None, secret: str | None) -> None:
    assert bearer_secret(header) == secret


def test_write_routes_need_a_token(client: TestClient) -> None:
    for call in (
        lambda h: client.post("/api/docs", json=BODY, headers=h),
        lambda h: client.post("/api/docs/file", files={"file": ("a.md", b"# A\n\nwords")}, headers=h),
        lambda h: client.delete("/api/docs/1", headers=h),
    ):
        for headers in ({}, {"Authorization": "Bearer wrong"}, {"Authorization": "Basic testtoken"}):
            resp = call(headers)
            assert resp.status_code == 401
            assert resp.headers["www-authenticate"] == "Bearer"
            assert resp.json()["error"] == "unauthorized"


def test_unauthorized_response_does_not_echo_the_secret(client: TestClient) -> None:
    resp = client.post("/api/docs", json=BODY, headers={"Authorization": "Bearer hunter2-secret"})
    assert "hunter2-secret" not in resp.text


def test_read_routes_need_no_token(client: TestClient) -> None:
    created = client.post(
        "/api/docs", json=BODY, headers={"Authorization": "Bearer testtoken"}
    ).json()
    assert client.get(f"/api/docs/{created['doc_id']}").status_code == 200
    assert client.get("/api/chunks/1").status_code == 200
    assert client.get("/api/docs/9999").status_code == 404
