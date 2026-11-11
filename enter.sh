#!/usr/bin/env bash
set -euo pipefail

CONTAINER='claude-dev-isolated'
WORKDIR="$(cd -- "$(dirname -- "$0")" && pwd)"
if [[ "$(podman inspect "$CONTAINER" --format '{{.State.Status}}' 2>/dev/null || true)" != 'running' ]]; then
  echo 'Container is not running. Run ./start.sh first.' >&2
  exit 1
fi
wayland_env=()
if [[ -f "$WORKDIR/runtime/wayland.enabled" ]]; then
  "$WORKDIR/gui.sh" restore
  if "$WORKDIR/gui.sh" ready >/dev/null 2>&1; then
    wayland_env=(--env XDG_RUNTIME_DIR=/run/user/1000 --env WAYLAND_DISPLAY=wayland-claude)
  fi
fi
exec podman exec -it --user 1000:1000 --workdir /home/claude "${wayland_env[@]}" "$CONTAINER" /usr/bin/bash
