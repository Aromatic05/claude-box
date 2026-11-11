#!/usr/bin/env bash
set -euo pipefail

# Share only the active host Wayland socket with the running rootless container.
# Mount attachment is performed in the container's mount namespace; no
# host D-Bus, audio socket, GPU device or runtime directory is exposed.
WORKDIR="$(cd -- "$(dirname -- "$0")" && pwd)"
CONTAINER='claude-dev-isolated'
TARGET='/run/user/1000/wayland-claude'
RUNTIME="$WORKDIR/runtime"
FLAG="$RUNTIME/wayland.enabled"
STATE="$RUNTIME/wayland.state"
FISH_CONFIG="$WORKDIR/home/.config/fish/conf.d/claude-wayland.fish"

die() { printf 'gui.sh: %s\n' "$*" >&2; exit 1; }

container_pid() {
  local pid
  [[ "$(podman inspect "$CONTAINER" --format '{{.State.Status}}' 2>/dev/null)" == 'running' ]] ||
    die "$CONTAINER is not running"
  pid="$(podman inspect "$CONTAINER" --format '{{.State.Pid}}')"
  [[ "$pid" =~ ^[1-9][0-9]*$ ]] && [[ -e "/proc/$pid/ns/mnt" ]] ||
    die "Cannot resolve $CONTAINER mount namespace"
  printf '%s\n' "$pid"
}

host_socket() {
  local name candidate
  for name in "$(printenv WAYLAND_DISPLAY || true)" \
              "$(systemctl --user show-environment 2>/dev/null | sed -n 's/^WAYLAND_DISPLAY=//p' | tail -1)"; do
    [[ "$name" =~ ^wayland-[0-9]+$ ]] || continue
    candidate="/run/user/1000/$name"
    [[ -S "$candidate" ]] || continue
    [[ "$(stat -Lc '%u:%g' -- "$candidate")" == '1000:1000' ]] ||
      die "Wayland socket must be owned by host UID:GID 1000:1000"
    printf '%s\n' "$candidate"
    return 0
  done
  return 1
}

mounted_at() {
  podman exec "$CONTAINER" findmnt -n -M "$TARGET" -o TARGET >/dev/null 2>&1
}

target_identity() {
  podman exec "$CONTAINER" stat -Lc '%d:%i' "$TARGET" 2>/dev/null
}

known_mount() {
  local pid="$1" ns="$2" saved_ns saved_inode
  [[ -f "$STATE" ]] || return 1
  read -r saved_ns saved_inode < "$STATE" || return 1
  [[ "$saved_ns" == "$ns" && "$saved_inode" == "$(target_identity)" ]]
}

prepare_target() {
  podman exec --user 0 "$CONTAINER" sh -ceu '
    if [ -L /run/user/1000 ]; then
      echo "Refusing symlink at /run/user/1000" >&2
      exit 1
    fi
    if [ ! -e /run/user/1000 ]; then
      mkdir -- /run/user/1000
      chown 1000:1000 -- /run/user/1000
      chmod 0700 -- /run/user/1000
    fi
    [ -d /run/user/1000 ] &&
    [ "$(stat -Lc "%u:%g" /run/user/1000)" = "1000:1000" ] || {
      echo "Incorrect /run/user/1000 ownership" >&2
      exit 1
    }
    if [ -L "$1" ]; then
      echo "Refusing symlink at Wayland target" >&2
      exit 1
    fi
    if [ -e "$1" ]; then
      [ -f "$1" ] && [ "$(stat -Lc "%u:%g" "$1")" = "1000:1000" ] || {
        echo "Wayland target conflicts with existing file" >&2
        exit 1
      }
    else
      install -m 0600 -o 1000 -g 1000 /dev/null "$1"
    fi
  ' sh "$TARGET"
}

attach_socket() {
  local pid="$1" source="$2"
  # An out-of-namespace path cannot be passed to mount --bind directly.
  # Clone the socket mount in the rootless host view, then move it into the
  # existing container mount namespace using the already verified Linux API.
  podman unshare python3 - "$pid" "$source" "$TARGET" <<'PY'
import ctypes
import os
import stat
import sys

pid, source, target = int(sys.argv[1]), sys.argv[2], sys.argv[3]
libc = ctypes.CDLL(None, use_errno=True)

def checked(result, operation):
    if result < 0:
        raise OSError(ctypes.get_errno(), operation)
    return result

source_fd = checked(libc.syscall(
    428, ctypes.c_int(-100), ctypes.c_char_p(os.fsencode(source)),
    ctypes.c_uint(1 | os.O_CLOEXEC)
), "open_tree")
if not stat.S_ISSOCK(os.fstat(source_fd).st_mode):
    raise ValueError("Host Wayland source is not a socket")
target_fd = os.open(f"/proc/{pid}/root{target}",
                    os.O_PATH | os.O_NOFOLLOW)
if not stat.S_ISREG(os.fstat(target_fd).st_mode):
    raise ValueError("Container target is not a regular placeholder")
ns_fd = os.open(f"/proc/{pid}/ns/mnt", os.O_RDONLY)
checked(libc.setns(ctypes.c_int(ns_fd), ctypes.c_int(0x20000)), "setns")
checked(libc.syscall(
    429, ctypes.c_int(source_fd), ctypes.c_char_p(b""),
    ctypes.c_int(target_fd), ctypes.c_char_p(b""),
    ctypes.c_uint(4 | 0x40)
), "move_mount")
PY
}

