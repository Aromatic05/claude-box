"""Reproducible pinned-Xray build without touching the running binary."""
from __future__ import annotations

import os
import re
import subprocess
import tempfile

from .common import ROOT, ControlError, command, output

SOURCE = ROOT / "xray-core"
PATCH = ROOT / "patches/0001-xray-tun-external-ifindex.patch"
OUTPUT = ROOT / "build"


def build_xray() -> None:
    stage = output("git", "-C", str(ROOT), "ls-files", "--stage", "--", "xray-core")
    entry = next((line.split()[1] for line in stage.splitlines()
                  if line.split() and line.split()[0] == "160000"), None)
    actual = output("git", "-C", str(SOURCE), "rev-parse", "HEAD")
    if not entry or actual != entry:
        raise ControlError("Xray submodule HEAD differs from the pinned gitlink; refusing to build.")
    if not PATCH.is_file() or PATCH.stat().st_size == 0:
        raise ControlError(f"Missing Xray overlay patch: {PATCH}")

    OUTPUT.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="xray-source.", dir=OUTPUT) as temp:
        archive = subprocess.Popen(
            ["git", "-C", str(SOURCE), "archive", "HEAD"],
            stdout=subprocess.PIPE, cwd=ROOT,
        )
        assert archive.stdout is not None
        try:
            extracted = subprocess.run(
                ["tar", "-x", "-C", temp], stdin=archive.stdout, cwd=ROOT, check=False,
            )
        finally:
            archive.stdout.close()
        archive_code = archive.wait()
        if archive_code or extracted.returncode:
            raise ControlError("Failed to extract pinned Xray source archive")

        overlay = PATCH.read_bytes()
        for dry in (True, False):
            args = ["patch", "--batch", "--directory", temp, "-p1"]
            if dry:
                args.insert(2, "--dry-run")
            applied = subprocess.run(args, input=overlay, cwd=ROOT, check=False)
            if applied.returncode:
                raise ControlError("Failed to apply local Xray patch")

        env = os.environ.copy()
        env["CGO_ENABLED"] = "0"
        command(
            "go", "build", "-trimpath", "-buildvcs=false",
            "-ldflags=-s -w -buildid=", "-o", str(OUTPUT / "xray"), "./main",
            cwd=__import__("pathlib").Path(temp), env=env, capture=False,
        )
    print(f"Built patched Xray at {OUTPUT / 'xray'} (pinned upstream: {entry}).")
    print("The running ./xray binary has not been modified.")
