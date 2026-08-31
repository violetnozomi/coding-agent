"""Protocol and attachment tests for the bounded webfetch tool."""
from __future__ import annotations

import gzip
import threading
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from nz_coder.protocol.attachments import MAX_IMAGE_BYTES
from nz_coder.tools import ToolOutput, dispatch, get_execution_mode, get_specs
from nz_coder.tools.webfetch import MAX_RESPONSE_BYTES, webfetch


_PNG = b"\x89PNG\r\n\x1a\nweb-image"
_HTML = b"""<!doctype html><html><head><style>hidden{}</style></head><body>
<h1>Example title</h1><script>secret()</script><p>Hello <a href="/docs">docs</a>.</p>
<ul><li>First</li><li>Second</li></ul><pre>value = 1</pre></body></html>"""


class _Handler(BaseHTTPRequestHandler):
    def do_GET(self):  # noqa: N802
        if self.path == "/html":
            self._send(200, _HTML, "text/html; charset=utf-8")
            return
        if self.path == "/plain":
            self._send(200, b"plain body", "text/plain; charset=utf-8")
            return
        if self.path == "/gzip":
            body = gzip.compress(b"compressed body")
            self._send(
                200,
                body,
                "text/plain; charset=utf-8",
                {"Content-Encoding": "gzip"},
            )
            return
        if self.path == "/image":
            self._send(200, _PNG, "image/png")
            return
        if self.path == "/redirect-image":
            self.send_response(302)
            self.send_header("Location", "/image")
            self.end_headers()
            return
        if self.path == "/bad-redirect":
            self.send_response(302)
            self.send_header("Location", "file:///etc/passwd")
            self.end_headers()
            return
        if self.path == "/oversize":
            self.send_response(200)
            self.send_header("Content-Type", "text/plain")
            self.send_header("Content-Length", str(MAX_RESPONSE_BYTES + 1))
            self.end_headers()
            return
        if self.path == "/oversize-stream":
            self.send_response(200)
            self.send_header("Content-Type", "text/plain")
            self.end_headers()
            self.wfile.write(b"x" * (MAX_RESPONSE_BYTES + 1))
            return
        if self.path == "/oversize-gzip":
            body = gzip.compress(b"x" * (MAX_RESPONSE_BYTES + 1))
            self._send(
                200,
                body,
                "text/plain",
                {"Content-Encoding": "gzip"},
            )
            return
        self._send(404, b"missing", "text/plain")

    def _send(self, status, body, content_type, headers=None):  # noqa: ANN001
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        for name, value in (headers or {}).items():
            self.send_header(name, value)
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, format, *args):  # noqa: A002, ANN001
        return None


@contextmanager
def _server():
    server = ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_address[1]}"
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)


def test_webfetch_is_registered_as_read_tool():
    specs = {item["function"]["name"]: item for item in get_specs()}

    assert "webfetch" in specs
    assert specs["webfetch"]["function"]["parameters"]["required"] == ["url"]
    assert get_execution_mode("webfetch") == "read"


def test_webfetch_converts_html_to_markdown_and_text():
    with _server() as base_url:
        markdown = webfetch(base_url + "/html")
        text = webfetch(base_url + "/html", format="text")
        html = webfetch(base_url + "/html", format="html")

    assert isinstance(markdown, ToolOutput)
    assert "# Example title" in markdown
    assert "[docs](/docs)" in markdown
    assert "- First" in markdown
    assert "```" in markdown
    assert "secret()" not in markdown
    assert "# Example title" not in text
    assert "Example title" in text
    assert "secret()" not in text
    assert "<script>secret()</script>" in html


def test_webfetch_returns_image_attachment_after_safe_redirect():
    with _server() as base_url:
        result = webfetch(base_url + "/redirect-image")

    assert isinstance(result, ToolOutput)
    assert result == "Image fetched successfully"
    assert result.attachments[0]["mime"] == "image/png"
    assert result.attachments[0]["url"].startswith("data:image/png;base64,")
    assert len(_PNG) < MAX_IMAGE_BYTES
    assert "/image (image/png)" in result.title


def test_webfetch_decodes_bounded_gzip_and_dispatch_preserves_metadata():
    with _server() as base_url:
        result = dispatch("webfetch", {"url": base_url + "/gzip"})

    assert isinstance(result, ToolOutput)
    assert result == "compressed body"
    assert "text/plain" in result.title


def test_webfetch_rejects_invalid_urls_redirects_and_parameters():
    with _server() as base_url:
        bad_redirect = webfetch(base_url + "/bad-redirect")
        oversized = webfetch(base_url + "/oversize")
        oversized_stream = webfetch(base_url + "/oversize-stream")
        oversized_gzip = webfetch(base_url + "/oversize-gzip")
        missing = webfetch(base_url + "/missing")

    assert bad_redirect == "Error: HTTP 302 while fetching URL"
    assert oversized == "Error: Response too large (exceeds 5MB limit)"
    assert oversized_stream == "Error: Response too large (exceeds 5MB limit)"
    assert oversized_gzip == "Error: Response too large (exceeds 5MB limit)"
    assert missing == "Error: HTTP 404 while fetching URL"
    assert webfetch("file:///etc/passwd").startswith("Error: URL must start")
    assert webfetch("https://user:secret@example.test/") == (
        "Error: URL cannot contain credentials"
    )
    assert webfetch("https://example.test/", format="pdf") == (
        "Error: format must be one of: text, markdown, html"
    )
    assert webfetch("https://example.test/", timeout=0) == (
        "Error: timeout must be greater than zero"
    )


def test_webfetch_rejects_nonfinite_timeout_before_network(monkeypatch):
    called = False

    def fail_if_called(_url):
        nonlocal called
        called = True
        raise AssertionError("network opener must not be created")

    monkeypatch.setattr("nz_coder.tools.webfetch._opener_for", fail_if_called)

    for timeout in (float("nan"), float("inf"), float("-inf")):
        assert webfetch("https://example.test/", timeout=timeout) == (
            "Error: timeout must be a positive finite number"
        )

    assert called is False
