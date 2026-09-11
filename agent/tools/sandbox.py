"""Child interpreter: fail-closed Linux seccomp syscall allowlist.
No file opens or network syscalls are allowed after initialization.
"""

import ctypes
import errno
import json
import resource
import sys

code = sys.stdin.read(6001)
resource.setrlimit(resource.RLIMIT_CPU, (2, 2))
resource.setrlimit(resource.RLIMIT_AS, (128 * 1024**2, 128 * 1024**2))
resource.setrlimit(resource.RLIMIT_FSIZE, (0, 0))
lib = ctypes.CDLL("libseccomp.so.2")
lib.seccomp_init.argtypes = [ctypes.c_uint32]
lib.seccomp_init.restype = ctypes.c_void_p
lib.seccomp_rule_add.argtypes = [ctypes.c_void_p, ctypes.c_uint32, ctypes.c_int, ctypes.c_uint]
lib.seccomp_syscall_resolve_name.argtypes = [ctypes.c_char_p]
lib.seccomp_load.argtypes = [ctypes.c_void_p]
ctx = lib.seccomp_init(0x00050000 | errno.EPERM)
if not ctx:
    sys.exit(70)
for name in (
    "read",
    "write",
    "close",
    "exit",
    "exit_group",
    "brk",
    "mmap",
    "munmap",
    "mprotect",
    "mremap",
    "futex",
    "rt_sigaction",
    "rt_sigprocmask",
    "rt_sigreturn",
    "sigaltstack",
    "clock_gettime",
    "getrandom",
):
    number = lib.seccomp_syscall_resolve_name(name.encode())
    if number < 0 or lib.seccomp_rule_add(ctx, 0x7FFF0000, number, 0) != 0:
        sys.exit(70)
if lib.seccomp_load(ctx) != 0:
    sys.exit(70)
# The kernel is the security boundary, not restricted builtins.
try:
    exec(compile(code, "<sandbox>", "exec"), {"__name__": "__main__"})
except BaseException as exc:
    print(json.dumps({"error": type(exc).__name__, "message": str(exc)[:300]}))
    sys.exit(1)
