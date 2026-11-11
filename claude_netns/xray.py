"""Translate one VLESS Reality share URL into the existing Xray TUN profile.

Unsupported protocols, transports and URL options fail explicitly rather than
silently changing the route or authentication semantics.
"""
from __future__ import annotations

import json
import os
import re
import tempfile
import uuid
from pathlib import Path
from urllib.parse import parse_qsl, unquote, urlsplit

from .config import ConfigError, settings

XRAY_CONFIG = Path(__file__).resolve().parent.parent / "runtime/xray.json"
INBOUND_TAG = "claude-tun-in"
OUTBOUND_TAG = "own-us-reality"


def outbound_from_link(url: str) -> dict:
    try:
        parsed = urlsplit(url)
        port = parsed.port
    except ValueError as exc:
        raise ConfigError("xray.url: invalid VLESS link authority or port") from exc
    if parsed.scheme.lower() != "vless":
        raise ConfigError("xray.url: only vless:// share links are supported")
    if not parsed.hostname or port is None or not 1 <= port <= 65535:
        raise ConfigError("xray.url: server and port are required")
    if parsed.password is not None or not parsed.username or parsed.path:
        raise ConfigError("xray.url: expected vless://UUID@host:port?parameters")
    identifier = unquote(parsed.username)
    try:
        uuid.UUID(identifier)
    except ValueError as exc:
        raise ConfigError("xray.url: user ID must be a UUID") from exc
    try:
        pairs = parse_qsl(parsed.query, keep_blank_values=True, strict_parsing=True)
    except ValueError as exc:
        raise ConfigError("xray.url: malformed query parameters") from exc
    params = dict(pairs)
    if len(params) != len(pairs):
        raise ConfigError("xray.url: duplicate query parameters")
    supported = {"type", "security", "encryption", "flow", "sni", "fp", "pbk", "sid", "spx", "headerType"}
    unknown = params.keys() - supported
    if unknown:
        raise ConfigError(f"xray.url: unsupported parameters: {sorted(unknown)}")
    if params.get("type", "tcp") != "tcp" or params.get("security") != "reality":
        raise ConfigError("xray.url: only TCP + Reality links are supported")
    if params.get("headerType", "none") != "none":
        raise ConfigError("xray.url: only TCP without a custom header is supported")
    if params.get("encryption", "none") != "none":
        raise ConfigError("xray.url: VLESS encryption must be none")
    if not all(params.get(field) for field in ("sni", "pbk", "fp")):
        raise ConfigError("xray.url: Reality sni, pbk and fp are required")
    short_id = params.get("sid", "")
    if short_id and (not re.fullmatch(r"[0-9a-fA-F]{2,16}", short_id) or len(short_id) % 2):
        raise ConfigError("xray.url: Reality sid must be even-length hex (up to 16 digits)")

    return {
        "tag": OUTBOUND_TAG,
        "protocol": "vless",
        "settings": {
            "vnext": [{
                "address": parsed.hostname,
                "port": port,
                "users": [{
                    "id": identifier,
                    "encryption": "none",
                    "flow": params.get("flow", ""),
                }],
            }],
        },
        "streamSettings": {
            "network": "tcp",
            "security": "reality",
            "realitySettings": {
                "fingerprint": params["fp"],
                "serverName": params["sni"],
                "publicKey": params["pbk"],
                "shortId": short_id,
                "spiderX": params.get("spx", "/"),
            },
        },
    }


def generate() -> dict:
    return {
        "log": {"loglevel": settings.xray.log_level},
        "inbounds": [{
            "tag": INBOUND_TAG,
            "protocol": "tun",
            "settings": {
                "name": settings.network.tun,
                "mtu": settings.network.mtu,
            },
        }],
        "outbounds": [
            outbound_from_link(settings.xray.url),
            {"tag": "block", "protocol": "blackhole"},
        ],
        "routing": {
            "domainStrategy": "AsIs",
            "rules": [{
                "type": "field",
                "inboundTag": [INBOUND_TAG],
                "outboundTag": OUTBOUND_TAG,
            }],
        },
    }


def write_generated_config() -> bool:
    """Write mode-0600 runtime JSON only if its effective settings changed."""
    data = json.dumps(generate(), indent=2, ensure_ascii=False) + "\n"
    XRAY_CONFIG.parent.mkdir(parents=True, exist_ok=True)
    if XRAY_CONFIG.is_file() and XRAY_CONFIG.read_text() == data:
        return False
    fd, tmp = tempfile.mkstemp(prefix=".xray-", suffix=".json", dir=XRAY_CONFIG.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            os.fchmod(stream.fileno(), 0o600)
            stream.write(data)
        os.replace(tmp, XRAY_CONFIG)
    finally:
        if os.path.exists(tmp):
            os.unlink(tmp)
    return True
