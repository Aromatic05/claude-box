"""Existing Podman pod, isolated NetNS, TUN FD handoff and Xray lifecycle."""
from __future__ import annotations

import os
import shutil
import signal
import subprocess
import sys
import time
from pathlib import Path

from .common import (
    CONTAINER, LOGS, POD, ROOT, RUNTIME, ControlError, command,
    exec_in_container, guest, ns_id, output, podman, podman_output,
)
from .mounts import mount_command
from .resources import SingleMount

ARCH_IMAGE = "docker.io/library/archlinux:latest"
PYTHON_IMAGE = "docker.io/library/python:3.12-slim"
TUN = "claude-tun"
BRIDGE = ROOT / "tun-bridge.py"
XRAY = ROOT / "xray"
CONFIG = ROOT / "xray-tun.json"

# The only shell executed for NetNS setup is the bounded command inside
# the existing Arch helper image (which supplies iproute2, not Python).
# It is kept byte-for-byte equivalent to the original network constraints.
NET_SETUP = r"""
for device in $(ip -o link show | cut -d: -f2 | tr -d " " | cut -d@ -f1); do
  case "$device" in lo|claude-tun) ;; *) echo "Unexpected network interface: $device" >&2; exit 1;; esac
done
if ip -4 route show default | grep -v "^default dev claude-tun "; then
  echo "Unexpected IPv4 default route" >&2; exit 1
fi
if ip -6 route show default | grep -v "^unreachable default "; then
  echo "Unexpected IPv6 default route" >&2; exit 1
fi
if ! ip link show dev claude-tun >/dev/null 2>&1; then
  ip tuntap add dev claude-tun mode tun user 1000
fi
ip -4 addr show dev claude-tun | grep -q "inet 10.203.0.2/30 " || \
  ip -4 addr add 10.203.0.2/30 dev claude-tun
ip link set dev claude-tun up
ip -4 route replace default dev claude-tun
ip -6 route replace unreachable default metric 1
"""


def read_pid(path: Path) -> int | None:
    try:
        value = path.read_text().strip()
    except FileNotFoundError:
        return None
    return int(value) if value.isdecimal() and int(value) > 0 else None


def is_running(pid: int | None) -> bool:
    if pid is None or pid <= 0:
        return False
    try:
        os.kill(pid, 0)
        stat_text = Path(f"/proc/{pid}/stat").read_text()
        return stat_text.rsplit(")", 1)[1].strip().split()[0] != "Z"
    except (ProcessLookupError, PermissionError, FileNotFoundError, IndexError, ValueError):
        return False


def bridge_owned(pid: int | None) -> bool:
    if not is_running(pid):
        return False
    try:
        args = Path(f"/proc/{pid}/cmdline").read_bytes().split(b"\0")
        return os.fsencode(BRIDGE) in args
    except (OSError, ValueError):
        return False


def xray_owned(pid: int | None) -> bool:
    if not is_running(pid):
        return False
    try:
        return os.readlink(f"/proc/{pid}/exe") == str(XRAY)
    except OSError:
        return False


def wait_stopped(pid: int, *, count: int = 60) -> bool:
    for _ in range(count):
        if not is_running(pid):
            return True
        time.sleep(0.1)
    return not is_running(pid)


def stop_owned_process(pid: int | None, *, bridge: bool) -> None:
    if not is_running(pid):
        return
    checker = bridge_owned if bridge else xray_owned
    name = "bridge" if bridge else "Xray"
    if not checker(pid):
        raise ControlError(f"Refusing to signal unknown {name} process: {pid}")
    os.kill(pid, signal.SIGTERM)
    if not wait_stopped(pid):
        raise ControlError(f"The previous dedicated {name} did not stop: {pid}")


def clear_gateway_state() -> None:
    for name in ("bridge.pid", "xray.pid", "bridge.netns", "tunfd.sock"):
        (RUNTIME / name).unlink(missing_ok=True)


def tun_ready() -> bool:
    result = exec_in_container("ip", "-o", "link", "show", "dev", TUN, check=False)
    return result.returncode == 0 and "LOWER_UP" in result.stdout