detach_socket() {
  local pid="$1"
  podman unshare nsenter --target "$pid" --user --mount --pid --root \
    --wdns=/ -- umount -- "$TARGET"
}

apply_socket() {
  local pid ns source inode
  source="$(host_socket)" || die 'No active Wayland socket found for UID 1000'
  inode="$(stat -Lc '%d:%i' -- "$source")"
  pid="$(container_pid)"
  ns="$(readlink "/proc/$pid/ns/mnt")"

  if mounted_at; then
    known_mount "$pid" "$ns" ||
      die "Unknown mount at $TARGET; refusing to replace it"
    if [[ "$(target_identity)" == "$inode" ]]; then
      printf 'Wayland socket already connected: %s <- %s\n' "$TARGET" "$source"
      return 0
    fi
    # Compositor restarted: detach only the mount recorded by this script.
    detach_socket "$pid"
  elif podman exec "$CONTAINER" test -e "$TARGET"; then
    [[ -f "$STATE" ]] ||
      die "Existing container path $TARGET is not managed by gui.sh"
  fi

  prepare_target
  attach_socket "$pid" "$source"
  if ! mounted_at || [[ "$(target_identity)" != "$inode" ]] ||
     [[ "$(podman exec "$CONTAINER" stat -Lc '%u:%g' "$TARGET")" != '1000:1000' ]]; then
    die "Wayland socket mount/UID verification failed"
  fi
  printf '%s %s\n' "$ns" "$inode" > "$STATE"
  chmod 0600 "$STATE"
  printf 'Wayland socket connected: %s <- %s (UID 1000)\n' "$TARGET" "$source"
}

ready() {
  local pid ns source inode
  [[ -f "$FLAG" && -f "$STATE" ]] || return 1
  source="$(host_socket)" || return 1
  inode="$(stat -Lc '%d:%i' -- "$source")"
  pid="$(container_pid)" || return 1
  ns="$(readlink "/proc/$pid/ns/mnt")"
  mounted_at && known_mount "$pid" "$ns" && [[ "$(target_identity)" == "$inode" ]]
}

install_fish_env() {
  if [[ -e "$FISH_CONFIG" ]]; then
    grep -Fxq '# Managed by claude-netns/gui.sh' "$FISH_CONFIG" ||
      die "Existing Fish config is not managed by gui.sh: $FISH_CONFIG"
    return 0
  fi
  mkdir -p -- "$(dirname -- "$FISH_CONFIG")"
  cat > "$FISH_CONFIG" <<'FISH'
# Managed by claude-netns/gui.sh
if test -S /run/user/1000/wayland-claude
    set -gx XDG_RUNTIME_DIR /run/user/1000
    set -gx WAYLAND_DISPLAY wayland-claude
end
FISH
  chmod 0644 "$FISH_CONFIG"
}

remove_fish_env() {
  if [[ -f "$FISH_CONFIG" ]] &&
     grep -Fxq '# Managed by claude-netns/gui.sh' "$FISH_CONFIG"; then
    rm -- "$FISH_CONFIG"
  fi
}

disable_socket() {
  local pid ns
  if [[ "$(podman inspect "$CONTAINER" --format '{{.State.Status}}' 2>/dev/null || true)" == 'running' ]]; then
    pid="$(container_pid)"
    ns="$(readlink "/proc/$pid/ns/mnt")"
    if mounted_at; then
      known_mount "$pid" "$ns" ||
        die "Unknown mount at $TARGET; refusing to unmount it"
      detach_socket "$pid"
    fi
    if [[ -f "$STATE" ]] && podman exec "$CONTAINER" test -f "$TARGET"; then
      podman exec --user 1000:1000 "$CONTAINER" rm -- "$TARGET"
    fi
  fi
  rm -f -- "$FLAG" "$STATE"
  remove_fish_env
  echo 'Wayland socket disconnected.'
}

(( $# == 1 )) || die 'Usage: ./gui.sh enable | disable | status'
case "$1" in
  enable)
    mkdir -p -- "$RUNTIME"
    apply_socket
    install_fish_env
    : > "$FLAG"
    chmod 0600 "$FLAG"
    ;;
  disable)
    disable_socket
    ;;
  status)
    if [[ ! -f "$FLAG" ]]; then
      echo 'Wayland socket integration is disabled.'
    elif ready; then
      echo "Wayland socket connected at $TARGET (UID 1000)."
    else
      echo 'Wayland socket enabled but not currently connected.'
    fi
    ;;
  restore)
    if [[ -f "$FLAG" ]]; then
      if host_socket >/dev/null; then
        apply_socket
        install_fish_env
      else
        echo 'Wayland session unavailable; leaving GUI integration disconnected.' >&2
      fi
    fi
    ;;
  ready)
    ready
    ;;
  *)
    die 'Usage: ./gui.sh enable | disable | status'
    ;;
esac
