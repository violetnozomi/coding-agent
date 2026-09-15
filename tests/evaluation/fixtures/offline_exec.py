"""Linux smoke launcher: inherited kernel denial of non-Unix networking."""
from __future__ import annotations

import ctypes
import errno
import os
import socket
import sys


def isolate_network():
    lib = ctypes.CDLL("libseccomp.so.2", use_errno=True)
    lib.seccomp_init.argtypes = [ctypes.c_uint32]
    lib.seccomp_init.restype = ctypes.c_void_p
    lib.seccomp_syscall_resolve_name.argtypes = [ctypes.c_char_p]
    lib.seccomp_rule_add.argtypes = [ctypes.c_void_p, ctypes.c_uint32, ctypes.c_int, ctypes.c_uint]
    lib.seccomp_load.argtypes = [ctypes.c_void_p]
    lib.seccomp_release.argtypes = [ctypes.c_void_p]

    class Comparison(ctypes.Structure):
        _fields_ = [("arg", ctypes.c_uint), ("op", ctypes.c_int),
                    ("a", ctypes.c_uint64), ("b", ctypes.c_uint64)]

    ctx = lib.seccomp_init(0x7FFF0000)  # SCMP_ACT_ALLOW
    if not ctx:
        raise RuntimeError("seccomp initialization failed")
    try:
        for name in (b"socket", b"socketpair"):
            number = lib.seccomp_syscall_resolve_name(name)
            assert number >= 0
            # SCMP_CMP_NE: only AF_UNIX may be created, including by children.
            assert lib.seccomp_rule_add(ctx, 0x50000 | errno.EPERM, number, 1,
                                        Comparison(0, 1, socket.AF_UNIX, 0)) == 0
        number = lib.seccomp_syscall_resolve_name(b"io_uring_setup")
        if number >= 0:
            assert lib.seccomp_rule_add(ctx, 0x50000 | errno.EPERM, number, 0) == 0
        assert lib.seccomp_load(ctx) == 0
    finally:
        lib.seccomp_release(ctx)
    for family in (socket.AF_INET, socket.AF_INET6):
        try:
            socket.socket(family)
        except PermissionError:
            continue
        raise RuntimeError("non-local socket was not denied")


if __name__ == "__main__":
    isolate_network()
    print("sandbox: IPv4/IPv6 sockets denied; Unix HTTP transport only", file=sys.stderr, flush=True)
    os.execvpe(sys.argv[1], sys.argv[1:], os.environ)
