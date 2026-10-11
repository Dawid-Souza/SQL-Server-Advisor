from __future__ import annotations

import ipaddress
import re


_HOST_LABEL = re.compile(r"^[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?$")


def normalize_server_name(value: str) -> str:
    """Validate a host without ever passing user-controlled shell syntax."""
    host = value.strip()
    if host in {".", "localhost"}:
        return host
    if not host or len(host) > 255 or "\\" in host or "/" in host or ":" in host:
        raise ValueError("Informe apenas o nome DNS, nome NetBIOS ou IP do Windows Server.")
    try:
        ipaddress.ip_address(host)
        return host
    except ValueError:
        pass
    labels = host.rstrip(".").split(".")
    if any(not _HOST_LABEL.fullmatch(label) for label in labels):
        raise ValueError("O nome do servidor contém caracteres inválidos.")
    return host.rstrip(".")


def safe_public_error(message: str, secret: str | None = None) -> str:
    result = message.strip()
    if secret:
        result = result.replace(secret, "********")
    return result[:1200]

