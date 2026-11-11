"""The single control interface, reached through control.sh."""
from __future__ import annotations

import argparse
import sys

from .build import build_xray
from .common import CONTAINER, ControlError, is_container_running
from .gateway import start, stop
from .mounts import mount_command
from .resources import SingleMount, resource_command
from .ssh import relay, serve


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="control.sh", description="claude-netns runtime controller")
    parser.add_argument(
        "command", choices=(
            "start", "stop", "serve", "relay", "status", "mount",
            "wayland", "kvm", "build-xray",
        )
    )
    parser.add_argument("action", nargs="?")
    parser.add_argument("target", nargs="?")
    args = parser.parse_args(argv)

    if args.command in ("start", "stop", "serve", "relay", "status", "build-xray"):
        if args.action is not None or args.target is not None:
            parser.error(f"{args.command} takes no arguments")
    if args.command == "start":
        start()
    elif args.command == "stop":
        stop()
    elif args.command == "serve":
        serve()
    elif args.command == "relay":
        relay()
    elif args.command == "status":
        print(f"Container {CONTAINER}: {'running' if is_container_running() else 'stopped'}")
        SingleMount("wayland").status()
        SingleMount("kvm").status()
    elif args.command == "mount":
        if args.action is None:
            parser.error("mount requires apply|list|remove|remove-all")
        mount_command(args.action, args.target)
    elif args.command in ("wayland", "kvm"):
        if args.action not in ("enable", "disable", "status", "restore", "ready") or args.target is not None:
            parser.error(f"{args.command} requires enable|disable|status")
        if not resource_command(args.command, args.action):
            return 1
    elif args.command == "build-xray":
        build_xray()
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except (ControlError, OSError) as error:
        print(f"control.sh: {error}", file=sys.stderr)
        sys.exit(1)
