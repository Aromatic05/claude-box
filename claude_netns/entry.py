"""Enter the running development container through the enter.sh interface."""
from __future__ import annotations

import os
import sys

def main() -> None:
    from .common import CONTAINER, ControlError, is_container_running
    from .config import settings
    from .resources import SingleMount
    if not is_container_running():
        raise ControlError("Container is not running. Run ./control.sh start first.")
    environment: list[str] = []
    wayland = SingleMount("wayland")
    if wayland.enabled():
        wayland.restore()
        if wayland.ready():
            environment = [
                "--env", f"XDG_RUNTIME_DIR={settings.wayland.runtime_dir}",
                "--env", f"WAYLAND_DISPLAY={settings.wayland.display}",
            ]
    os.execvp(
        "podman",
        [
            "podman", "exec", "-it", "--user", settings.container.user,
            "--workdir", settings.container.home, *environment,
            CONTAINER, "/usr/bin/bash",
        ],
    )


if __name__ == "__main__":
    try:
        if len(sys.argv) != 1:
            raise ValueError("enter.sh accepts no arguments")
        main()
    except (RuntimeError, ValueError, OSError) as error:
        print(f"enter.sh: {error}", file=sys.stderr)
        sys.exit(1)
