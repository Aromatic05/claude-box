"""Opt-in Wayland socket and KVM mounts, sharing one namespace implementation."""
from __future__ import annotations

import os
import re
import stat
import sys
from pathlib import Path

from .common import (
    CONTAINER, GID, ROOT, RUNTIME, UID, ControlError, command, container_exists,
    container_file_id, container_pid, container_stat, enable_flag, file_id, guest,
    is_container_running, managed_state, mount_into_container, mounted_at, ns_id,
    output, remove_state, saved_state, unmount_from_container,
)

WAYLAND_TARGET = "/run/user/1000/wayland-claude"
KVM_TARGET = "/dev/kvm"
FISH_CONFIG = ROOT / "home/.config/fish/conf.d/claude-wayland.fish"
FISH_MARKER = "# Managed by claude-netns"
FISH_CONTENT = f"""\
{FISH_MARKER}
if test -S {WAYLAND_TARGET}
    set -gx XDG_RUNTIME_DIR /run/user/1000
    set -gx WAYLAND_DISPLAY wayland-claude
end
"""


def active_wayland_socket() -> Path:
    names = [os.environ.get("WAYLAND_DISPLAY", "")]
    result = command("systemctl", "--user", "show-environment", check=False)
    if result.returncode == 0:
        names.extend(
            line.partition("=")[2] for line in result.stdout.splitlines()
            if line.startswith("WAYLAND_DISPLAY=")
        )
    for name in names:
        if not re.fullmatch(r"wayland-[0-9]+", name):
            continue
        path = Path(f"/run/user/{UID}/{name}")
        try:
            source = path.stat()
        except FileNotFoundError:
            continue
        if not stat.S_ISSOCK(source.st_mode):
            continue
        if (source.st_uid, source.st_gid) != (UID, GID):
            raise ControlError("Host Wayland socket must belong to UID:GID 1000:1000")
        return path
    raise ControlError("No active Wayland socket found for UID 1000")


def host_kvm() -> Path:
    path = Path(KVM_TARGET)
    try:
        node = path.stat()
    except FileNotFoundError as exc:
        raise ControlError("Host /dev/kvm is unavailable") from exc
    if not stat.S_ISCHR(node.st_mode) or (os.major(node.st_rdev), os.minor(node.st_rdev)) != (10, 232):
        raise ControlError("Unexpected /dev/kvm character device")
    return path


