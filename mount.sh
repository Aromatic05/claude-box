#!/usr/bin/env bash
set -euo pipefail

# Mount a host directory in the running container without recreating it.
WORKDIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
CONFIG="$WORKDIR/mounts.conf"
CONTAINER='claude-dev-isolated'
OWNER='1000:1000'

die() {
  printf 'mount.sh: %s\n' "$*" >&2
  exit 1
}

container_pid() {
  local pid
  [[ "$(podman inspect "$CONTAINER" --format '{{.State.Status}}')" == 'running' ]] ||
    die "$CONTAINER is not running"
  pid="$(podman inspect "$CONTAINER" --format '{{.State.Pid}}')"
  [[ "$pid" =~ ^[1-9][0-9]*$ && -e "/proc/$pid/ns/mnt" ]] ||
    die "No running mount namespace for $CONTAINER"
  printf '%s\n' "$pid"
}

validate_destination() {
  local dest="$1"
  [[ "$dest" == /* && "$dest" != '/' && "$dest" != */ ]] ||
    die "Invalid container destination: $dest"
  case "/${dest#/}/" in
    *'/../'*|*'/./'*|*'//'*) die "Non-normalized container destination: $dest" ;;
  esac
}

declare -a TARGETS=() SOURCES=()
read_config() {
  local line target source resolved existing trimmed
  local line_no=0
  [[ -f "$CONFIG" ]] || die "Missing configuration: $CONFIG"
  while IFS= read -r line || [[ -n "$line" ]]; do
    ((line_no+=1))
    line="${line%$'\r'}"
    trimmed="${line#"${line%%[![:space:]]*}"}"
    [[ -z "$trimmed" || "$trimmed" == \#* ]] && continue
    [[ "$line" == *:* ]] || die "$CONFIG:$line_no: expected container-path:host-path"
    target="${line%%:*}"
    source="${line#*:}"
    validate_destination "$target"
    [[ "$source" == /* ]] || die "$CONFIG:$line_no: host source must be absolute"
    [[ -d "$source" ]] || die "$CONFIG:$line_no: host source is not an existing directory: $source"
    resolved="$(realpath -e -- "$source")"
    [[ "$(stat -Lc '%u:%g' -- "$resolved")" == "$OWNER" ]] ||
      die "$CONFIG:$line_no: source $resolved must belong to UID:GID $OWNER; refusing to change ownership"
    for existing in "${TARGETS[@]}"; do
      [[ "$existing" != "$target" ]] || die "$CONFIG:$line_no: duplicate destination: $target"
    done
    TARGETS+=("$target")
    SOURCES+=("$resolved")
  done < "$CONFIG"
}

mounted_at() {
  podman exec "$CONTAINER" findmnt -n -M "$1" -o TARGET >/dev/null 2>&1
}

same_source() {
  local target="$1" source="$2" host_identity container_identity
  host_identity="$(stat -Lc '%d:%i' -- "$source")"
  container_identity="$(podman exec "$CONTAINER" stat -Lc '%d:%i' -- "$target" 2>/dev/null)" || return 1
  [[ "$host_identity" == "$container_identity" ]] &&
    [[ "$(podman exec "$CONTAINER" stat -Lc '%u:%g' -- "$target")" == "$OWNER" ]]
}

prepare_destination() {
  # Only new components are chowned; no existing container/host path is chowned.
  podman exec --user 0 "$CONTAINER" sh -ceu '
    dest=$1
    rest=${dest#/}
    current=
    while [ -n "$rest" ]; do
      component=${rest%%/*}
      if [ "$component" = "$rest" ]; then rest=; else rest=${rest#*/}; fi
      current="$current/$component"
      if [ -L "$current" ]; then
        echo "Refusing symlink in destination: $current" >&2
        exit 1
      fi
      if [ ! -e "$current" ]; then
        mkdir -- "$current"
        chown 1000:1000 -- "$current"
      fi
      [ -d "$current" ] || { echo "Not a directory: $current" >&2; exit 1; }
    done
    [ "$(stat -Lc "%u:%g" -- "$dest")" = "1000:1000" ] || {
      echo "Destination must belong to UID:GID 1000:1000: $dest" >&2
      exit 1
    }
    [ -z "$(ls -A -- "$dest")" ] || {
      echo "Destination is not empty: $dest" >&2
      exit 1
    }
  ' sh "$1"
}

bind_into_namespace() {
  local pid="$1" source="$2" target="$3"
  # Open a detached clone while the source is visible in the rootless host
  # mount namespace. Then attach it in the container's mount namespace.
  # A direct bind using an out-of-namespace cwd fails with EINVAL.
  podman unshare python3 - "$pid" "$source" "$target" <<'PY'
import ctypes
import os
import sys

pid, source, target = int(sys.argv[1]), sys.argv[2], sys.argv[3]
libc = ctypes.CDLL(None, use_errno=True)
# Linux open_tree(2), move_mount(2); 64-bit syscall numbers.
AT_FDCWD = -100
OPEN_TREE_CLONE = 1
MOVE_MOUNT_F_EMPTY_PATH = 4
MOVE_MOUNT_T_EMPTY_PATH = 0x40
CLONE_NEWNS = 0x20000

def checked(result, operation):
    if result < 0:
        raise OSError(ctypes.get_errno(), operation)
    return result

source_fd = checked(libc.syscall(
    428, ctypes.c_int(AT_FDCWD), ctypes.c_char_p(os.fsencode(source)),
    ctypes.c_uint(OPEN_TREE_CLONE | os.O_CLOEXEC)
), "open_tree")
target_fd = os.open(f"/proc/{pid}/root{target}",
                    os.O_PATH | os.O_DIRECTORY | os.O_NOFOLLOW)
ns_fd = os.open(f"/proc/{pid}/ns/mnt", os.O_RDONLY)
checked(libc.setns(ctypes.c_int(ns_fd), ctypes.c_int(CLONE_NEWNS)), "setns")
checked(libc.syscall(
    429, ctypes.c_int(source_fd), ctypes.c_char_p(b""),
    ctypes.c_int(target_fd), ctypes.c_char_p(b""),
    ctypes.c_uint(MOVE_MOUNT_F_EMPTY_PATH | MOVE_MOUNT_T_EMPTY_PATH)
), "move_mount")
PY
}

apply_one() {
  local pid="$1" target="$2" source="$3"
  if mounted_at "$target"; then
    same_source "$target" "$source" ||
      die "Existing mount at $target has a different source; refusing to replace it"
    printf 'already mounted: %s <- %s\n' "$target" "$source"
    return
  fi
  prepare_destination "$target"
  bind_into_namespace "$pid" "$source" "$target"
  mounted_at "$target" && same_source "$target" "$source" ||
    die "Mount verification failed: $target <- $source"
  printf 'mounted: %s <- %s (container UID:GID %s)\n' "$target" "$source" "$OWNER"
}

remove_one() {
  local pid="$1" target="$2" source="$3"
  if ! mounted_at "$target"; then
    printf 'not mounted: %s\n' "$target"
    return
  fi
  same_source "$target" "$source" ||
    die "Mount at $target is not the configured source; refusing to unmount"
  podman unshare nsenter --target "$pid" --user --mount --pid --root \
    --wdns=/ -- umount -- "$target"
  printf 'unmounted: %s\n' "$target"
}

usage() {
  echo 'Usage: ./mount.sh apply | list | remove <container-path> | remove-all' >&2
  exit 2
}

(( $# >= 1 )) || usage
action="$1"
case "$action" in
  apply|list|remove-all) (( $# == 1 )) || usage ;;
  remove) (( $# == 2 )) || usage; validate_destination "$2" ;;
  *) usage ;;
esac
read_config
if (( ${#TARGETS[@]} == 0 )); then
  [[ "$action" == 'remove' ]] && die "No configured mount for $2"
  [[ "$action" == 'list' ]] && echo 'No dynamic mounts configured.'
  exit 0
fi
pid="$(container_pid)"
case "$action" in
  apply)
    for i in "${!TARGETS[@]}"; do apply_one "$pid" "${TARGETS[i]}" "${SOURCES[i]}"; done
    ;;
  list)
    for i in "${!TARGETS[@]}"; do
      if mounted_at "${TARGETS[i]}"; then
        if same_source "${TARGETS[i]}" "${SOURCES[i]}"; then state=mounted
        else state=conflict
        fi
      else
        state=unmounted
      fi
      printf '%s: %s <- %s\n' "$state" "${TARGETS[i]}" "${SOURCES[i]}"
    done
    ;;
  remove)
    found=0
    for i in "${!TARGETS[@]}"; do
      if [[ "${TARGETS[i]}" == "$2" ]]; then
        remove_one "$pid" "${TARGETS[i]}" "${SOURCES[i]}"
        found=1
        break
      fi
    done
    (( found )) || die "No configured mount for $2"
    ;;
  remove-all)
    for ((i=${#TARGETS[@]}-1; i>=0; i--)); do
      remove_one "$pid" "${TARGETS[i]}" "${SOURCES[i]}"
    done
    ;;
esac
