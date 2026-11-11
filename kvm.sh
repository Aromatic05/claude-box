#!/usr/bin/env bash
set -euo pipefail

# Opt-in /dev/kvm access for the existing rootless development container.
# No container recreation, privilege escalation or other device forwarding.
WORKDIR="$(cd -- "$(dirname -- "$0")" && pwd)"
RUNTIME="$WORKDIR/runtime"
CONTAINER='claude-dev-isolated'
DEVICE='/dev/kvm'
FLAG="$RUNTIME/kvm.enabled"
STATE="$RUNTIME/kvm.state"

die() { printf 'kvm.sh: %s\n' "$*" >&2; exit 1; }

container_pid() {
  local pid
  [[ "$(podman inspect "$CONTAINER" --format '{{.State.Status}}' 2>/dev/null)" == 'running' ]] ||
    die "$CONTAINER is not running"
  pid="$(podman inspect "$CONTAINER" --format '{{.State.Pid}}')"
  [[ "$pid" =~ ^[1-9][0-9]*$ ]] && [[ -e "/proc/$pid/ns/mnt" ]] ||
    die "Cannot find the container mount namespace"
  printf '%s\n' "$pid"
}

host_device() {
  [[ -c "$DEVICE" ]] || return 1
  [[ "$(stat -Lc '%t:%T' "$DEVICE")" == 'a:e8' ]] ||
    die "Unexpected /dev/kvm device number"
  printf '%s\n' "$DEVICE"
}

mounted_at() {
  podman exec "$CONTAINER" findmnt -n -M "$DEVICE" -o TARGET >/dev/null 2>&1
}

target_identity() {
  podman exec "$CONTAINER" stat -Lc '%d:%i' "$DEVICE" 2>/dev/null
}

known_mount() {
  local ns="$1" saved_ns saved_inode
  [[ -f "$STATE" ]] || return 1
  read -r saved_ns saved_inode < "$STATE" || return 1
  [[ "$ns" == "$saved_ns" && "$(target_identity)" == "$saved_inode" ]]
}

prepare_target() {
  podman exec --user 0 "$CONTAINER" sh -ceu '
    if [ -L /dev/kvm ]; then
      echo "Refusing symlink at /dev/kvm" >&2
      exit 1
    fi
    if [ -e /dev/kvm ]; then
      [ -f /dev/kvm ] && [ "$(stat -Lc "%u:%g" /dev/kvm)" = "1000:1000" ] || {
        echo "Conflicting /dev/kvm node" >&2
        exit 1
      }
    else
      install -m 0600 -o 1000 -g 1000 /dev/null /dev/kvm
    fi
  '
}

attach_device() {
  local pid="$1"
  # open_tree() clones the host's char device in the rootless user namespace;
  # move_mount() installs it into the existing container mount namespace.
  podman unshare python3 - "$pid" "$DEVICE" <<'PY'
import ctypes
import os
import stat
import sys

pid, device = int(sys.argv[1]), sys.argv[2]
libc = ctypes.CDLL(None, use_errno=True)

def checked(result, operation):
    if result < 0:
        raise OSError(ctypes.get_errno(), operation)
    return result

source_fd = checked(libc.syscall(
    428, ctypes.c_int(-100), ctypes.c_char_p(os.fsencode(device)),
    ctypes.c_uint(1 | os.O_CLOEXEC)
), "open_tree")
if not stat.S_ISCHR(os.fstat(source_fd).st_mode):
    raise ValueError("Host /dev/kvm is not a character device")
target_fd = os.open(f"/proc/{pid}/root{device}", os.O_PATH | os.O_NOFOLLOW)
if not stat.S_ISREG(os.fstat(target_fd).st_mode):
    raise ValueError("Container /dev/kvm target is not a regular placeholder")
ns_fd = os.open(f"/proc/{pid}/ns/mnt", os.O_RDONLY)
checked(libc.setns(ctypes.c_int(ns_fd), ctypes.c_int(0x20000)), "setns")
checked(libc.syscall(
    429, ctypes.c_int(source_fd), ctypes.c_char_p(b""),
    ctypes.c_int(target_fd), ctypes.c_char_p(b""),
    ctypes.c_uint(4 | 0x40)
), "move_mount")
PY
}

detach_device() {
  local pid="$1"
  podman unshare nsenter --target "$pid" --user --mount --pid --root \
    --wdns=/ -- umount -- "$DEVICE"
}

