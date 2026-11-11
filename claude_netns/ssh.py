"""Existing host LAN SSH listener and per-connection Podman relay."""
from __future__ import annotations

import os
import re
import time

from .common import CONTAINER, LOGS, ROOT, ControlError, exec_in_container, output, podman
from .gateway import start

LISTEN_ADDRESS = "192.168.50.200"
LISTEN_PORT = "22222"


def host_address_ready() -> bool:
    addresses = output("ip", "-4", "-o", "addr", "show")
    return any(
        re.search(rf"\binet\s+{re.escape(LISTEN_ADDRESS)}/", line)
        for line in addresses.splitlines()
    )


def sshd_listening() -> bool:
    sockets = exec_in_container("/usr/bin/ss", "-lnt").stdout
    return bool(re.search(r"127\.0\.0\.1:22022\s", sockets))


def serve() -> None:
    for _ in range(60):
        if host_address_ready():
            break
        time.sleep(1)
    if not host_address_ready():
        raise ControlError(f"LAN address {LISTEN_ADDRESS} is unavailable; refusing to start the isolated Pod.")
    start()
    exec_in_container("/usr/sbin/sshd", "-t", "-f", "/etc/ssh/sshd_config_claude", user="0")
    if not sshd_listening():
        podman(
            "exec", "--detach", "--user", "0", CONTAINER, "/usr/sbin/sshd",
            "-D", "-e", "-f", "/etc/ssh/sshd_config_claude",
        )
    for _ in range(40):
        if sshd_listening():
            break
        time.sleep(0.1)
    else:
        raise ControlError("The isolated SSH daemon failed to listen on 127.0.0.1:22022.")

    os.execv(
        "/usr/bin/socat",
        [
            "/usr/bin/socat", "-ly", "-lf", str(LOGS / "ssh-relay.log"),
            f"TCP-LISTEN:{LISTEN_PORT},bind={LISTEN_ADDRESS},reuseaddr,fork",
            f"EXEC:{ROOT / 'control.sh'} relay",
        ],
    )


def relay() -> None:
    # Each connection has no published Podman port or direct network route.
    os.execvp(
        "podman",
        [
            "podman", "exec", "-i", "--user", "1000:1000",
            CONTAINER, "/usr/bin/socat", "STDIO", "TCP:127.0.0.1:22022",
        ],
    )
