# claude-netns

A dedicated, rootless Podman development container with a separate network namespace and a fail-closed transparent Xray Reality exit. This setup does not change host routes or the existing `xray-dae.service`.

## Run

```sh
cd ~/Applications/OwnProject/claude-netns
./control.sh start
./enter.sh
```

`control.sh start` starts the existing `claude-private-netns` Pod and `claude-dev-isolated` container, re-creates the named `claude-tun` if necessary, and starts the **local `./xray` binary** in the host network namespace. The short-lived helper opens the TUN in the isolated namespace and passes its FD and interface index to the host-side Xray through a Unix socket. Re-running `control.sh start` reuses a healthy running gateway; a Pod/namespace restart is handled by replacing the previous dedicated gateway.

`enter.sh` opens a normal Bash session in `/home/claude`. The container user is `aromatic` (UID 1000), with passwordless sudo configured *inside this container only* (`sudo -n id`).

## Isolated objects

- Pod: `claude-private-netns`, `--network none` (no pasta/bridge/host underlay)
- Container: `claude-dev-isolated` (`archlinux:latest`), Rootless Podman
- Only permanent Podman bind mount: `./home` -> `/home/claude`; optional dynamic bind mounts are configured in `config.toml`
- Separate NetNS: loopback and `claude-tun` only
- IPv4: sole default route through `claude-tun` (`10.203.0.2/30`)
- IPv6: unreachable default route
- DNS: container `/etc/resolv.conf` uses `1.1.1.1`; its packets travel through the same TUN
- Development container has no `NET_ADMIN`/`NET_RAW`; optional Wayland sharing exposes only the selected compositor socket, not the host session bus or runtime directory
- Host-side Xray's only usable proxy outbound is the VLESS Reality share URL in the local, untracked `config.toml`

## Files

- `control.sh`: the only control entrypoint (start/stop, mounts, Wayland, KVM, SSH service, Xray build)
- `enter.sh`: the only container-entry shell script
- `claude_netns/`: Python implementation shared by both entrypoints
- `config.toml`: the only manually maintained runtime configuration (mode `0600`; contains the proxy share URL)
- `config.toml.example`: public example of the configuration schema, with placeholder credentials
- `xray`: dedicated Xray v26.10.31 binary, independent from `/usr/bin/xray`
- `runtime/xray.json`: generated dedicated Xray configuration (permission `0600`; never edit manually)
- `tun-bridge.py`: FD handoff and host-side Xray process supervision
- `xray-core/`: corresponding source tree with the small external-TUN-FD interface-index compatibility change
- `runtime/` and `logs/`: generated state and diagnostics

## Wayland socket (optional)

```sh
./control.sh wayland enable
./control.sh wayland status
./control.sh wayland disable
```

`control.sh wayland enable` attaches only the active host Wayland socket (e.g.
`/run/user/1000/wayland-1`) to `/run/user/1000/wayland-claude` in the
running container. The mounted socket stays owned by UID:GID 1000:1000.
No host D-Bus, PipeWire, PulseAudio, GPU devices or entire XDG runtime
directory is mounted.

`./enter.sh` supplies `XDG_RUNTIME_DIR=/run/user/1000` and
`WAYLAND_DISPLAY=wayland-claude` when the socket is enabled and connected.
SSH uses Fish as the default login shell, so the Python Wayland controller also manages
`/home/claude/.config/fish/conf.d/claude-wayland.fish` to set these two
variables automatically for new SSH sessions while the socket exists.
For an SSH Fish shell opened before enabling the integration, run:

```fish
source ~/.config/fish/conf.d/claude-wayland.fish
```

The enabled setting and socket identity are stored in
`runtime/wayland.enabled` and `runtime/wayland.state` (ignored by Git). `control.sh start`
restores the Wayland bind after container restarts, and `enter.sh`
refreshes it after compositor socket replacement. If no Wayland session is
available at service startup, networking continues without GUI integration.

## KVM acceleration (optional)

The existing Rootless Podman container can use KVM without recreating it or
running the guest process as root. Only `/dev/kvm` is shared; this does not
expose other host devices or enable privileged container mode.

```sh
./control.sh kvm enable
./control.sh kvm status
./control.sh kvm disable
```

`enable` dynamically attaches the host character device to `/dev/kvm`
inside the container and verifies `KVM_GET_API_VERSION` and `KVM_CREATE_VM`
as container UID 1000. Both the opt-in setting and mount identity live in
`runtime/kvm.enabled` and `runtime/kvm.state` (ignored by Git).
`control.sh start` restores the device after the container is restarted. `disable`
unmounts the device and removes its container-local placeholder.

To check acceleration once enabled:

```sh
podman exec --user 1000:1000 claude-dev-isolated sh -c \
  'printf "info kvm\nquit\n" | qemu-system-x86_64 -machine q35,accel=kvm -m 128 -smp 1 -nodefaults -display none -monitor stdio -S'
```

## Source control and Xray build

