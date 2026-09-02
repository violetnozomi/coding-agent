"""SSRF regression coverage for model-initiated web fetching."""
from __future__ import annotations

from email.message import Message
from urllib.error import URLError
from urllib.request import Request

import pytest

from nz_coder.foundation.network_policy import NetworkTargetPolicy, UnsafeNetworkTarget


@pytest.mark.parametrize(
    "url",
    [
        "http://127.0.0.1/",
        "http://localhost/",
        "http://sub.localhost/",
        "http://[::1]/",
        "http://10.0.0.1/",
        "http://172.16.0.1/",
        "http://192.168.1.1/",
        "http://169.254.169.254/latest/meta-data/",
        "http://[::ffff:127.0.0.1]/",
        "http://[fe80::1]/",
        "http://224.0.0.1/",
        "http://0.0.0.0/",
        "http://192.0.2.1/",
    ],
)
def test_literal_private_and_nonroutable_targets_are_rejected(url):
    from nz_coder.tools.webfetch import webfetch

    assert webfetch(url).startswith("Error:")


def test_dns_with_any_private_answer_is_rejected():
    policy = NetworkTargetPolicy(
        resolver=lambda _host, _port: ("93.184.216.34", "10.0.0.8")
    )

    with pytest.raises(UnsafeNetworkTarget, match="private"):
        policy.validate_url("https://mixed.example.test/")


def test_public_redirect_to_private_target_is_rejected():
    from nz_coder.tools.webfetch import _SafeRedirectHandler

    policy = NetworkTargetPolicy(resolver=lambda _host, _port: ("93.184.216.34",))
    handler = _SafeRedirectHandler(policy)

    with pytest.raises(URLError, match="private"):
        handler.redirect_request(
            Request("https://public.example.test/start"),
            None,
            302,
            "Found",
            Message(),
            "http://127.0.0.1/private",
        )


def test_simulated_public_response_remains_fetchable(monkeypatch):
    from nz_coder.tools.webfetch import scoped_webfetch_network_policy, webfetch

    class Response:
        headers = Message()

        def __enter__(self):
            self.headers["Content-Type"] = "text/plain; charset=utf-8"
            return self

        def __exit__(self, *_args):
            return False

        def read(self, _limit):
            return b"public body"

        def geturl(self):
            return "https://public.example.test/resource"

    class Opener:
        def open(self, _request, timeout):
            assert timeout > 0
            return Response()

    monkeypatch.setattr("nz_coder.tools.webfetch._opener_for", lambda _url: Opener())
    policy = NetworkTargetPolicy(resolver=lambda _host, _port: ("93.184.216.34",))

    with scoped_webfetch_network_policy(policy):
        result = webfetch("https://public.example.test/resource")

    assert result == "public body"
