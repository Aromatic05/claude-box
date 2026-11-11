#!/usr/bin/env bash
set -euo pipefail

# Reproduce the local Xray build from the pinned upstream submodule
# plus the isolated patch, without modifying the submodule or live binary.
WORKDIR="$(cd -- "$(dirname -- "$0")" && pwd)"
SOURCE="$WORKDIR/xray-core"
PATCH="$WORKDIR/patches/0001-xray-tun-external-ifindex.patch"
OUTPUT="$WORKDIR/build"
PINNED="$(git -C "$WORKDIR" ls-files --stage -- xray-core | awk '$1 == "160000" { print $2 }')"
ACTUAL="$(git -C "$SOURCE" rev-parse HEAD)"

[[ -n "$PINNED" && "$ACTUAL" == "$PINNED" ]] || {
  echo "Xray submodule HEAD differs from the pinned gitlink; refusing to build." >&2
  exit 1
}
[[ -s "$PATCH" ]] || {
  echo "Missing Xray overlay patch: $PATCH" >&2
  exit 1
}

mkdir -p "$OUTPUT"
BUILD_SOURCE="$(mktemp -d "$OUTPUT/xray-source.XXXXXXXX")"
trap 'rm -rf -- "$BUILD_SOURCE"' EXIT

git -C "$SOURCE" archive HEAD | tar -x -C "$BUILD_SOURCE"
patch --batch --dry-run --directory "$BUILD_SOURCE" -p1 < "$PATCH"
patch --batch --directory "$BUILD_SOURCE" -p1 < "$PATCH"

(
  cd -- "$BUILD_SOURCE"
  CGO_ENABLED=0 go build -trimpath -buildvcs=false \
    -ldflags='-s -w -buildid=' -o "$OUTPUT/xray" ./main
)
echo "Built patched Xray at $OUTPUT/xray (pinned upstream: $PINNED)."
echo "The running ./xray binary has not been modified."
