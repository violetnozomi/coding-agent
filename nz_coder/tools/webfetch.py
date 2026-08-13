"""Bounded HTTP(S) web fetching with text conversion and image attachments."""
from __future__ import annotations

import ipaddress
import re
import zlib
from html.parser import HTMLParser
from urllib.error import HTTPError, URLError
from urllib.parse import urljoin, urlsplit, urlunsplit
from urllib.request import HTTPRedirectHandler, ProxyHandler, Request, build_opener

from nz_coder.attachments import SUPPORTED_IMAGE_MIMES, make_image_attachment
from nz_coder.tools import ToolOutput, register

MAX_RESPONSE_BYTES = 5 * 1024 * 1024
DEFAULT_TIMEOUT_SECONDS = 30.0
MAX_TIMEOUT_SECONDS = 120.0
_FORMATS = frozenset({"text", "markdown", "html"})
_SKIP_TAGS = frozenset({"script", "style", "noscript", "iframe", "object", "embed"})
_BLOCK_TAGS = frozenset({
    "article", "aside", "blockquote", "dd", "div", "dl", "dt", "footer",
    "header", "main", "nav", "p", "section", "table", "tbody", "td",
    "tfoot", "th", "thead", "tr",
})


def _normalize_url(value: str) -> str:
    """Return one absolute HTTP(S) URL with an ASCII hostname."""
    raw = str(value or "").strip()
    if not raw or any(ord(character) < 0x20 for character in raw):
        raise ValueError("URL must be a non-empty HTTP(S) URL")
    parsed = urlsplit(raw)
    if parsed.scheme.lower() not in {"http", "https"}:
        raise ValueError("URL must start with http:// or https://")
    if not parsed.hostname:
        raise ValueError("URL must include a hostname")
    if parsed.username is not None or parsed.password is not None:
        raise ValueError("URL cannot contain credentials")
    try:
        port = parsed.port
        hostname = parsed.hostname.encode("idna").decode("ascii")
    except (UnicodeError, ValueError) as exc:
        raise ValueError("URL contains an invalid hostname or port") from exc
    if ":" in hostname and not hostname.startswith("["):
        hostname = f"[{hostname}]"
    netloc = hostname + (f":{port}" if port is not None else "")
    return urlunsplit((parsed.scheme.lower(), netloc, parsed.path or "/", parsed.query, ""))