class SingleMount:
    def __init__(self, kind: str):
        if kind not in ("wayland", "kvm"):
            raise ValueError(f"Unknown mount: {kind}")
        self.kind = kind
        self.target = WAYLAND_TARGET if kind == "wayland" else KVM_TARGET
        self.flag = RUNTIME / f"{kind}.enabled"
        self.state = RUNTIME / f"{kind}.state"

    def source(self) -> Path:
        return active_wayland_socket() if self.kind == "wayland" else host_kvm()

    def enabled(self) -> bool:
        return self.flag.is_file()

    def known(self, ns: str) -> bool:
        previous = saved_state(self.state)
        if previous is None or previous[0] != ns:
            return False
        return previous[1] == container_file_id(self.target)

    def ready(self) -> bool:
        if not self.enabled() or not self.state.is_file() or not is_container_running():
            return False
        try:
            source = self.source()
            ns = ns_id(container_pid())
            return (
                mounted_at(self.target)
                and self.known(ns)
                and container_file_id(self.target) == file_id(source)
            )
        except (ControlError, OSError):
            return False

    def status(self) -> None:
        if not self.enabled():
            print(f"{self.kind.upper() if self.kind == 'kvm' else 'Wayland socket'} integration is disabled.")
        elif self.ready():
            if self.kind == "kvm":
                print("KVM connected at /dev/kvm (UID 1000).")
            else:
                print(f"Wayland socket connected at {self.target} (UID 1000).")
        else:
            print(f"{self.kind.upper() if self.kind == 'kvm' else 'Wayland socket'} integration is enabled but not currently connected.")

    def _verify(self, source_identity: str) -> bool:
        if not mounted_at(self.target) or container_file_id(self.target) != source_identity:
            return False
        if self.kind == "wayland":
            return container_stat(self.target, "%u:%g") == "1000:1000"
        return container_stat(self.target, "%t:%T") == "a:e8"

    def attach(self) -> None:
        source = self.source()
        source_identity = file_id(source)
        pid = container_pid()
        ns = ns_id(pid)

        if mounted_at(self.target):
            if not self.known(ns):
                raise ControlError(f"Existing mount at {self.target} is not managed by claude-netns")
            if container_file_id(self.target) == source_identity:
                if self.kind == "kvm":
                    print("KVM already connected: /dev/kvm (container UID 1000)")
                else:
                    print(f"Wayland socket already connected: {self.target} <- {source}")
                return
            unmount_from_container(pid, self.target)
        elif container_exists(self.target) and not self.state.is_file():
            raise ControlError(f"Existing path {self.target} is not managed by claude-netns")

        guest("prepare-socket" if self.kind == "wayland" else "prepare-kvm", self.target)
        mount_into_container(pid, str(source), self.target, "socket" if self.kind == "wayland" else "char")
        try:
            if not self._verify(source_identity):
                raise ControlError(f"{self.kind} mount identity verification failed")
            if self.kind == "kvm":
                print(guest("verify-kvm", user="1000:1000"))
        except Exception:
            if mounted_at(self.target):
                unmount_from_container(pid, self.target)
            guest("remove-placeholder", self.target)
            raise

        managed_state(self.flag, self.state, ns, source_identity)
        if self.kind == "kvm":
            print("KVM connected: /dev/kvm (UID 1000, API and VM creation verified).")
        else:
            print(f"Wayland socket connected: {self.target} <- {source} (UID 1000)")

    def enable(self) -> None:
        self.attach()
        if self.kind == "wayland":
            install_fish_config()
        enable_flag(self.flag)

    def restore(self) -> None:
        if not self.enabled():
            return
        if self.kind == "wayland":
            try:
                self.source()
            except ControlError:
                print("Wayland session unavailable; leaving GUI integration disconnected.", file=sys.stderr)
                return
        self.attach()
        if self.kind == "wayland":
            install_fish_config()

    def disable(self) -> None:
        if is_container_running():
            pid = container_pid()
            ns = ns_id(pid)
            if mounted_at(self.target):
                if not self.known(ns):
                    raise ControlError(f"Existing mount at {self.target} is not managed by claude-netns; refusing to unmount")
                unmount_from_container(pid, self.target)
            if self.state.is_file():
                guest("remove-placeholder", self.target)
        remove_state(self.flag, self.state)
        if self.kind == "wayland":
            remove_fish_config()
            print("Wayland socket disconnected.")
        else:
            print("KVM disconnected.")


def install_fish_config() -> None:
    if FISH_CONFIG.exists() or FISH_CONFIG.is_symlink():
        if not FISH_CONFIG.is_file():
            raise ControlError(f"Existing Fish config is not managed by claude-netns: {FISH_CONFIG}")
        current = FISH_CONFIG.read_text()
        if not current.splitlines() or not current.splitlines()[0].startswith(FISH_MARKER):
            raise ControlError(f"Existing Fish config is not managed by claude-netns: {FISH_CONFIG}")
        if current.splitlines()[1:] == FISH_CONTENT.splitlines()[1:] and current != FISH_CONTENT:
            FISH_CONFIG.write_text(FISH_CONTENT)
        return
    FISH_CONFIG.parent.mkdir(parents=True, exist_ok=True)
    FISH_CONFIG.write_text(FISH_CONTENT)
    FISH_CONFIG.chmod(0o644)


def remove_fish_config() -> None:
    if FISH_CONFIG.is_file() and FISH_CONFIG.read_text().startswith(FISH_MARKER):
        FISH_CONFIG.unlink()


def resource_command(kind: str, action: str) -> bool:
    resource = SingleMount(kind)
    if action == "enable":
        resource.enable()
    elif action == "disable":
        resource.disable()
    elif action == "restore":
        resource.restore()
    elif action == "status":
        resource.status()
    elif action == "ready":
        return resource.ready()
    else:
        raise ControlError(f"Unknown {kind} action: {action}")
    return True
