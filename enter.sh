#!/usr/bin/env bash
set -euo pipefail

CONTAINER='claude-dev-isolated'
if [[ "$(podman inspect "$CONTAINER" --format '{{.State.Status}}' 2>/dev/null || true)" != 'running' ]]; then
  echo 'Container is not running. Run ./start.sh first.' >&2
  exit 1
fi
exec podman exec -it --user 1000:1000 --workdir /home/claude "$CONTAINER" /usr/bin/bash
