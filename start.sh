#!/usr/bin/env bash
set -euo pipefail

POD='claude-private-netns'
CONTAINER='claude-dev-isolated'
TUN='claude-tun'
WORKDIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
ARCH_IMAGE='docker.io/library/archlinux:latest'
PYTHON_IMAGE='docker.io/library/python:3.12-slim'
RUNTIME="$WORKDIR/runtime"
BRIDGE="$WORKDIR/tun-bridge.py"
XRAY="$WORKDIR/xray"
CONFIG="$WORKDIR/xray-tun.json"

# Only operate on the dedicated Pod, never on other Podman objects.
if ! podman pod exists "$POD" || ! podman container exists "$CONTAINER"; then
  echo 'Dedicated Podman pod/container is missing; not creating or changing another one.' >&2
  exit 1
fi
for file in "$BRIDGE" "$XRAY" "$CONFIG"; do
  if [[ ! -f "$file" ]]; then
    echo "Required gateway file is missing: $file" >&2
    exit 1
  fi
done

infra="$(podman pod inspect "$POD" --format '{{.InfraContainerID}}')"
network="$(podman inspect "$infra" --format '{{.HostConfig.NetworkMode}}')"
container_network="$(podman inspect "$CONTAINER" --format '{{.HostConfig.NetworkMode}}')"
mount_count="$(podman inspect "$CONTAINER" --format '{{len .Mounts}}')"
mount_source="$(podman inspect "$CONTAINER" --format '{{(index .Mounts 0).Source}}')"
if [[ "$network" != 'none' || "$container_network" != "container:$infra" ||
      "$mount_count" != '1' || "$mount_source" != "$WORKDIR/home" ]]; then
  echo 'Isolation check failed: unexpected Pod network or container mount.' >&2
  exit 1
fi

if [[ "$(podman pod inspect "$POD" --format '{{.State}}')" != 'Running' ]]; then
  podman pod start "$POD"
fi
if [[ "$(podman inspect "$CONTAINER" --format '{{.State.Status}}')" != 'running' ]]; then
  podman start "$CONTAINER"
fi

infra_pid="$(podman inspect "$infra" --format '{{.State.Pid}}')"
netns="$(readlink "/proc/$infra_pid/ns/net")"
container_pid="$(podman inspect "$CONTAINER" --format '{{.State.Pid}}')"
container_netns="$(readlink "/proc/$container_pid/ns/net")"
if [[ "$container_netns" != "$netns" ]]; then
  echo "Container NetNS $container_netns differs from Pod NetNS $netns; restarting $CONTAINER." >&2
  podman stop -t 5 "$CONTAINER"
  podman start "$CONTAINER"
  container_pid="$(podman inspect "$CONTAINER" --format '{{.State.Pid}}')"
  container_netns="$(readlink "/proc/$container_pid/ns/net")"
  if [[ "$container_netns" != "$netns" ]]; then
    echo 'Container and Pod NetNS still differ after restart; refusing to start gateway.' >&2
    exit 1
  fi
fi

# Configure only this dedicated network namespace: no pasta/bridge/uplink is
# present. There is only one IPv4 default route, through the named TUN.
podman run --rm --pod "$POD" --user 0 --cap-add NET_ADMIN \
  --device /dev/net/tun --entrypoint /bin/sh "$ARCH_IMAGE" -ceu '
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
'

# Resolver settings live only in the development container. DNS packets use
# the same TUN route; there is no direct network device in the namespace.
podman exec --user 0 "$CONTAINER" /bin/sh -ceu '
  if [ ! -e /etc/resolv.conf ]; then
    printf "nameserver 1.1.1.1\noptions timeout:2 attempts:2\n" > /etc/resolv.conf
    chmod 0644 /etc/resolv.conf
  fi
'

mkdir -p "$RUNTIME" "$WORKDIR/logs"
chmod 700 "$WORKDIR/logs"
chmod 777 "$RUNTIME"  # the short-lived Podman root helper uses a subordinate UID

is_running() {
  local pid="${1:-}"
  [[ "$pid" =~ ^[0-9]+$ ]] && kill -0 "$pid" 2>/dev/null &&
    [[ -r "/proc/$pid/stat" ]] && [[ "$(ps -o stat= -p "$pid" | head -c 1)" != 'Z' ]]
}

is_our_bridge() {
  local pid="${1:-}"
  is_running "$pid" && [[ -r "/proc/$pid/cmdline" ]] &&
    tr '\0' '\n' < "/proc/$pid/cmdline" | grep -Fxq "$BRIDGE"
}