def restore_mounts() -> None:
    mount_command("apply")
    for kind, description in (("wayland", "Wayland socket"), ("kvm", "KVM device")):
        try:
            SingleMount(kind).restore()
        except (ControlError, OSError, subprocess.SubprocessError) as exc:
            print(f"{description} restore failed; network gateway is unaffected: {exc}", file=sys.stderr)


def check_existing_objects() -> str:
    if (
        podman("pod", "exists", POD, check=False).returncode
        or podman("container", "exists", CONTAINER, check=False).returncode
    ):
        raise ControlError("Dedicated Podman pod/container is missing; not creating or changing another one.")
    for path in (BRIDGE, XRAY, CONFIG):
        if not path.is_file():
            raise ControlError(f"Required gateway file is missing: {path}")

    infra = podman_output("pod", "inspect", POD, "--format", "{{.InfraContainerID}}")
    network = podman_output("inspect", infra, "--format", "{{.HostConfig.NetworkMode}}")
    container_network = podman_output("inspect", CONTAINER, "--format", "{{.HostConfig.NetworkMode}}")
    mount_count = podman_output("inspect", CONTAINER, "--format", "{{len .Mounts}}")
    mount_source = podman_output("inspect", CONTAINER, "--format", "{{(index .Mounts 0).Source}}")
    if (
        network != "none" or container_network != f"container:{infra}"
        or mount_count != "1" or mount_source != str(ROOT / "home")
    ):
        raise ControlError("Isolation check failed: unexpected Pod network or container mount.")
    return infra


def ensure_netns(infra: str) -> tuple[str, int]:
    if podman_output("pod", "inspect", POD, "--format", "{{.State}}") != "Running":
        podman("pod", "start", POD, capture=False)
    if podman_output("inspect", CONTAINER, "--format", "{{.State.Status}}") != "running":
        podman("start", CONTAINER, capture=False)

    infra_pid = int(podman_output("inspect", infra, "--format", "{{.State.Pid}}"))
    netns = ns_id(infra_pid, "net")
    container_pid = int(podman_output("inspect", CONTAINER, "--format", "{{.State.Pid}}"))
    container_ns = ns_id(container_pid, "net")
    if container_ns != netns:
        print(f"Container NetNS {container_ns} differs from Pod NetNS {netns}; restarting {CONTAINER}.", file=sys.stderr)
        podman("stop", "-t", "5", CONTAINER, capture=False)
        podman("start", CONTAINER, capture=False)
        container_pid = int(podman_output("inspect", CONTAINER, "--format", "{{.State.Pid}}"))
        if ns_id(container_pid, "net") != netns:
            raise ControlError("Container and Pod NetNS still differ after restart; refusing to start gateway.")
    return netns, infra_pid


