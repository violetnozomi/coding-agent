"""为已安装的外部 CPython 获取与实际 Bash 调用绑定的字节码标记。"""
from __future__ import annotations

from contextlib import contextmanager
import os
from pathlib import Path
import re
import shlex
import shutil
import sys
import tempfile


def _installed_identity(executable: str, workspace: Path):
    try:
        binary = Path(executable).resolve(strict=True)
        prefix = binary.parent.parent
        match = re.fullmatch(r"python(3)\.(\d+)", binary.name)
        if not match or binary.parent.name != "bin" or prefix.is_relative_to(workspace):
            return None
        host = Path(sys.base_prefix).resolve()
        conda = host.parent.parent if host.parent.name == "envs" else host
        managed_conda = ((prefix == conda or prefix.parent == conda / "envs")
                         and (conda / "conda-meta/history").is_file()
                         and (prefix / "conda-meta/history").is_file())
        if prefix not in {host, Path("/usr"), Path("/usr/local")} and not managed_conda:
            return None
        with binary.open("rb") as stream:
            if stream.read(4) != b"\x7fELF":
                return None
            identity = os.fstat(stream.fileno())
        tag = "cpython-" + "".join(match.groups())
        cache = prefix / "lib" / binary.name / "encodings" / "__pycache__" / f"__init__.{tag}.pyc"
        if not cache.resolve(strict=True).is_relative_to(prefix) or cache.resolve().is_relative_to(workspace):
            return None
        with cache.open("rb") as stream:
            magic = stream.read(4)
        if len(magic) == 4 and magic.endswith(b"\r\n"):
            return binary, identity.st_dev, identity.st_ino, identity.st_ctime_ns, tag, magic.hex()
    except (OSError, RuntimeError):
        pass
    return None


@contextmanager
def capture_python_identity(shell, command, tokens, environment, cwd, workspace, metadata):
    from nz_coder.runtime.verification.domain.identity import command_identity

    direct = (sys.platform.startswith("linux") and shell.kind.value == "bash"
              and len(tokens) >= 3 and tokens[1:3] in (["-m", "unittest"], ["-m", "pytest"])
              and re.fullmatch(r"python(?:3(?:\.\d+)?)?", Path(tokens[0]).name)
              and not command_identity(command, str(cwd)).opaque)
    path = None
    if direct:
        path = str(cwd / tokens[0]) if "/" in tokens[0] else shutil.which(tokens[0], path=environment.get("PATH"))
    origin = _installed_identity(path, workspace) if path and Path(path).is_absolute() else None
    if not origin or origin[-2] == sys.implementation.cache_tag:
        yield command, ()
        return
    try:
        receipt = tempfile.TemporaryFile()
    except OSError:
        yield command, ()
        return
    with receipt:
        fd = receipt.fileno()
        name = shlex.quote(tokens[0])
        # 在真正的登录 shell 中核对命令解析；交给项目之前即关闭私有回执 FD。
        probe = (f'if [[ $(builtin type -t -- {name}) == file && '
                 f'"$(builtin type -p -- {name})" -ef {shlex.quote(str(origin[0]))} ]]; '
                 f'then builtin printf 1 >&{fd}; fi\nexec {fd}>&-\n')
        yield probe + command, (fd,)
        receipt.seek(0)
        if receipt.read(2) == b"1" and _installed_identity(path, workspace) == origin:
            metadata["python_bytecode_identity"] = [origin[-2], origin[-1]]
