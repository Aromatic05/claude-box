"""Small Python operations executed inside the existing container via podman exec -i.

The source is passed over stdin; this file is not bind-mounted into the container.
"""
import fcntl
import os
import pathlib
import stat
import sys

UID = GID = 1000


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


def prepare_socket(target):
    runtime = pathlib.Path("/run/user/1000")
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


def verify_kvm():
    fd = os.open("/dev/kvm", os.O_RDWR | os.O_CLOEXEC)
    try:
        version = fcntl.ioctl(fd, 0xAE00, 0)
        if version != 12:
            raise RuntimeError(f"Unexpected KVM API version: {version}")
        vm = fcntl.ioctl(fd, 0xAE01, 0)
        os.close(vm)
        print(f"UID {os.getuid()}: KVM API {version}, VM creation available")
    finally:
        os.close(fd)


def ensure_resolver():
    path = pathlib.Path("/etc/resolv.conf")
    if not path.exists():
        path.write_text("nameserver 1.1.1.1\noptions timeout:2 attempts:2\n")
        path.chmod(0o644)


if __name__ == "__main__":
    cmd, *args = sys.argv[1:]
    functions = {
        "prepare-dir": prepare_dir,
        "prepare-socket": prepare_socket,
        "prepare-kvm": ensure_file,
        "remove-placeholder": remove_placeholder,
        "verify-kvm": verify_kvm,
        "ensure-resolver": ensure_resolver,
    }
    if cmd not in functions:
        raise RuntimeError(f"Unsupported container operation: {cmd}")
    functions[cmd](*args)
