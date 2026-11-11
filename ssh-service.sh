#!/usr/bin/env bash
set -euo pipefail

WORKDIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
CONTAINER='claude-dev-isolated'
LISTEN_ADDRESS='192.168.50.200'
LISTEN_PORT='22222'

# The user service can start before NetworkManager assigns the LAN address.
# Wait before creating the Pod so a failed SSH bind cannot leave it half-started.
host_address_ready() {
  ip -4 -o addr show | awk -v address="$LISTEN_ADDRESS" '
    $3 == "inet" { split($4, parts, "/"); if (parts[1] == address) found = 1 }
    END { exit !found }
  '
}

for ((i=0; i<60; i++)); do
  host_address_ready && break
  sleep 1
done
if ! host_address_ready; then
  echo "LAN address $LISTEN_ADDRESS is unavailable; refusing to start the isolated Pod." >&2
  exit 1
fi

"$WORKDIR/start.sh"

# Ensure the dedicated SSH daemon is available inside this specific container.
# It only listens on loopback inside the isolated network namespace.
podman exec --user 0 "$CONTAINER" /usr/sbin/sshd -t -f /etc/ssh/sshd_config_claude

if ! podman exec "$CONTAINER" /usr/bin/ss -lnt | grep -Eq '127\.0\.0\.1:22022[[:space:]]'; then
  podman exec --detach --user 0 "$CONTAINER" /usr/sbin/sshd -D -e -f /etc/ssh/sshd_config_claude
fi
for ((i=0; i<40; i++)); do
  if podman exec "$CONTAINER" /usr/bin/ss -lnt | grep -Eq '127\.0\.0\.1:22022[[:space:]]'; then
    break
  fi
  sleep 0.1
done
if ! podman exec "$CONTAINER" /usr/bin/ss -lnt | grep -Eq '127\.0\.0\.1:22022[[:space:]]'; then
  echo 'The isolated SSH daemon failed to listen on 127.0.0.1:22022.' >&2
  exit 1
fi

# No published Podman ports or direct network interfaces: each accepted
# connection is streamed through podman exec to the container's SSH daemon.
exec /usr/bin/socat -ly -lf "$WORKDIR/logs/ssh-relay.log" \
  "TCP-LISTEN:${LISTEN_PORT},bind=${LISTEN_ADDRESS},reuseaddr,fork" \
  "EXEC:$WORKDIR/ssh-forward.sh"