This project is a standalone Git repository. `xray-core/` is an upstream Git
submodule pinned to Xray-core `v26.10.31` (commit
`a4a3dff5b20c2faae567b742c172c7b95e75c083`). The root repository keeps
the rootless external-TUN interface-index change separately in
`patches/0001-xray-tun-external-ifindex.patch`. Do not commit that local change
to the upstream submodule.

Initialize the upstream source and local configuration after cloning:

```sh
git submodule update --init
cp config.toml.example config.toml
chmod 600 config.toml
```

Edit `config.toml` to set the container, TUN, DNS, SSH ports, mounts, and
`[xray].url`. The URL must be a `vless://` share link for TCP + Reality,
including its `sni`, `fp` and `pbk` query parameters. Other transports or
unsupported parameters are rejected rather than ignored. Python reads this
file with the standard-library `tomllib` module and generates
`runtime/xray.json` automatically. This generated file keeps the same TUN
inbound, fail-closed route and blackhole outbound as before.

After changing the TOML, restart `claude-netns.service` to apply the SSH
listener and proxy settings; `control.sh start` also detects Xray setting
changes when used without the SSH service. The sample contains no working
credentials. To build without changing the submodule or overwriting the
currently running Xray executable:

```sh
./control.sh build-xray
```

The controller checks the submodule gitlink, exports that exact upstream commit,
applies the project patch in an ignored temporary directory, and produces
`build/xray`. Deploy the new binary separately when appropriate. The live
`./xray`, `config.toml`, `home/`, runtime data, diagnostic
files and logs are intentionally not committed. Existing installations keep
using their current local executable and configuration.

## Dynamic host-directory mounts

Add `[[mounts]]` entries to `config.toml` using separate source/target fields:

```toml
[[mounts]]
source = "/home/aromatic/Applications/OwnProject/grub_port"
target = "/workspace/grub"

[[mounts]]
source = "/home/aromatic/Applications/WorkProject"
target = "/workspace/work"
```

Only existing host directories owned by UID:GID `1000:1000` may be mounted.
The container's UID `1000` remains the owner of these directories, and new
container mountpoints are created as UID:GID `1000:1000`. The controller never
changes file ownership. Nested files keep their existing host ownership.

```sh
./control.sh mount apply
./control.sh mount list
./control.sh mount remove /workspace/grub
./control.sh mount remove-all
```

Mounts become visible to the running container immediately, and `control.sh start`
reapplies the configuration after container restarts. Unmount before removing
a `[[mounts]]` entry from `config.toml`; only mounts still matching their configured source
may be removed by the controller. New mountpoints must be empty and owned by UID
1000, and unrelated mounts are never replaced.

## Fail-closed

No `pasta` device or physical route exists in the container's namespace. When Xray exits, `claude-tun` loses carrier and IPv4/IPv6/DNS external connections fail; if the TUN is removed, its default route is removed too. The independent gateway must be running for network access. A temporary helper with `NET_ADMIN` is used only to initialize/reopen the TUN; the normal developer container cannot change its own network routes.

To stop only the isolated environment:

```sh
podman pod stop claude-private-netns
```

Then use `./control.sh start` to restore its namespace, TUN and gateway. This Pod is not configured for autostart and should never be switched to `--network host`, pasta or bridge unless you intentionally redesign the isolation.

## SSH access from the local network

The user systemd service opens `192.168.50.200:22222` (TCP by default, configured under `[ssh]`) and forwards each
connection over `podman exec` to an OpenSSH daemon listening only at
`127.0.0.1:22022` (also from `[ssh]`) **inside** the isolated Pod. The Pod remains `--network none`;
its normal outbound traffic still requires the host-side Xray TUN FD.

```sh
ssh -p 22222 aromatic@192.168.50.200
```

SSH authentication accepts **public keys only**. The container's
`/home/claude/.ssh/authorized_keys` initially contains only the public key from
the host's `~/.ssh/id_ed25519.pub`. The private key is never mounted or copied
into the container. Password/keyboard-interactive authentication, root login,
and SSH forwarding are disabled. To grant access to another client, explicitly
add that client's trusted public key to `./home/.ssh/authorized_keys`.

The pinned SSH **host** public key for port 22222 is saved in
`./ssh_known_hosts` (not an authentication credential).

## systemd user service

`./claude-netns.service` is the source unit file. Its only external path is the
required symlink at `~/.config/systemd/user/claude-netns.service`.
The unit is enabled under `default.target` with user lingering already enabled.
It starts the Pod, independent host-side `./xray`, container OpenSSH, and
`192.168.50.200:22222` TCP relay, and stops them together.

```sh
systemctl --user status claude-netns.service
systemctl --user start claude-netns.service
systemctl --user stop claude-netns.service
systemctl --user restart claude-netns.service
journalctl --user -u claude-netns.service -n 30 --no-pager
```

`./control.sh stop` delegates to systemd when the service is running, so the LAN SSH
listener stops along with the gateway. `./control.sh start` remains available to start
the network portion manually; use `systemctl --user start claude-netns.service`
for the complete environment including SSH.
