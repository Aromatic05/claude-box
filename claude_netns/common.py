"""Shared process, Podman, runtime, and ownership utilities."""
from __future__ import annotations

import os
import subprocess
from pathlib import Path

from .config import settings

ROOT = Path(__file__).resolve().parent.parent
RUNTIME = ROOT / "runtime"
LOGS = ROOT / "logs"
CONTAINER = settings.container.name
POD = settings.container.pod
UID = settings.container.uid
GID = settings.container.gid


class ControlError(RuntimeError):
    pass


def command(*args: str | os.PathLike[str], check: bool = True,
            input_text: str | None = None, capture: bool = True,
            env: dict[str, str] | None = None, cwd: Path = ROOT) -> subprocess.CompletedProcess[str]:
    argv = [os.fspath(arg) for arg in args]
    result = subprocess.run(
        argv, cwd=cwd, text=True, input=input_text, env=env,
        stdout=subprocess.PIPE if capture else None,
        stderr=subprocess.PIPE if capture else None,
        check=False,
    )
    if check and result.returncode:
        detail = (result.stderr or result.stdout or "").strip()
        raise ControlError(f"{' '.join(argv)} failed ({result.returncode}): {detail}")
    return result


def output(*args: str | os.PathLike[str], **kwargs) -> str:
    return command(*args, **kwargs).stdout.strip()


def podman(*args: str, **kwargs) -> subprocess.CompletedProcess[str]:
    return command("podman", *args, **kwargs)


def podman_output(*args: str, **kwargs) -> str:
    return output("podman", *args, **kwargs)


def exec_in_container(*args: str, user: str | None = None,
                      input_text: str | None = None,
                      check: bool = True) -> subprocess.CompletedProcess[str]:
    argv = ["exec"]
    if input_text is not None:
        argv.append("-i")
    if user is not None:
        argv.extend(["--user", user])
    return podman(*argv, CONTAINER, *args, input_text=input_text, check=check)


def guest(operation: str, *args: str, user: str = "0") -> str:
    source = (Path(__file__).parent / "guest.py").read_text()
    return exec_in_container("python3", "-", str(UID), str(GID), operation, *args,
                             user=user, input_text=source).stdout.strip()


def is_container_running() -> bool:
    result = podman("inspect", CONTAINER, "--format", "{{.State.Status}}", check=False)
    return result.returncode == 0 and result.stdout.strip() == "running"


def container_pid() -> int:
    if not is_container_running():
        raise ControlError(f"{CONTAINER} is not running")
    value = podman_output("inspect", CONTAINER, "--format", "{{.State.Pid}}")
    if not value.isdecimal() or int(value) <= 0 or not Path(f"/proc/{value}/ns/mnt").exists():
        raise ControlError(f"No running mount namespace for {CONTAINER}")
    return int(value)


def ns_id(pid: int, kind: str = "mnt") -> str:
    return os.readlink(f"/proc/{pid}/ns/{kind}")


def file_id(path: str | Path) -> str:
    st = os.stat(path)
    return f"{st.st_dev}:{st.st_ino}"


def container_stat(path: str, fmt: str) -> str:
    return exec_in_container("stat", "-Lc", fmt, "--", path).stdout.strip()


def container_file_id(path: str) -> str:
    return container_stat(path, "%d:%i")


def container_exists(path: str, *, flag: str = "-e") -> bool:
    return exec_in_container("test", flag, path, check=False).returncode == 0


def mounted_at(path: str) -> bool:
    return exec_in_container("findmnt", "-n", "-M", path, "-o", "TARGET",
                             check=False).returncode == 0


def mount_into_container(pid: int, source: str, target: str, kind: str) -> None:
    command("podman", "unshare", "python3", "-m", "claude_netns.mountapi",
            "attach", str(pid), source, target, kind)


def unmount_from_container(pid: int, target: str) -> None:
    command("podman", "unshare", "nsenter", "--target", str(pid),
            "--user", "--mount", "--pid", "--root", "--wdns=/",
            "--", "umount", "--", target)


def managed_state(flag: Path, state: Path, namespace: str, identity: str) -> None:
    RUNTIME.mkdir(parents=True, exist_ok=True)
    state.write_text(f"{namespace} {identity}\n")
    state.chmod(0o600)


def saved_state(state: Path) -> tuple[str, str] | None:
    if not state.is_file():
        return None
    fields = state.read_text().split()
    return (fields[0], fields[1]) if len(fields) == 2 else None


def enable_flag(flag: Path) -> None:
    RUNTIME.mkdir(parents=True, exist_ok=True)
    flag.touch()
    flag.chmod(0o600)


def remove_state(*paths: Path) -> None:
    for path in paths:
        path.unlink(missing_ok=True)


def verify_owner(path: Path | str) -> bool:
    st = os.stat(path)
    return (st.st_uid, st.st_gid) == (UID, GID)
