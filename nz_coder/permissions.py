"""Permission system: deny → mode → allow → ask pipeline."""

import re
from nz_coder import config
from nz_coder.command_policy import classify_bash, is_known_read_only_command

MODES = ("default", "auto", "plan")
WRITE_TOOLS = {
    "write_file",
    "edit_file",
    "apply_patch",
    "python_structural_edit",
    "save_memory",
    "delete_memory",
}
READ_TOOLS = {"read_file", "list_directory", "grep_search", "glob_search", "list_memories"}

DANGEROUS_BASH = [
    (r"\bsudo\b", "sudo"),
    (r"\brm\s+(-[a-zA-Z]*)?r", "recursive delete"),
    (r"\bmkfs\b", "format disk"),
    (r"\bdd\s+if=", "disk dump"),
    (r">\s*/dev/", "write to device"),
]


class PermissionManager:
    def __init__(self, mode: str = None):
        self.mode = mode or config.PERMISSION_MODE
        if self.mode not in MODES:
            self.mode = "default"

    def check(self, tool_name: str, tool_input: dict) -> dict:
        """Returns {"behavior": "allow"|"deny"|"ask", "reason": str}."""
        # Step 0: bash security validation
        if tool_name == "bash":
            command = tool_input.get("command", "")
            for pattern, label in DANGEROUS_BASH:
                if re.search(pattern, command):
                    return {"behavior": "deny", "reason": f"Blocked: {label} detected in command"}
            classification = classify_bash(command)
            if classification["dangerous"]:
                return {"behavior": "deny", "reason": f"Blocked: {classification['reason']}"}
            if self.mode == "plan":
                if classification["mutating"] or not is_known_read_only_command(command):
                    return {"behavior": "deny", "reason": f"Plan mode: shell blocked ({classification['reason']})"}
                return {"behavior": "allow", "reason": "Plan mode: read-only shell allowed"}
            if self.mode == "auto":
                return {"behavior": "allow", "reason": "Auto mode"}
            if classification["mutating"] or not is_known_read_only_command(command):
                return {"behavior": "ask", "reason": f"Shell command needs approval: {classification['reason']}"}
            return {"behavior": "allow", "reason": "Read-only shell command"}

        # Step 1: plan mode blocks all writes
        if self.mode == "plan":
            if tool_name in WRITE_TOOLS:
                return {"behavior": "deny", "reason": "Plan mode: write operations blocked"}
            return {"behavior": "allow", "reason": "Plan mode: read-only allowed"}

        # Step 2: read tools always allowed
        if tool_name in READ_TOOLS or tool_name in ("todo", "compact", "task", "load_skill"):
            return {"behavior": "allow", "reason": "Safe tool"}

        # Step 3: auto mode allows writes without asking
        if self.mode == "auto":
            return {"behavior": "allow", "reason": "Auto mode"}

        # Step 4: default mode asks for writes
        if tool_name in WRITE_TOOLS:
            return {"behavior": "ask", "reason": f"Write operation: {tool_name}"}

        return {"behavior": "allow", "reason": "Default allow"}

    def ask_user(self, tool_name: str, tool_input: dict) -> bool:
        """Interactive confirmation. Returns True if user approves."""
        import json
        preview = json.dumps(tool_input, ensure_ascii=False)[:300]
        print(f"\n  [Permission] {tool_name}: {preview}")
        try:
            answer = input("  Allow? (y/n/a=always): ").strip().lower()
        except (EOFError, KeyboardInterrupt):
            return False
        if answer == "a":
            self.mode = "auto"
            return True
        return answer in ("y", "yes")
