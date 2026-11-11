#!/usr/bin/env python3
"""Transfer a Podman-NetNS TUN FD to a host-NetNS Xray process via SCM_RIGHTS."""
import argparse
import array
import fcntl
import json
import os
import signal
import socket
import struct
import subprocess
import sys
import time
from pathlib import Path

TUNSETIFF = 0x400454ca
TUNGETIFF = 0x800454d2
IFF_TUN = 0x0001
IFF_NO_PI = 0x1000
IFREQ = '16sH'


def netns():
    return os.readlink('/proc/self/ns/net')


def check_tun(fd, name):
    info = fcntl.ioctl(fd, TUNGETIFF, bytes(40))
    actual = info[:16].split(b'\0', 1)[0].decode()
    if actual != name:
        raise RuntimeError('Unexpected TUN interface: ' + actual)


def send(ns_socket, name):
    fd = os.open('/dev/net/tun', os.O_RDWR | os.O_NONBLOCK)
    try:
        fcntl.ioctl(fd, TUNSETIFF, struct.pack(IFREQ, name.encode(), IFF_TUN | IFF_NO_PI) + bytes(22))
        check_tun(fd, name)
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as client:
            client.settimeout(15)
            client.connect(ns_socket)
            rights = array.array('i', [fd])
            client.sendmsg([json.dumps({'netns': netns(), 'index': socket.if_nametoindex(name)}).encode()],
                           [(socket.SOL_SOCKET, socket.SCM_RIGHTS, rights)])
            reply = client.recv(128)
            if reply != b'OK':
                raise RuntimeError('FD receiver rejected interface: ' + reply.decode(errors='replace'))
    finally:
        os.close(fd)


def serve(ns_socket, expected_ns, name, config, runtime, logfile, xray, namespace_pid):
    runtime = Path(runtime)
    sock_path = Path(ns_socket)
    pid_path = runtime / 'bridge.pid'
    xray_pid_path = runtime / 'xray.pid'
    ns_path = runtime / 'bridge.netns'
    proc = None
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as server:
        if sock_path.exists():
            sock_path.unlink()
        server.bind(ns_socket)
        os.chmod(ns_socket, 0o777)  # Podman root is mapped to a subordinate host UID.
        server.listen(1)
        server.settimeout(25)
        pid_path.write_text(str(os.getpid()))
        ns_path.write_text(expected_ns)
        try:
            conn, _ = server.accept()
            with conn:
                conn.settimeout(8)
                msg, ancillary, _, _ = conn.recvmsg(128, socket.CMSG_SPACE(4))
                fds = array.array('i')
                for level, kind, data in ancillary:
                    if level == socket.SOL_SOCKET and kind == socket.SCM_RIGHTS:
                        fds.frombytes(data[:4])
                if len(fds) != 1:
                    conn.sendall(b'FAIL:missing FD')
                    raise RuntimeError('Expected exactly one TUN fd')
                fd = fds[0]
                try:
                    identity = json.loads(msg)
                    if identity['netns'] != expected_ns:
                        conn.sendall(b'FAIL:netns mismatch')
                        raise RuntimeError('Sender is in unexpected NetNS')
                    index = identity['index']
                    if type(index) is not int or index <= 0:
                        conn.sendall(b'FAIL:invalid link index')
                        raise RuntimeError('Invalid TUN index')
                    check_tun(fd, name)
                    namespace_path = f'/proc/{namespace_pid}/ns/net'
                    if os.readlink(namespace_path) != expected_ns:
                        conn.sendall(b'FAIL:namespace PID changed')
                        raise RuntimeError('Pod namespace identity changed')
                    env = os.environ.copy()
                    env['XRAY_TUN_FD'] = str(fd)
                    env['XRAY_TUN_IFINDEX'] = str(index)
                    with open(logfile, 'a', buffering=1) as log:
                        proc = subprocess.Popen([xray, 'run', '-c', config],
                                                env=env, pass_fds=(fd,),
                                                stdout=log, stderr=subprocess.STDOUT,
                                                start_new_session=True)
                    xray_pid_path.write_text(str(proc.pid))
                    time.sleep(0.5)
                    if proc.poll() is not None:
                        conn.sendall(b'FAIL:Xray startup failed')
                        raise RuntimeError(f'Xray exited with code {proc.returncode}')
                    conn.sendall(b'OK')
                finally:
                    os.close(fd)
            if sock_path.exists():
                sock_path.unlink()
            def terminate(_sig, _frame):
                if proc and proc.poll() is None:
                    proc.terminate()
            signal.signal(signal.SIGTERM, terminate)
            signal.signal(signal.SIGINT, terminate)
            sys.exit(proc.wait())
        finally:
            if proc and proc.poll() is None:
                proc.terminate()
                try:
                    proc.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    proc.kill()
                    proc.wait()
            for path in (sock_path, pid_path, xray_pid_path, ns_path):
                try:
                    path.unlink()
                except FileNotFoundError:
                    pass


if __name__ == '__main__':
    ap = argparse.ArgumentParser()
    ap.add_argument('operation', choices=('send', 'serve'))
    ap.add_argument('--socket', required=True)
    ap.add_argument('--name', required=True)
    ap.add_argument('--expected-ns')
    ap.add_argument('--config')
    ap.add_argument('--runtime')
    ap.add_argument('--log')
    ap.add_argument('--xray')
    ap.add_argument('--namespace-pid', type=int)
    args = ap.parse_args()
    if args.operation == 'send':
        send(args.socket, args.name)
    else:
        if not (args.expected_ns and args.config and args.runtime and args.log and args.xray and args.namespace_pid):
            ap.error('serve requires --expected-ns, --config, --runtime, --log, --xray and --namespace-pid')
        serve(args.socket, args.expected_ns, args.name, args.config, args.runtime, args.log,
              args.xray, args.namespace_pid)
