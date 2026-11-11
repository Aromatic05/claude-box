"""Single private TOML configuration source for the runtime."""
from __future__ import annotations

import ipaddress
import os
import re
import tomllib
from dataclasses import dataclass
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
CONFIG_PATH = ROOT / "config.toml"


class ConfigError(ValueError):
    pass


@dataclass(frozen=True)
class Container:
    pod: str
    name: str
    uid: int
    gid: int
    home: str

    @property
    def user(self) -> str:
        return f"{self.uid}:{self.gid}"


@dataclass(frozen=True)
class Network:
    tun: str
    address: str
    mtu: int
    dns: tuple[str, ...]


@dataclass(frozen=True)
class SSH:
    listen_address: str
    listen_port: int
    guest_address: str
    guest_port: int


@dataclass(frozen=True)
class Wayland:
    display: str
    runtime_dir: str


@dataclass(frozen=True)
class KVM:
    device: str


@dataclass(frozen=True)
class Images:
    arch: str
    python: str


@dataclass(frozen=True)
class Xray:
    url: str
    log_level: str


@dataclass(frozen=True)
class Mount:
    source: str
    target: str


@dataclass(frozen=True)
class Settings:
    container: Container
    network: Network
    ssh: SSH
    wayland: Wayland
    kvm: KVM
    images: Images
    xray: Xray
    mounts: tuple[Mount, ...]


def table(document: dict, name: str, keys: set[str]) -> dict:
    value = document.get(name)
    if not isinstance(value, dict):
        raise ConfigError(f"config.toml: [{name}] must be a table")
    unknown = value.keys() - keys
    if unknown:
        raise ConfigError(f"config.toml: [{name}] unexpected keys: {sorted(unknown)}")
    if value.keys() != keys:
        missing = keys - value.keys()
        raise ConfigError(f"config.toml: [{name}] missing keys: {sorted(missing)}")
    return value


def string(value: object, label: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ConfigError(f"config.toml: {label} must be a non-empty string")
    return value


def positive(value: object, label: str, maximum: int) -> int:
    if type(value) is not int or not 1 <= value <= maximum:
        raise ConfigError(f"config.toml: {label} must be an integer between 1 and {maximum}")
    return value


def absolute(value: object, label: str) -> str:
    path = string(value, label)
    if not path.startswith("/") or path == "/" or os.path.normpath(path) != path or "//" in path:
        raise ConfigError(f"config.toml: {label} must be a normalized absolute path")
    return path


def valid_port(value: object, label: str) -> int:
    return positive(value, label, 65535)


def valid_ipv4(value: object, label: str) -> str:
    text = string(value, label)
    try:
        addr = ipaddress.IPv4Address(text)
    except ipaddress.AddressValueError as exc:
        raise ConfigError(f"config.toml: {label} must be an IPv4 address") from exc
    return str(addr)


def load() -> Settings:
    if not CONFIG_PATH.is_file():
        raise ConfigError("Missing config.toml; copy config.toml.example and configure it")
    if CONFIG_PATH.stat().st_mode & 0o777 != 0o600:
        raise ConfigError("config.toml contains credentials and must have permission 0600")
    try:
        with CONFIG_PATH.open("rb") as handle:
            data = tomllib.load(handle)
    except tomllib.TOMLDecodeError as exc:
        raise ConfigError(f"Invalid config.toml: {exc}") from exc
    allowed = {"container", "network", "ssh", "wayland", "kvm", "images", "xray", "mounts"}
    if unknown := data.keys() - allowed:
        raise ConfigError(f"config.toml: unexpected tables: {sorted(unknown)}")

    c = table(data, "container", {"pod", "name", "uid", "gid", "home"})
    container = Container(
        string(c["pod"], "container.pod"),
        string(c["name"], "container.name"),
        positive(c["uid"], "container.uid", 65535),
        positive(c["gid"], "container.gid", 65535),
        absolute(c["home"], "container.home"),
    )
    n = table(data, "network", {"tun", "address", "mtu", "dns"})
    tun = string(n["tun"], "network.tun")
    if not re.fullmatch(r"[A-Za-z0-9_.-]{1,15}", tun):
        raise ConfigError("config.toml: network.tun must be a Linux interface name (1-15 characters)")
    address = string(n["address"], "network.address")
    try:
        address = str(ipaddress.IPv4Interface(address))
    except ValueError as exc:
        raise ConfigError("config.toml: network.address must be IPv4 CIDR") from exc
    dns = n["dns"]
    if not isinstance(dns, list) or not dns:
        raise ConfigError("config.toml: network.dns must be a non-empty array")
    network = Network(tun, address, positive(n["mtu"], "network.mtu", 65535),
                      tuple(valid_ipv4(ip, "network.dns") for ip in dns))
    s = table(data, "ssh", {"listen_address", "listen_port", "guest_address", "guest_port"})
    listen = valid_ipv4(s["listen_address"], "ssh.listen_address")
    guest = valid_ipv4(s["guest_address"], "ssh.guest_address")
    if not ipaddress.ip_address(guest).is_loopback:
        raise ConfigError("config.toml: ssh.guest_address must be loopback")
    if ipaddress.ip_address(listen).is_unspecified:
        raise ConfigError("config.toml: ssh.listen_address must bind an explicit host address")
    ssh = SSH(listen, valid_port(s["listen_port"], "ssh.listen_port"),
              guest, valid_port(s["guest_port"], "ssh.guest_port"))
    w = table(data, "wayland", {"display"})
    display = string(w["display"], "wayland.display")
    if not re.fullmatch(r"[A-Za-z0-9_.-]+", display) or not display.startswith("wayland-"):
        raise ConfigError("config.toml: wayland.display must be a Wayland socket basename")
    wayland = Wayland(display, f"/run/user/{container.uid}")
    k = table(data, "kvm", {"device"})
    kvm = KVM(absolute(k["device"], "kvm.device"))
    i = table(data, "images", {"arch", "python"})
    images = Images(string(i["arch"], "images.arch"), string(i["python"], "images.python"))
    x = table(data, "xray", {"url", "log_level"})
    xray = Xray(string(x["url"], "xray.url"), string(x["log_level"], "xray.log_level"))
    if xray.log_level not in {"debug", "info", "warning", "error", "none"}:
        raise ConfigError("config.toml: xray.log_level must be debug/info/warning/error/none")

    mounts = data.get("mounts", [])
    if not isinstance(mounts, list):
        raise ConfigError("config.toml: mounts must be an array of tables")
    mount_items = []
    for idx, item in enumerate(mounts, start=1):
        if not isinstance(item, dict) or item.keys() != {"source", "target"}:
            raise ConfigError(f"config.toml: [[mounts]] #{idx} requires source and target")
        mount_items.append(Mount(
            absolute(item["source"], f"mounts[{idx}].source"),
            absolute(item["target"], f"mounts[{idx}].target"),
        ))
    return Settings(container, network, ssh, wayland, kvm, images, xray, tuple(mount_items))


settings = load()
