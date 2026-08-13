"""Named SWE-bench dataset profiles and leaderboard intent."""
from __future__ import annotations

from dataclasses import dataclass
import hashlib


@dataclass(frozen=True)
class BenchmarkProfile:
    """Immutable identity and completeness contract for one benchmark."""

    name: str
    dataset: str
    split: str
    expected_instances: int
    leaderboard: bool
    instance_ids_sha256: str = ""


PROFILES = {
    "verified": BenchmarkProfile(
        name="verified",
        dataset="princeton-nlp/SWE-bench_Verified",
        split="test",
        expected_instances=500,
        leaderboard=True,
        instance_ids_sha256="a6b0fd7c8c2969a0eef892e032250adcfa6d32362d395c246930e61b575ac9b9",
    ),
    "lite": BenchmarkProfile(
        name="lite",
        dataset="princeton-nlp/SWE-bench_Lite",
        split="test",
        expected_instances=300,
        leaderboard=False,
        instance_ids_sha256="6b9850decb64f71aaed19d394195eb254b666a4abe7f113365195b3e4de2b450",
    ),
}
DEFAULT_PROFILE = "verified"


def get_profile(name: str) -> BenchmarkProfile:
    """Return a known profile or raise a CLI-friendly error."""
    normalized = str(name or DEFAULT_PROFILE).strip().lower()
    try:
        return PROFILES[normalized]
    except KeyError as exc:
        choices = ", ".join(sorted(PROFILES))
        raise ValueError(f"unknown SWE-bench profile {name!r}; choose: {choices}") from exc


def instance_ids_digest(instance_ids: list[str]) -> str:
    """Hash the sorted official instance set independent of dataset ordering."""
    payload = "\n".join(sorted(str(item) for item in instance_ids)) + "\n"
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()
