"""Linux open_tree(2) / move_mount(2) in the Podman rootless user namespace.

Called only as: podman unshare python3 -m claude_netns.mountapi attach PID SRC DST KIND
The detached source mount is opened before setns() changes the mount namespace.
"""
from __future__ import annotations

import ctypes
import os
import stat
import sys

AT_FDCWD = -100
OPEN_TREE_CLONE = 1
MOVE_MOUNT_F_EMPTY_PATH = 4
MOVE_MOUNT_T_EMPTY_PATH = 0x40
CLONE_NEWNS = 0x20000
# Linux x86-64 syscall numbers, matching the original rootless implementation.
SYS_OPEN_TREE = 428
SYS_MOVE_MOUNT = 429


def attach(pid: int, source: str, target: str, kind: str) -> None:
    modes = {"dir": stat.S_ISDIR, "socket": stat.S_ISSOCK, "char": stat.S_ISCHR}
    if kind not in modes:
        raise ValueError(f"Unsupported mount type: {kind}")
    libc = ctypes.CDLL(None, use_errno=True)

    def checked(result: int, operation: str) -> int:
        if result < 0:
            raise OSError(ctypes.get_errno(), operation)
        return result

    source_fd = checked(libc.syscall(
        SYS_OPEN_TREE, ctypes.c_int(AT_FDCWD),
        ctypes.c_char_p(os.fsencode(source)),
        ctypes.c_uint(OPEN_TREE_CLONE | os.O_CLOEXEC),
    ), "open_tree")
    try:
        if not modes[kind](os.fstat(source_fd).st_mode):
            raise ValueError(f"Source has incorrect type for {kind}: {source}")
        flags = os.O_PATH | os.O_NOFOLLOW
        if kind == "dir":
            flags |= os.O_DIRECTORY
        target_fd = os.open(f"/proc/{pid}/root{target}", flags)
        try:
            target_mode = os.fstat(target_fd).st_mode
            if kind != "dir" and not stat.S_ISREG(target_mode):
                raise ValueError(f"Target is not a file placeholder: {target}")
            ns_fd = os.open(f"/proc/{pid}/ns/mnt", os.O_RDONLY)
            try:
                checked(libc.setns(ctypes.c_int(ns_fd), ctypes.c_int(CLONE_NEWNS)), "setns")
                checked(libc.syscall(
                    SYS_MOVE_MOUNT, ctypes.c_int(source_fd), ctypes.c_char_p(b""),
                    ctypes.c_int(target_fd), ctypes.c_char_p(b""),
                    ctypes.c_uint(MOVE_MOUNT_F_EMPTY_PATH | MOVE_MOUNT_T_EMPTY_PATH),
                ), "move_mount")
            finally:
                os.close(ns_fd)
        finally:
            os.close(target_fd)
    finally:
        os.close(source_fd)


if __name__ == "__main__":
    if len(sys.argv) != 6 or sys.argv[1] != "attach":
        sys.exit("Usage: mountapi attach PID SOURCE TARGET dir|socket|char")
    attach(int(sys.argv[2]), sys.argv[3], sys.argv[4], sys.argv[5])