remove_placeholder() {
  if podman exec "$CONTAINER" test -f "$DEVICE" &&
     [[ "$(podman exec "$CONTAINER" stat -Lc '%u:%g' "$DEVICE")" == '1000:1000' ]]; then
    podman exec --user 0 "$CONTAINER" rm -- "$DEVICE"
  fi
}

verify_device() {
  podman exec -i --user 1000:1000 "$CONTAINER" python3 - <<'PY'
import fcntl
import os

fd = os.open("/dev/kvm", os.O_RDWR | os.O_CLOEXEC)
try:
    version = fcntl.ioctl(fd, 0xAE00, 0)
    if version != 12:
        raise RuntimeError(f"Unexpected KVM API version: {version}")
    vm_fd = fcntl.ioctl(fd, 0xAE01, 0)
    os.close(vm_fd)
    print(f"UID {os.getuid()}: KVM API {version}, VM creation available")
finally:
    os.close(fd)
PY
}

attach() {
  local pid ns source_inode
  host_device >/dev/null || die 'Host /dev/kvm is unavailable'
  source_inode="$(stat -Lc '%d:%i' "$DEVICE")"
  pid="$(container_pid)"
  ns="$(readlink "/proc/$pid/ns/mnt")"

  if mounted_at; then
    known_mount "$ns" ||
      die "Existing /dev/kvm mount is not managed by kvm.sh"
    if [[ "$(target_identity)" == "$source_inode" ]]; then
      printf 'KVM already connected: %s (container UID 1000)\n' "$DEVICE"
      return 0
    fi
    detach_device "$pid"
  elif podman exec "$CONTAINER" test -e "$DEVICE"; then
    [[ -f "$STATE" ]] ||
      die "Existing /dev/kvm path is not managed by kvm.sh"
  fi

  prepare_target
  attach_device "$pid"
  if ! mounted_at || [[ "$(target_identity)" != "$source_inode" ]] ||
     [[ "$(podman exec "$CONTAINER" stat -Lc '%t:%T' "$DEVICE")" != 'a:e8' ]]; then
    detach_device "$pid" || true
    remove_placeholder
    die 'KVM device mount verification failed'
  fi
  if ! verify_device; then
    detach_device "$pid"
    remove_placeholder
    die 'KVM API is not accessible to UID 1000'
  fi
  printf '%s %s\n' "$ns" "$source_inode" > "$STATE"
  chmod 0600 "$STATE"
  echo 'KVM connected: /dev/kvm (UID 1000, API and VM creation verified).'
}

ready() {
  local pid ns source_inode
  [[ -f "$FLAG" && -f "$STATE" ]] || return 1
  host_device >/dev/null || return 1
  source_inode="$(stat -Lc '%d:%i' "$DEVICE")"
  pid="$(container_pid)" || return 1
  ns="$(readlink "/proc/$pid/ns/mnt")"
  mounted_at && known_mount "$ns" && [[ "$(target_identity)" == "$source_inode" ]]
}

disable() {
  local pid ns
  if [[ "$(podman inspect "$CONTAINER" --format '{{.State.Status}}' 2>/dev/null || true)" == 'running' ]]; then
    pid="$(container_pid)"
    ns="$(readlink "/proc/$pid/ns/mnt")"
    if mounted_at; then
      known_mount "$ns" ||
        die "Existing /dev/kvm mount is not managed by kvm.sh; refusing to unmount"
      detach_device "$pid"
    fi
    if [[ -f "$STATE" ]]; then
      remove_placeholder
    fi
  fi
  rm -f -- "$FLAG" "$STATE"
  echo 'KVM disconnected.'
}

(( $# == 1 )) || die 'Usage: ./kvm.sh enable | disable | status'
case "$1" in
  enable)
    mkdir -p -- "$RUNTIME"
    attach
    : > "$FLAG"
    chmod 0600 "$FLAG"
    ;;
  disable)
    disable
    ;;
  status)
    if [[ ! -f "$FLAG" ]]; then
      echo 'KVM integration is disabled.'
    elif ready; then
      echo 'KVM connected at /dev/kvm (UID 1000).'
    else
      echo 'KVM integration is enabled but not currently connected.'
    fi
    ;;
  restore)
    if [[ -f "$FLAG" ]]; then
      attach
    fi
    ;;
  ready)
    ready
    ;;
  *)
    die 'Usage: ./kvm.sh enable | disable | status'
    ;;
esac
