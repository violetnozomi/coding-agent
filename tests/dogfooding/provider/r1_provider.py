"""Linux R1-only, conservative before-dispatch budget gate; no answer logic."""
from __future__ import annotations

import json
import os
from pathlib import Path
import time
from urllib.parse import urlsplit

from nz_coder.protocol.attachments import openai_chat_messages
from nz_coder.providers.capabilities import ModelCapabilities, prepare_openai_request
from nz_coder.providers.openai_compatible import OpenAICompatibleProvider


def reserve(ledger: Path, input_bound: int, output_cap: int) -> None:
    """Never refund: failures, retries and abandoned responses remain charged."""
    import fcntl

    cost = (input_bound * 0.44 + output_cap * 1.32) / 1_000_000
    with ledger.open("a+", encoding="utf-8") as handle:
        os.chmod(ledger, 0o600)
        fcntl.flock(handle, fcntl.LOCK_EX)
        handle.seek(0)
        rows = [json.loads(line) for line in handle if line.strip()]
        if sum(row["reserved_usd"] for row in rows) + cost > 5:
            raise RuntimeError("R1 total USD 5 budget exhausted before dispatch")
        handle.write(json.dumps({
            "call": len(rows) + 1, "time": time.time(),
            "input_bound": input_bound, "output_cap": output_cap,
            "reserved_usd": cost,
        }) + "\n")
        handle.flush()
        os.fsync(handle.fileno())


class MeteredProvider(OpenAICompatibleProvider):
    """Use the installed product's request normalization and actual SDK client."""

    def create_client(self):
        from openai import OpenAI
        return OpenAI(api_key=self.api_key, base_url=self.base_url,
                      max_retries=0, timeout=90)

    def create_completion(self, client, **kwargs):
        if kwargs.get("model") != "deepseek-v4-flash":
            raise ValueError("R1 model has no approved price")
        cap = kwargs.get("max_tokens") or kwargs.get("max_completion_tokens")
        if not isinstance(cap, int) or not 1 <= cap <= 4096:
            raise ValueError("R1 output cap must be explicit and <= 4096")
        capabilities = kwargs.pop("_capabilities", None)
        if not isinstance(capabilities, ModelCapabilities):
            capabilities = self.capabilities(kwargs["model"])
        request = prepare_openai_request(capabilities, kwargs)
        request["messages"] = openai_chat_messages(list(request.get("messages") or []))
        for message in request["messages"]:
            content = message.get("content")
            if not (content is None or isinstance(content, str)):
                raise ValueError("R1 only prices text input; multimodal request rejected")
        # UTF-8 bytes conservatively bound text tokens; include schemas/history and
        # reserve extra framing. Do not put request bodies or credentials in ledger.
        size = len(json.dumps(request, ensure_ascii=False).encode("utf-8")) + 4096
        ledger = Path(os.environ["NZ_R1_LEDGER"])
        if not ledger.is_absolute():
            raise ValueError("R1 ledger must be an explicit private absolute path")
        reserve(ledger, size, cap)
        return client.chat.completions.create(**request)


def factory(**kwargs):
    endpoint = urlsplit(kwargs["base_url"])
    if (endpoint.scheme != "https" or endpoint.hostname != "api.deepseek.com"
            or endpoint.username or endpoint.password or endpoint.query
            or endpoint.port not in (None, 443)
            or endpoint.path.rstrip("/") not in ("", "/v1")):
        raise ValueError("R1 endpoint must be the existing official DeepSeek service")
    return MeteredProvider(**kwargs)
