"""Small Python operations executed inside the existing container via podman exec -i.

The source is passed over stdin; this file is not bind-mounted into the container.
"""
import fcntl
import os
import pathlib
import re
import signal
import stat
import sys

UID = GID = 0


def ensure_file(target):
    path = pathlib.Path(target)
    if path.is_symlink():
        raise RuntimeError(f"Refusing symlink at {path}")
    if path.exists():
        if not path.is_file() or (path.stat().st_uid, path.stat().st_gid) != (UID, GID):
            raise RuntimeError(f"Conflicting file at {path}")
    else:
        fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
        os.close(fd)
        os.chown(path, UID, GID)


def prepare_dir(target):
    current = pathlib.Path("/")
    for part in pathlib.PurePosixPath(target).parts[1:]:
        current = current / part
        if current.is_symlink():
            raise RuntimeError(f"Refusing symlink in destination: {current}")
        if not current.exists():
            current.mkdir()
            os.chown(current, UID, GID)
        if not current.is_dir():
            raise RuntimeError(f"Not a directory: {current}")
    st = current.stat()
    if (st.st_uid, st.st_gid) != (UID, GID):
        raise RuntimeError(f"Destination must belong to UID:GID 1000:1000: {current}")
    if any(current.iterdir()):
        raise RuntimeError(f"Destination is not empty: {current}")


def prepare_socket(target, runtime_path):
    runtime = pathlib.Path(runtime_path)
    if runtime.is_symlink():
        raise RuntimeError(f"Refusing symlink at {runtime}")
    if not runtime.exists():
        runtime.mkdir()
        os.chown(runtime, UID, GID)
        runtime.chmod(0o700)
    if not runtime.is_dir() or (runtime.stat().st_uid, runtime.stat().st_gid) != (UID, GID):
        raise RuntimeError(f"Incorrect {runtime} ownership")
    ensure_file(target)


def remove_placeholder(target):
    path = pathlib.Path(target)
    if path.is_symlink():
        return
    if path.exists() and path.is_file():
        st = path.stat()
        if (st.st_uid, st.st_gid) == (UID, GID):
            path.unlink()


def verify_kvm(device):
    fd = os.open(device, os.O_RDWR | os.O_CLOEXEC)
    try:
        version = fcntl.ioctl(fd, 0xAE00, 0)
        if version != 12:
            raise RuntimeError(f"Unexpected KVM API version: {version}")
        vm = fcntl.ioctl(fd, 0xAE01, 0)
        os.close(vm)
        print(f"UID {os.getuid()}: KVM API {version}, VM creation available")
    finally:
        os.close(fd)


def ensure_resolver(*dns):
    path = pathlib.Path("/etc/resolv.conf")
    if path.is_symlink():
        raise RuntimeError("Refusing to replace a symlinked /etc/resolv.conf")
    content = "".join(f"nameserver {address}\n" for address in dns) + "options timeout:2 attempts:2\n"
    if not path.exists() or path.read_text() != content:
        path.write_text(content)
        path.chmod(0o644)


SSHD_CONFIG = pathlib.Path("/etc/ssh/sshd_config_claude")
SSHD_PID = pathlib.Path("/run/sshd-claude.pid")


def configure_sshd(port, address):
    text = SSHD_CONFIG.read_text()
    for directive, value in (("Port", port), ("ListenAddress", address)):
        pattern = rf"(?m)^{directive} .*$"
        if len(re.findall(pattern, text)) != 1:
            raise RuntimeError(f"Expected one {directive} directive in dedicated sshd config")
        text = re.sub(pattern, f"{directive} {value}", text, count=1)
    if text == SSHD_CONFIG.read_text():
        print("unchanged")
        return
    SSHD_CONFIG.write_text(text)
    print("changed")


def reload_sshd():
    if not SSHD_PID.is_file():
        return
    pid = int(SSHD_PID.read_text().strip())
    process = pathlib.Path(f"/proc/{pid}/cmdline")
    if not process.is_file():
        return
    cmdline = process.read_bytes()
    executable = pathlib.Path(f"/proc/{pid}/exe").resolve()
    if (
        executable.name != "sshd"
        or str(SSHD_CONFIG).encode() not in cmdline
    ):
        raise RuntimeError("Refusing to reload unrelated SSH process")
    os.kill(pid, signal.SIGHUP)
    print("reloaded")


if __name__ == "__main__":
    UID, GID = int(sys.argv[1]), int(sys.argv[2])
    cmd, *args = sys.argv[3:]
    functions = {
        "prepare-dir": prepare_dir,
        "prepare-socket": prepare_socket,
        "prepare-kvm": ensure_file,
        "remove-placeholder": remove_placeholder,
        "verify-kvm": verify_kvm,
        "ensure-resolver": ensure_resolver,
        "configure-sshd": configure_sshd,
        "reload-sshd": reload_sshd,
    }
    if cmd not in functions:
        raise RuntimeError(f"Unsupported container operation: {cmd}")
    functions[cmd](*args)
