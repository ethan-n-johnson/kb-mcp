"""scripts/demo_check.sh: syntax, usage and a full run against a canned local server."""

from __future__ import annotations

import json
import shutil
import subprocess
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from typing import Any, Iterator

import pytest

SCRIPT = Path(__file__).resolve().parent.parent / "scripts" / "demo_check.sh"

pytestmark = pytest.mark.skipif(shutil.which("jq") is None, reason="jq is not installed")

STALE_URLS = [
    "https://docs.aws.amazon.com/AmazonS3/latest/userguide/Versioning.html",
    "https://docs.aws.amazon.com/AmazonCloudFront/latest/DeveloperGuide/Introduction.html",
    "https://raw.githubusercontent.com/fastapi/fastapi/abc/docs/en/docs/tutorial/debugging.md",
]


def make_handler(config: dict[str, Any]) -> type[BaseHTTPRequestHandler]:
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args: Any) -> None:
            pass

        def send(self, body: Any, code: int = 200, headers: dict[str, str] | None = None) -> None:
            data = json.dumps(body).encode()
            self.send_response(code)
            self.send_header("content-type", "application/json")
            for k, v in (headers or {}).items():
                self.send_header(k, v)
            self.send_header("content-length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def do_GET(self) -> None:  # noqa: N802
            if self.path.startswith("/healthz"):
                self.send({"status": "ok", "mode": config["mode"], "models": {"embedder": "ok", "reranker": "ok"},
                           "fallback_toggle": False, "chunks": 784, "index_size": 784})
            elif self.path.startswith("/api/search"):
                conf = "low" if "sourdough" in self.path else "high"
                self.send({"mode": "semantic", "confidence": conf, "results": [{"heading_path": "A > B"}]})
            elif self.path.startswith("/api/stale"):
                self.send({"docs": [{"source_url": u} for u in STALE_URLS[: config["stale"]]]})
            elif self.path.startswith("/api/gaps"):
                self.send({"gaps": []})
            elif self.path.startswith("/static/"):
                self.send({}, headers={"Cache-Control": "public, max-age=300"})
            else:
                self.send({}, 404)

        def do_HEAD(self) -> None:  # noqa: N802
            self.do_GET()

        def do_POST(self) -> None:  # noqa: N802
            length = int(self.headers.get("content-length", "0"))
            body = json.loads(self.rfile.read(length))
            if body["method"] == "tools/list":
                self.send({"jsonrpc": "2.0", "id": 1, "result": {"tools": [{}] * 12}})
            elif config["gate"]:
                self.send({"jsonrpc": "2.0", "id": 2, "result": {"isError": True, "content": [
                    {"type": "text", "text": "Error executing tool delete: This tool needs a write token: add ..."}]}})
            else:
                self.send({"jsonrpc": "2.0", "id": 2, "result": {"isError": True, "content": [
                    {"type": "text", "text": "not_found: doc 999999999 not found"}]}})

    return Handler


@pytest.fixture
def server() -> Iterator[tuple[str, dict[str, Any]]]:
    config: dict[str, Any] = {"mode": "semantic", "stale": 3, "gate": True}
    httpd = HTTPServer(("127.0.0.1", 0), make_handler(config))
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    yield f"http://127.0.0.1:{httpd.server_address[1]}", config
    httpd.shutdown()


def run(url: str, tmp_path: Path) -> subprocess.CompletedProcess[str]:
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir(exist_ok=True)
    stub = bin_dir / "aws"
    stub.write_text("#!/usr/bin/env bash\nexit 255\n", encoding="utf-8")  # not logged in
    stub.chmod(0o755)
    return subprocess.run(
        ["bash", str(SCRIPT), url], capture_output=True, text=True, timeout=60,
        env={"PATH": f"{bin_dir}:/usr/bin:/bin", "HOME": str(tmp_path)},
    )


def test_the_script_is_valid_bash_and_needs_a_url() -> None:
    assert subprocess.run(["bash", "-n", str(SCRIPT)]).returncode == 0
    result = subprocess.run(["bash", str(SCRIPT)], capture_output=True, text=True)
    assert result.returncode == 2 and "usage" in result.stderr


def test_everything_passes_against_a_healthy_stack(server: tuple[str, dict[str, Any]], tmp_path: Path) -> None:
    url, _ = server
    result = run(url, tmp_path)
    assert result.returncode == 0, result.stdout
    assert "FAIL" not in result.stdout
    assert result.stdout.count("PASS") >= 10
    assert "SKIP  stack mode and healthy targets" in result.stdout


def test_a_failing_line_makes_the_exit_code_non_zero(server: tuple[str, dict[str, Any]], tmp_path: Path) -> None:
    url, config = server
    config.update(mode="keyword", stale=2, gate=False)
    result = run(url, tmp_path)
    assert result.returncode == 1
    out = result.stdout
    assert "FAIL  mode is 'keyword'" in out
    assert "FAIL  /api/stale shows 2 of the 3" in out
    assert "FAIL  MCP delete without a token answered" in out
