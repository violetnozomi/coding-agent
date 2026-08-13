"""Dependency-free shell completion generation for the NZ-Coder CLI."""
from __future__ import annotations

import sys
from typing import TextIO


_COMMANDS = "run init doctor serve mcp provider-smoke swebench models extensions completion"
_RUN_FLAGS = (
    "--cwd --provider --model --variant --effort --permission-mode --session "
    "--continue --resume --no-session --max-turns --file --attach --output"
)
_VALUES = "default auto plan acceptEdits text json jsonl"


def _bash() -> str:
    return f'''_nz_coder_complete() {{
  local cur="${{COMP_WORDS[COMP_CWORD]}}"
  local words="{_COMMANDS} {_RUN_FLAGS} {_VALUES}"
  COMPREPLY=( $(compgen -W "$words" -- "$cur") )
}}
complete -F _nz_coder_complete nz-coder
'''


def _zsh() -> str:
    return f'''#compdef nz-coder
_nz_coder() {{
  local -a words
  words=({_COMMANDS} {_RUN_FLAGS} {_VALUES})
  _describe 'nz-coder command or option' words
}}
compdef _nz_coder nz-coder
'''


def _fish() -> str:
    words = [*_COMMANDS.split(), *_RUN_FLAGS.split(), *_VALUES.split()]
    return "".join(
        f"complete -c nz-coder -f -a '{word}'\n" for word in words
    )


def completion_main(
    argv: list[str] | None = None,
    *,
    stdout: TextIO | None = None,
    stderr: TextIO | None = None,
) -> int:
    """Print a completion script for bash, zsh, or fish."""
    args = list(sys.argv[1:] if argv is None else argv)
    output = stdout or sys.stdout
    errors = stderr or sys.stderr
    if len(args) != 1 or args[0] not in {"bash", "zsh", "fish"}:
        errors.write("Usage: nz-coder completion <bash, zsh, or fish>\n")
        return 2
    scripts = {"bash": _bash, "zsh": _zsh, "fish": _fish}
    output.write(scripts[args[0]]())
    return 0


__all__ = ["completion_main"]
