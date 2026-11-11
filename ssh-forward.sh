#!/usr/bin/env bash
set -euo pipefail
# Each accepted host-local TCP connection runs a stream relay into the
# existing container's loopback-only OpenSSH daemon. No network interface,
# Podman port publication, new bind mount, or external route is required.
exec podman exec -i --user 1000:1000 claude-dev-isolated \
  /usr/bin/socat STDIO TCP:127.0.0.1:22022
