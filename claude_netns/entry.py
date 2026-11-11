"""Enter the running development container through the enter.sh interface."""
from __future__ import annotations

import os
import sys

from .common import CONTAINER, ControlError, is_container_running
from .resources import SingleMount


def main() -> None:
    if not is_container_running():
        raise ControlError("Container is not running. Run ./control.sh start first.")
    environment: list[str] = []
    wayland = SingleMount("wayland")
    if wayland.enabled():
        wayland.restore()
        if wayland.ready():
            environment = [
                "--env", "XDG_RUNTIME_DIR=/run/user/1000",
                "--env", "WAYLAND_DISPLAY=wayland-claude",
            ]
    os.execvp(
        "podman",
        [
            "podman", "exec", "-it", "--user", "1000:1000",
            "--workdir", "/home/claude", *environment,
            CONTAINER, "/usr/bin/bash",
        ],
    )


if __name__ == "__main__":
    try:
        if len(sys.argv) != 1:
            raise ControlError("enter.sh accepts no arguments")
        main()
    except (ControlError, OSError) as error:
        print(f"enter.sh: {error}", file=sys.stderr)
        sys.exit(1)
