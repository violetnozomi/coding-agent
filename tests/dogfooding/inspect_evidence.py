"""Export numeric/public R1 evidence only; never copy raw messages or host paths."""
from __future__ import annotations

import argparse
import json
from pathlib import Path


def summarize(path: Path) -> list[dict]:
    snapshot = json.loads(path.read_text())
    groups = {}
    for record in snapshot["messages"]:
        info = record["info"]
        identity = info.get("interaction_run_id")
        content = info.get("content")
        if info.get("role") == "user" and isinstance(content,str) and content.startswith("R1:F"):
            groups[identity] = {"case":content.split()[0], "request_accepted":info.get("time",{}).get("created"),
                "first_frontend_feedback":"UNKNOWN", "frontend_finished":"UNKNOWN",
                "assistant_turns":0, "tools":[], "business_final":None,
                "provider_calls_including_auxiliary":"UNKNOWN"}
        if identity not in groups:
            continue
        group = groups[identity]
        if info.get("role") == "assistant":
            group["assistant_turns"] += 1
            if info.get("end_state"):
                group["business_final"] = info.get("time",{}).get("completed")
                group["end_reason"] = info["end_state"].get("reason")
        for part in record.get("parts",[]):
            if part.get("type") == "tool":
                state = part.get("state",{})
                metadata = state.get("metadata",{})
                group["tools"].append({"name":part.get("tool"),"status":state.get("status"),
                    "time":state.get("time"), "exit":metadata.get("exit"),
                    "truncated":metadata.get("truncated"),"bytes":metadata.get("total_output_bytes")})
    return list(groups.values())


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("snapshot",type=Path)
    args = parser.parse_args()
    print(json.dumps(summarize(args.snapshot),indent=2))