def start() -> None:
    infra = check_existing_objects()
    netns, infra_pid = ensure_netns(infra)

    podman(
        "run", "--rm", "--pod", POD, "--user", "0", "--cap-add", "NET_ADMIN",
        "--device", "/dev/net/tun", "--entrypoint", "/bin/sh",
        ARCH_IMAGE, "-ceu", NET_SETUP, capture=False,
    )
    guest("ensure-resolver")
    RUNTIME.mkdir(parents=True, exist_ok=True)
    LOGS.mkdir(parents=True, exist_ok=True)
    LOGS.chmod(0o700)
    RUNTIME.chmod(0o777)  # short-lived Podman helper has a subordinate host UID

    bridge_pid = read_pid(RUNTIME / "bridge.pid")
    xray_pid = read_pid(RUNTIME / "xray.pid")
    previous_ns = (RUNTIME / "bridge.netns").read_text().strip() if (RUNTIME / "bridge.netns").is_file() else ""

    if bridge_owned(bridge_pid) and xray_owned(xray_pid) and previous_ns == netns and tun_ready():
        restore_mounts()
        print(f"Dedicated Xray already running (PID {xray_pid}, NetNS {netns}).")
        return

    stop_owned_process(bridge_pid, bridge=True)
    stop_owned_process(xray_pid, bridge=False)
    clear_gateway_state()
    shutil.copyfile(BRIDGE, RUNTIME / "helper.py")
    (RUNTIME / "helper.py").chmod(0o644)

    # Own only the dedicated bridge/Xray processes started by this invocation.
    try:
        with (LOGS / "bridge.log").open("wb") as bridge_log:
            subprocess.Popen(
                [
                    sys.executable, str(BRIDGE), "serve",
                    "--socket", str(RUNTIME / "tunfd.sock"),
                    "--name", TUN, "--expected-ns", netns,
                    "--namespace-pid", str(infra_pid),
                    "--config", str(CONFIG), "--runtime", str(RUNTIME),
                    "--log", str(LOGS / "xray.log"), "--xray", str(XRAY),
                ],
                cwd=ROOT, stdin=subprocess.DEVNULL, stdout=bridge_log,
                stderr=subprocess.STDOUT, start_new_session=True,
            )

        for _ in range(40):
            if (RUNTIME / "tunfd.sock").is_socket():
                break
            time.sleep(0.1)
        else:
            tail = (LOGS / "bridge.log").read_text(errors="replace").splitlines()[-12:]
            raise ControlError("TUN FD receiver did not start.\n" + "\n".join(tail))

        # Only the short-lived helper opens /dev/net/tun in the dedicated Pod.
        podman(
            "run", "--rm", "--pod", POD, "--user", "0", "--cap-add", "NET_ADMIN",
            "--device", "/dev/net/tun", "--volume", f"{RUNTIME}:/handoff:rw",
            "--entrypoint", "python3", PYTHON_IMAGE,
            "/handoff/helper.py", "send", "--socket", "/handoff/tunfd.sock",
            "--name", TUN, capture=False,
        )
        xray_pid = read_pid(RUNTIME / "xray.pid")
        if not xray_owned(xray_pid):
            raise ControlError("Dedicated Xray failed to stay running; namespace remains fail-closed.")
        for _ in range(20):
            if tun_ready():
                break
            time.sleep(0.1)
        else:
            raise ControlError("TUN has no carrier after Xray startup; namespace remains fail-closed.")
        restore_mounts()
    except BaseException:
        # Preserve failure cleanup semantics: clean up only owned processes.
        bridge_pid = read_pid(RUNTIME / "bridge.pid")
        xray_pid = read_pid(RUNTIME / "xray.pid")
        if bridge_owned(bridge_pid):
            os.kill(bridge_pid, signal.SIGTERM)
            wait_stopped(bridge_pid)
        if xray_owned(xray_pid):
            os.kill(xray_pid, signal.SIGTERM)
        clear_gateway_state()
        raise

    print(f"Started {CONTAINER} with {TUN} (Xray PID {xray_pid}, NetNS {netns}).")


def stop() -> None:
    if (
        os.environ.get("CLAUDE_NETNS_FROM_SYSTEMD") != "1"
        and command("systemctl", "--user", "is-active", "--quiet",
                    "claude-netns.service", check=False).returncode == 0
    ):
        command("systemctl", "--user", "stop", "claude-netns.service", capture=False)
        return
    bridge_pid = read_pid(RUNTIME / "bridge.pid")
    xray_pid = read_pid(RUNTIME / "xray.pid")

    if is_running(bridge_pid) and not bridge_owned(bridge_pid):
        raise ControlError(f"Refusing to signal unknown bridge process: {bridge_pid}")
    if is_running(xray_pid) and not xray_owned(xray_pid):
        raise ControlError(f"Refusing to signal unknown Xray process: {xray_pid}")

    if bridge_owned(bridge_pid):
        stop_owned_process(bridge_pid, bridge=True)
    if is_running(xray_pid):
        stop_owned_process(xray_pid, bridge=False)

    if podman("pod", "exists", POD, check=False).returncode == 0:
        if podman_output("pod", "inspect", POD, "--format", "{{.State}}") == "Running":
            podman("pod", "stop", "-t", "2", POD, capture=False)
    clear_gateway_state()
    print(f"Stopped dedicated Xray bridge and {POD}.")
