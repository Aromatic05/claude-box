#!/usr/bin/env bash
set -euo pipefail

# When managed by the user systemd service, stop its main listener first.
# ExecStop sets this variable to avoid calling systemctl recursively.
if [[ "${CLAUDE_NETNS_FROM_SYSTEMD:-}" != '1' ]] &&
   systemctl --user is-active --quiet claude-netns.service; then
  exec systemctl --user stop claude-netns.service
fi

WORKDIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
RUNTIME="$WORKDIR/runtime"
POD='claude-private-netns'
BRIDGE="$WORKDIR/tun-bridge.py"
XRAY="$WORKDIR/xray"

read_pid() {
  local file="$1"
  if [[ -f "$file" ]]; then cat "$file"; fi
}

is_running() {
  local pid="${1:-}"
  [[ "$pid" =~ ^[0-9]+$ ]] && kill -0 "$pid" 2>/dev/null &&
    [[ "$(ps -o stat= -p "$pid" 2>/dev/null | head -c 1)" != 'Z' ]]
}

is_our_bridge() {
  local pid="${1:-}"
  is_running "$pid" && [[ -r "/proc/$pid/cmdline" ]] &&
    tr '\0' '\n' < "/proc/$pid/cmdline" | grep -Fxq "$BRIDGE"
}

is_our_xray() {
  local pid="${1:-}"
  is_running "$pid" &&
    [[ "$(readlink "/proc/$pid/exe" 2>/dev/null || true)" == "$XRAY" ]]
}

wait_stopped() {
  local pid="$1"
  for ((n=0; n<60; n++)); do
    is_running "$pid" || return 0
    sleep 0.1
  done
  ! is_running "$pid"
}

bridge_pid="$(read_pid "$RUNTIME/bridge.pid")"
xray_pid="$(read_pid "$RUNTIME/xray.pid")"

# PID files alone are not sufficient evidence of ownership.
if is_running "$bridge_pid" && ! is_our_bridge "$bridge_pid"; then
  echo "Refusing to signal unknown bridge process: $bridge_pid" >&2
  exit 1
fi
if is_running "$xray_pid" && ! is_our_xray "$xray_pid"; then
  echo "Refusing to signal unknown Xray process: $xray_pid" >&2
  exit 1
fi

# The bridge owns the dedicated Xray child and normally shuts it down.
if is_our_bridge "$bridge_pid"; then
  kill -TERM "$bridge_pid"
  if ! wait_stopped "$bridge_pid"; then
    echo "Dedicated bridge did not stop: $bridge_pid" >&2
    exit 1
  fi
fi

# Handle a remaining gateway without affecting the system Xray service.
if is_running "$xray_pid"; then
  if ! is_our_xray "$xray_pid"; then
    echo "Refusing to signal unknown Xray process: $xray_pid" >&2
    exit 1
  fi
  kill -TERM "$xray_pid"
  if ! wait_stopped "$xray_pid"; then
    echo "Dedicated Xray did not stop: $xray_pid" >&2
    exit 1
  fi
fi

# Stop only the named isolated Pod. The namespace and TUN go away with it.
if podman pod exists "$POD"; then
  if [[ "$(podman pod inspect "$POD" --format '{{.State}}')" == 'Running' ]]; then
    podman pod stop -t 2 "$POD"
  fi
fi

# Only stale state files belonging to this workspace are removed.
rm -f "$RUNTIME/bridge.pid" "$RUNTIME/xray.pid" \
      "$RUNTIME/bridge.netns" "$RUNTIME/tunfd.sock"
printf 'Stopped dedicated Xray bridge and %s.\n' "$POD"