is_our_xray() {
  local pid="${1:-}"
  is_running "$pid" && [[ "$(readlink "/proc/$pid/exe" 2>/dev/null || true)" == "$XRAY" ]]
}

tun_ready() {
  podman exec "$CONTAINER" ip -o link show dev "$TUN" 2>/dev/null | grep -q 'LOWER_UP'
}

read_pid() {
  if [[ -f "$1" ]]; then cat "$1"; fi
}

bridge_pid="$(read_pid "$RUNTIME/bridge.pid")"
xray_pid="$(read_pid "$RUNTIME/xray.pid")"
previous_ns="$(cat "$RUNTIME/bridge.netns" 2>/dev/null || true)"
if is_our_bridge "$bridge_pid" && is_our_xray "$xray_pid" &&
   [[ "$previous_ns" == "$netns" ]] && tun_ready; then
  "$WORKDIR/mount.sh" apply
  echo "Dedicated Xray already running (PID $xray_pid, NetNS $netns)."
  exit 0
fi

# A prior Pod may have been restarted, leaving a gateway bound to its old
# namespace. Stop only the process identified by our own PID file and path.
if is_running "$bridge_pid"; then
  if ! is_our_bridge "$bridge_pid"; then
    echo 'PID file names an unknown bridge process; refusing to kill it.' >&2
    exit 1
  fi
  kill "$bridge_pid"
  for ((n=0; n<60; n++)); do
    is_running "$bridge_pid" || break
    sleep 0.1
  done
  if is_running "$bridge_pid"; then
    echo 'The old dedicated bridge did not stop; refusing to launch another.' >&2
    exit 1
  fi
fi
if is_running "$xray_pid"; then
  if ! is_our_xray "$xray_pid"; then
    echo 'PID file names an unknown Xray process; refusing to kill it.' >&2
    exit 1
  fi
  kill "$xray_pid"
  for ((n=0; n<60; n++)); do
    is_running "$xray_pid" || break
    sleep 0.1
  done
  if is_running "$xray_pid"; then
    echo 'The previous dedicated Xray did not stop; refusing to launch another.' >&2
    exit 1
  fi
fi
rm -f "$RUNTIME/bridge.pid" "$RUNTIME/xray.pid" "$RUNTIME/bridge.netns" "$RUNTIME/tunfd.sock"

cp "$BRIDGE" "$RUNTIME/helper.py"
chmod 644 "$RUNTIME/helper.py"

setsid python3 "$BRIDGE" serve \
  --socket "$RUNTIME/tunfd.sock" --name "$TUN" --expected-ns "$netns" \
  --namespace-pid "$infra_pid" --config "$CONFIG" --runtime "$RUNTIME" \
  --log "$WORKDIR/logs/xray.log" --xray "$XRAY" \
  > "$WORKDIR/logs/bridge.log" 2>&1 < /dev/null &

for ((n=0; n<40; n++)); do
  [[ -S "$RUNTIME/tunfd.sock" ]] && break
  sleep 0.1
done
if [[ ! -S "$RUNTIME/tunfd.sock" ]]; then
  echo 'TUN FD receiver did not start.' >&2
  tail -n 12 "$WORKDIR/logs/bridge.log" >&2
  exit 1
fi

# Only this short-lived helper opens the TUN. The development container has
# no NET_ADMIN, no TUN device bind mount, and no extra user directory mounts.
if ! podman run --rm --pod "$POD" --user 0 --cap-add NET_ADMIN \
    --device /dev/net/tun --volume "$RUNTIME:/handoff:rw" \
    --entrypoint python3 "$PYTHON_IMAGE" \
    /handoff/helper.py send --socket /handoff/tunfd.sock --name "$TUN"; then
  echo 'Failed to hand off TUN to dedicated Xray; namespace remains fail-closed.' >&2
  exit 1
fi

xray_pid="$(read_pid "$RUNTIME/xray.pid")"
if ! is_our_xray "$xray_pid"; then
  echo 'Dedicated Xray failed to stay running; namespace remains fail-closed.' >&2
  exit 1
fi
for ((n=0; n<20; n++)); do
  tun_ready && break
  sleep 0.1
done
if ! tun_ready; then
  echo 'TUN has no carrier after Xray startup; namespace remains fail-closed.' >&2
  exit 1
fi
"$WORKDIR/mount.sh" apply
printf 'Started %s with %s (Xray PID %s, NetNS %s).\n' "$CONTAINER" "$TUN" "$xray_pid" "$netns"
