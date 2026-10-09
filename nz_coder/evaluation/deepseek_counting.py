"""V4.1纯文本参考编码；公开recipe尚无托管API计数等价保证。"""
from __future__ import annotations

import hashlib
from importlib.metadata import version
import json
from pathlib import Path

from nz_coder.evaluation.model_relay import InputAccounting

TOKENIZER_SHA256 = "81f64d1248a68ce3663e07ab3ee48b851e5df0e32d27cb98e4c9a268151e8d99"
RECIPE_REVISION = "57b9c842429d850b03b5fdbe3fed266c13cc7f2b"
MODELS = {"deepseek-v4-flash", "deepseek-flash"}
NATIVE_SHA256 = "1461bc4895c188d4ad60ca05eb7bd663aa7fa3286f2ecbebca39ecd73c5b0ec1"
GAP = "public_v41_recipe_has_no_hosted_api_equivalence_or_hidden_overhead_bound"


def _keys(value, allowed):
    if not isinstance(value, dict) or value.keys() - allowed:
        raise ValueError("unsupported_fields_or_shape")


def validate_text_request(payload):
    """不让官方转换器忽略的字段悄悄消失；仅接受本实验的文本请求。"""
    _keys(payload, {"model", "messages", "tools", "tool_choice", "max_tokens", "stream", "stream_options",
                    "thinking", "reasoning_effort", "temperature", "top_p", "n"})
    if payload.get("model") not in MODELS or payload.get("n", 1) != 1:
        raise ValueError("unsupported_model_or_choices")
    if "thinking" in payload:
        _keys(payload["thinking"], {"type"})
        if payload["thinking"].get("type") not in {"enabled", "disabled"}:
            raise ValueError("unsupported_thinking")
    if payload.get("reasoning_effort") not in {None, "none", "low", "high", "max"}:
        raise ValueError("unsupported_effort_alias_mapping")
    if "stream_options" in payload:
        _keys(payload["stream_options"], {"include_usage"})
    messages = payload.get("messages")
    if not isinstance(messages, list) or not messages:
        raise ValueError("missing_messages")
    for message in messages:
        _keys(message, {"role", "content", "name", "tool_call_id", "tool_calls", "reasoning_content", "prefix"})
        if message.get("role") not in {"system", "user", "assistant", "tool"}:
            raise ValueError("unsupported_role")
        if message.get("content") is not None and not isinstance(message["content"], str):
            raise ValueError("non_text_content")
        for key in ("name", "tool_call_id", "reasoning_content"):
            if key in message and not isinstance(message[key], str):
                raise ValueError("non_text_message_field")
        for call in message.get("tool_calls", []):
            _keys(call, {"id", "type", "function"})
            if call.get("type") != "function":
                raise ValueError("unsupported_call_type")
            _keys(call.get("function"), {"name", "arguments"})
            if not all(isinstance(call["function"].get(k), str) for k in ("name", "arguments")):
                raise ValueError("invalid_call")
    for tool in payload.get("tools", []):
        _keys(tool, {"type", "function"})
        if tool.get("type") != "function":
            raise ValueError("unsupported_tool")
        _keys(tool.get("function"), {"name", "description", "parameters"})
    choice = payload.get("tool_choice", "auto")
    if isinstance(choice, dict):
        _keys(choice, {"type", "function"})
        _keys(choice.get("function"), {"name"})
        if choice.get("type") != "function":
            raise ValueError("unsupported_tool_choice")
    elif choice not in {"auto", "none", "required"}:
        raise ValueError("unsupported_tool_choice")


class DeepSeekV41Counter:
    """引用官方编码实现，不把本地token定义冒充服务端token契约。"""

    def __init__(self, tokenizer: Path):
        if hashlib.sha256(tokenizer.read_bytes()).hexdigest() != TOKENIZER_SHA256:
            raise ValueError("v41_tokenizer_hash_mismatch")
        import deepseek_recipe as recipe
        if version("deepseek-recipe") != "0.1.1" or recipe.__version__ != "0.1.0":
            raise ValueError("unaudited_recipe_version")
        self.recipe = recipe
        native_hash = hashlib.sha256(Path(recipe._native.__file__).read_bytes()).hexdigest()
        if native_hash != NATIVE_SHA256:
            raise ValueError("unaudited_recipe_binary")
        self.encoding = recipe.DeepseekV41Encoding().with_tokenizer(recipe.Tokenizer.from_file(str(tokenizer)))
        self.resources = {"tokenizer_sha256": TOKENIZER_SHA256, "recipe_distribution": version("deepseek-recipe"),
                          "recipe_native_version": recipe.__version__,
                          "recipe_native_sha256": native_hash,
                          "reference_source_revision": RECIPE_REVISION}
        self.contract_id = hashlib.sha256(json.dumps(self.resources, sort_keys=True).encode()).hexdigest()

    def __call__(self, raw: bytes, payload: dict) -> InputAccounting:
        digest = hashlib.sha256(raw).hexdigest()
        try:
            if json.loads(raw) != payload:
                raise ValueError("payload_changed_after_serialization")
            validate_text_request(payload)
            request = self.recipe.ChatCompletionRequest(raw)
            converted = request.convert(self.recipe.ConversionOptions(default_thinking_mode=True))
            count = len(self.encoding.encode(converted.conversation))
        except (ValueError, TypeError, self.recipe.ConversionError) as exc:
            return InputAccounting(None, "v41-reference-rejected:" + str(exc), contract_id=self.contract_id,
                                   payload_sha256=digest)
        return InputAccounting(None, GAP, contract_id=self.contract_id, payload_sha256=digest,
                               reference_count=count)

    def status(self):
        return {"model_scope": sorted(MODELS), "resources": self.resources, "contract_id": self.contract_id,
                "counter_source_sha256": hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
                "relay_source_sha256": hashlib.sha256(Path(__file__).with_name("model_relay.py").read_bytes()).hexdigest(),
                "service_scope": "hosted-deepseek",
                "output_contract": {"main": 64000, "review": 1024, "total": 100000, "completion_includes_reasoning": True},
                "evidence_level": "unknown", "strict_input_bound_verified": False, "reason": GAP}
