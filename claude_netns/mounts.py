"""Directory bind mounts from the unified config.toml [[mounts]] entries."""
from __future__ import annotations

import posixpath
from pathlib import Path

from .config import settings
from .common import (
    CONTAINER, ROOT, UID, GID, ControlError, container_file_id,
    container_pid, container_stat, file_id, guest, mount_into_container,
    mounted_at, unmount_from_container,
)

def validate_destination(target: str) -> None:
    if (
        not target.startswith("/")
        or target == "/"
        or target.endswith("/")
        or posixpath.normpath(target) != target
        or "//" in target
    ):
        raise ControlError(f"Invalid or non-normalized container destination: {target}")


def read_config() -> list[tuple[str, Path]]:
    mounts: list[tuple[str, Path]] = []
    seen: set[str] = set()
    for number, entry in enumerate(settings.mounts, 1):
        target, source = entry.target, entry.source
        validate_destination(target)
        if not source.startswith("/"):
            raise ControlError(f"config.toml mounts[{number}]: host source must be absolute")
        source_path = Path(source)
        if not source_path.is_dir():
            raise ControlError(f"config.toml mounts[{number}]: host source is not an existing directory: {source}")
        resolved = source_path.resolve(strict=True)
        info = resolved.stat()
        if (info.st_uid, info.st_gid) != (UID, GID):
            raise ControlError(f"config.toml mounts[{number}]: source {resolved} must belong to UID:GID {UID}:{GID}; refusing to change ownership")
        if target in seen:
            raise ControlError(f"config.toml mounts[{number}]: duplicate destination: {target}")
        seen.add(target)
        mounts.append((target, resolved))
    return mounts


def same_source(target: str, source: Path) -> bool:
    try:
        return container_file_id(target) == file_id(source) and container_stat(target, "%u:%g") == f"{UID}:{GID}"
    except (OSError, ControlError):
        return False


def apply_one(pid: int, target: str, source: Path) -> None:
    if mounted_at(target):
        if not same_source(target, source):
            raise ControlError(f"Existing mount at {target} has a different source; refusing to replace it")
        print(f"already mounted: {target} <- {source}")
        return
    guest("prepare-dir", target)
    mount_into_container(pid, str(source), target, "dir")
    if not mounted_at(target) or not same_source(target, source):
        raise ControlError(f"Mount verification failed: {target} <- {source}")
    print(f"mounted: {target} <- {source} (container UID:GID {UID}:{GID})")


def remove_one(pid: int, target: str, source: Path) -> None:
    if not mounted_at(target):
        print(f"not mounted: {target}")
        return
    if not same_source(target, source):
        raise ControlError(f"Mount at {target} is not the configured source; refusing to unmount")
    unmount_from_container(pid, target)
    print(f"unmounted: {target}")


def mount_command(action: str, target: str | None = None) -> None:
    if action not in ("apply", "list", "remove", "remove-all"):
        raise ControlError(f"Unknown mount action: {action}")
    if action == "remove":
        if target is None:
            raise ControlError("mount remove requires a destination")
        validate_destination(target)
    elif target is not None:
        raise ControlError(f"mount {action} accepts no destination")
    mounts = read_config()
    if not mounts:
        if action == "remove":
            raise ControlError(f"No configured mount for {target}")
        if action == "list":
            print("No dynamic mounts configured.")
        return
    pid = container_pid()
    if action == "apply":
        for destination, source in mounts:
            apply_one(pid, destination, source)
    elif action == "list":
        for destination, source in mounts:
            state = "unmounted"
            if mounted_at(destination):
                state = "mounted" if same_source(destination, source) else "conflict"
            print(f"{state}: {destination} <- {source}")
    elif action == "remove":
        for destination, source in mounts:
            if destination == target:
                remove_one(pid, destination, source)
                break
        else:
            raise ControlError(f"No configured mount for {target}")
    else:
        for destination, source in reversed(mounts):
            remove_one(pid, destination, source)
