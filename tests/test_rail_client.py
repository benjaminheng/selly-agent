"""RailClient against a fake carousell.ai HTTP server: JSON-RPC create + fail-closed live verify."""

from __future__ import annotations

import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from selly_agent.rail.client import (
    RailAuthError,
    RailClient,
    RailToolError,
    _text_content,
)


class _Handler(BaseHTTPRequestHandler):
    # behavior is set on the server instance (self.server) by each test
    def log_message(self, *args):  # silence the test server
        pass

    def do_POST(self):
        length = int(self.headers.get("Content-Length", 0))
        body = json.loads(self.rfile.read(length)) if length else {}
        auth = self.headers.get("Authorization", "")
        if self.server.require_auth and auth != f"Bearer {self.server.expected_key}":
            self._send(401, {"error": "bad key"})
            return
        method = body.get("method")
        if method == "initialize":
            self._send(200, {"jsonrpc": "2.0", "id": body["id"], "result": {"capabilities": {}}})
        elif method == "tools/call":
            self._send(200, {"jsonrpc": "2.0", "id": body["id"], "result": self.server.tool_result})
        else:
            self._send(200, {"jsonrpc": "2.0", "id": body.get("id"), "result": {}})

    def do_GET(self):
        # the listing-page verify GET
        status = self.server.listing_status
        self.send_response(status)
        self.end_headers()
        self.wfile.write(b"ok" if status == 200 else b"no")

    def _send(self, status, obj):
        payload = json.dumps(obj).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.end_headers()
        self.wfile.write(payload)


@pytest.fixture
def fake_rail():
    server = ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
    server.require_auth = True
    server.expected_key = "guest-key"
    server.tool_result = {"structuredContent": {}}
    server.listing_status = 200
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    base = f"http://127.0.0.1:{server.server_address[1]}"
    try:
        yield server, base
    finally:
        server.shutdown()


def _client(base, key="guest-key"):
    return RailClient(api_base=base, api_key=key, web_base_url=base)


def test_initialize_and_create_listing(fake_rail) -> None:
    server, base = fake_rail
    url = f"{base}/listing/1-lamp"
    server.tool_result = {"structuredContent": {"listing_id": "L1", "url": url}}
    client = _client(base)
    client.initialize()
    listing = client.create_listing({"title": "Lamp", "price_cents": 8000, "currency": "SGD"})
    assert listing == {"listing_id": "L1", "url": url}


def test_create_listing_from_text_content(fake_rail) -> None:
    server, base = fake_rail
    url = f"{base}/listing/2"
    server.tool_result = {"content": [{"type": "text", "text": json.dumps({"url": url})}]}
    listing = _client(base).create_listing({"title": "x"})
    assert listing["url"] == url and listing["listing_id"] is None


def test_bad_key_raises_auth_error(fake_rail) -> None:
    _, base = fake_rail
    with pytest.raises(RailAuthError):
        _client(base, key="wrong").create_listing({"title": "x"})


def test_create_listing_without_url_is_tool_error(fake_rail) -> None:
    server, base = fake_rail
    server.tool_result = {"structuredContent": {"listing_id": "L1"}}  # no url
    with pytest.raises(RailToolError, match="no listing URL"):
        _client(base).create_listing({"title": "x"})


def test_rail_reports_iserror_result(fake_rail) -> None:
    server, base = fake_rail
    server.tool_result = {"isError": True, "content": [{"type": "text", "text": "over quota"}]}
    with pytest.raises(RailToolError, match="over quota"):
        _client(base).create_listing({"title": "x"})


def test_verify_listing_url_live_200(fake_rail) -> None:
    server, base = fake_rail
    server.listing_status = 200
    _client(base).verify_listing_url(f"{base}/listing/1")  # no raise


def test_verify_fails_closed_on_non_200(fake_rail) -> None:
    server, base = fake_rail
    server.listing_status = 404
    with pytest.raises(RailToolError, match="404"):
        _client(base).verify_listing_url(f"{base}/listing/1")


def test_verify_rejects_wrong_base_or_path(fake_rail) -> None:
    _, base = fake_rail
    with pytest.raises(RailToolError, match="not under"):
        _client(base).verify_listing_url("https://evil.example/listing/1")
    with pytest.raises(RailToolError, match="not under"):
        _client(base).verify_listing_url(f"{base}/u/chat")


def test_text_content_helper() -> None:
    result = {
        "content": [
            {"type": "text", "text": "a"},
            {"type": "image"},
            {"type": "text", "text": "b"},
        ]
    }
    assert _text_content(result) == "ab"