class _SafeRedirectHandler(HTTPRedirectHandler):
    """Keep redirects inside the same HTTP(S)-only URL contract."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):  # noqa: ANN001
        try:
            normalized = _normalize_url(urljoin(req.full_url, newurl))
        except ValueError as exc:
            raise URLError(str(exc)) from exc
        return super().redirect_request(req, fp, code, msg, headers, normalized)


class _HTMLRenderer(HTMLParser):
    """Small standard-library HTML text/Markdown renderer."""

    def __init__(self, *, markdown: bool) -> None:
        super().__init__(convert_charrefs=True)
        self.markdown = markdown
        self.output: list[str] = []
        self.skip_depth = 0
        self.links: list[str] = []
        self.in_pre = 0

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        tag = tag.lower()
        if tag in _SKIP_TAGS:
            self.skip_depth += 1
            return
        if self.skip_depth:
            return
        attributes = dict(attrs)
        if tag == "blockquote" and self.markdown:
            self.output.append("\n\n> ")
        elif tag in _BLOCK_TAGS:
            self.output.append("\n\n")
        elif tag == "br":
            self.output.append("\n")
        elif tag in {"ul", "ol"}:
            self.output.append("\n")
        elif tag == "li":
            self.output.append("\n- " if self.markdown else "\n")
        elif tag in {"h1", "h2", "h3", "h4", "h5", "h6"}:
            prefix = "#" * int(tag[1]) + " " if self.markdown else ""
            self.output.append("\n\n" + prefix)
        elif tag == "pre":
            self.in_pre += 1
            self.output.append("\n```\n" if self.markdown else "\n")
        elif tag == "code" and self.markdown and not self.in_pre:
            self.output.append("`")
        elif tag == "a" and self.markdown:
            self.links.append(str(attributes.get("href") or ""))
            self.output.append("[")

    def handle_endtag(self, tag: str) -> None:
        tag = tag.lower()
        if tag in _SKIP_TAGS:
            self.skip_depth = max(0, self.skip_depth - 1)
            return
        if self.skip_depth:
            return
        if tag == "pre":
            self.in_pre = max(0, self.in_pre - 1)
            self.output.append("\n```\n" if self.markdown else "\n")
        elif tag == "code" and self.markdown and not self.in_pre:
            self.output.append("`")
        elif tag == "a" and self.markdown:
            href = self.links.pop() if self.links else ""
            self.output.append(f"]({href})" if href else "]")
        elif tag in _BLOCK_TAGS or tag in {
            "h1", "h2", "h3", "h4", "h5", "h6", "li", "ul", "ol",
        }:
            self.output.append("\n")

    def handle_data(self, data: str) -> None:
        if self.skip_depth or not data:
            return
        if self.in_pre:
            self.output.append(data)
            return
        self.output.append(re.sub(r"\s+", " ", data))

    def rendered(self) -> str:
        value = "".join(self.output)
        value = re.sub(r"[ \t]+\n", "\n", value)
        value = re.sub(r"\n{3,}", "\n\n", value)
        return value.strip()


def _html_content(content: str, *, markdown: bool) -> str:
    renderer = _HTMLRenderer(markdown=markdown)
    renderer.feed(content)
    renderer.close()
    return renderer.rendered()


def _bounded_decompress(data: bytes, encoding: str) -> bytes:
    """Decode common HTTP content encodings without permitting decompression bombs."""
    selected = encoding.strip().lower()
    if not selected or selected == "identity":
        return data
    if selected == "gzip":
        decoder = zlib.decompressobj(16 + zlib.MAX_WBITS)
    elif selected == "deflate":
        decoder = zlib.decompressobj()
    else:
        raise ValueError(f"Unsupported content encoding: {encoding}")
    output = decoder.decompress(data, MAX_RESPONSE_BYTES + 1)
    if decoder.unconsumed_tail or len(output) > MAX_RESPONSE_BYTES:
        raise ValueError("Response too large (exceeds 5MB limit)")
    output += decoder.flush(MAX_RESPONSE_BYTES + 1 - len(output))
    if len(output) > MAX_RESPONSE_BYTES:
        raise ValueError("Response too large (exceeds 5MB limit)")
    return output


def _accept_header(format_name: str) -> str:
    if format_name == "markdown":
        return (
            "text/markdown;q=1.0, text/x-markdown;q=0.9, text/plain;q=0.8, "
            "text/html;q=0.7, */*;q=0.1"
        )
    if format_name == "text":
        return "text/plain;q=1.0, text/markdown;q=0.9, text/html;q=0.8, */*;q=0.1"
    return (
        "text/html;q=1.0, application/xhtml+xml;q=0.9, text/plain;q=0.8, "
        "text/markdown;q=0.7, */*;q=0.1"
    )


def _opener_for(url: str):
    """Bypass ambient proxies for loopback while preserving them for remote URLs."""
    hostname = urlsplit(url).hostname or ""
    try:
        loopback = ipaddress.ip_address(hostname).is_loopback
    except ValueError:
        loopback = hostname.lower() == "localhost"
    if loopback:
        return build_opener(ProxyHandler({}), _SafeRedirectHandler())
    return build_opener(_SafeRedirectHandler())


def webfetch(
    url: str,
    format: str = "markdown",  # noqa: A002
    timeout: float | None = None,
) -> str:
    """Fetch a bounded HTTP(S) resource as Markdown, text, HTML, or an image."""
    try:
        selected_format = str(format or "markdown").strip().lower()
        if selected_format not in _FORMATS:
            raise ValueError("format must be one of: text, markdown, html")
        if timeout is None:
            timeout_seconds = DEFAULT_TIMEOUT_SECONDS
        elif isinstance(timeout, bool) or not isinstance(timeout, (int, float)):
            raise ValueError("timeout must be a number of seconds")
        else:
            timeout_seconds = min(float(timeout), MAX_TIMEOUT_SECONDS)
            if timeout_seconds <= 0:
                raise ValueError("timeout must be greater than zero")

        normalized_url = _normalize_url(url)
        request = Request(
            normalized_url,
            headers={
                "User-Agent": (
                    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                    "AppleWebKit/537.36 (KHTML, like Gecko) "
                    "Chrome/143.0.0.0 Safari/537.36"
                ),
                "Accept": _accept_header(selected_format),
                "Accept-Language": "en-US,en;q=0.9",
                "Accept-Encoding": "gzip, deflate",
            },
            method="GET",
        )
        opener = _opener_for(normalized_url)
        with opener.open(request, timeout=timeout_seconds) as response:
            content_length = response.headers.get("Content-Length")
            if content_length:
                try:
                    declared_size = int(content_length)
                except ValueError:
                    declared_size = 0
                if declared_size > MAX_RESPONSE_BYTES:
                    raise ValueError("Response too large (exceeds 5MB limit)")
            data = response.read(MAX_RESPONSE_BYTES + 1)
            if len(data) > MAX_RESPONSE_BYTES:
                raise ValueError("Response too large (exceeds 5MB limit)")
            data = _bounded_decompress(
                data,
                str(response.headers.get("Content-Encoding") or ""),
            )
            content_type = str(response.headers.get("Content-Type") or "")
            mime = content_type.split(";", 1)[0].strip().lower()
            final_url = _normalize_url(str(response.geturl()))

        title = f"{final_url} ({content_type})"
        if mime in SUPPORTED_IMAGE_MIMES:
            return ToolOutput(
                "Image fetched successfully",
                title=title,
                attachments=[make_image_attachment(data, mime)],
            )

        charset_match = re.search(r"charset=([^;\s]+)", content_type, re.I)
        charset = charset_match.group(1).strip('"\'') if charset_match else "utf-8"
        try:
            content = data.decode(charset, errors="replace")
        except LookupError:
            content = data.decode("utf-8", errors="replace")
        if selected_format == "html":
            output = content
        elif "text/html" in content_type.lower() or mime == "application/xhtml+xml":
            output = _html_content(content, markdown=selected_format == "markdown")
        else:
            output = content
        return ToolOutput(output, title=title)
    except HTTPError as exc:
        return f"Error: HTTP {exc.code} while fetching URL"
    except URLError as exc:
        return f"Error: Request failed: {exc.reason}"
    except (OSError, TimeoutError, ValueError, zlib.error) as exc:
        return f"Error: {exc}"
    except Exception as exc:
        return f"Error: {type(exc).__name__}: {exc}"


register(
    name="webfetch",
    description=(
        "Fetch an HTTP(S) URL and return text, Markdown, HTML, or an image attachment. "
        "Prefer a more targeted available tool when one exists."
    ),
    parameters={
        "type": "object",
        "properties": {
            "url": {"type": "string", "description": "The URL to fetch."},
            "format": {
                "type": "string",
                "enum": ["text", "markdown", "html"],
                "default": "markdown",
            },
            "timeout": {
                "type": "number",
                "description": "Optional timeout in seconds, capped at 120.",
            },
        },
        "required": ["url"],
        "additionalProperties": False,
    },
    handler=webfetch,
    execution="read",
)
