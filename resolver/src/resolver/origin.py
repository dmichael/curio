"""Safe request-origin selection for direct and explicitly trusted proxy traffic."""

from __future__ import annotations

import ipaddress
import re
from collections.abc import Iterable
from urllib.parse import urlsplit

from starlette.requests import Request

_HOST_LABEL = re.compile(r"[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?$")


def _has_bad_characters(value: str) -> bool:
    return not value or any(ord(char) < 33 or ord(char) == 127 or ord(char) > 126 for char in value)


def _has_control_characters(value: str) -> bool:
    return any(ord(char) < 32 or ord(char) == 127 or ord(char) > 126 for char in value)


def _normal_host(host: str) -> str | None:
    """Return a safe, canonical HTTP Host name (without a port)."""
    if _has_bad_characters(host) or any(char in host for char in "/@\\?#[]"):
        return None
    try:
        return str(ipaddress.ip_address(host)).lower()
    except ValueError:
        pass
    if len(host) > 253 or host.endswith("."):
        return None
    labels = host.split(".")
    if not all(_HOST_LABEL.fullmatch(label) for label in labels):
        return None
    return host.lower()


def _origin_from_parts(scheme: str, authority: str) -> str | None:
    """Build an origin from a scheme and Host-style authority, or reject it."""
    if _has_bad_characters(scheme) or _has_bad_characters(authority):
        return None
    if scheme.lower() not in {"http", "https"}:
        return None
    if any(char in authority for char in "/@\\?#") or authority.endswith(":"):
        return None
    try:
        parsed = urlsplit(f"//{authority}")
        host = parsed.hostname
        port = parsed.port
    except ValueError:
        return None
    if not host or parsed.username is not None or parsed.password is not None:
        return None
    normal_host = _normal_host(host)
    if normal_host is None:
        return None
    if port is not None and not 1 <= port <= 65535:
        return None
    host_for_url = f"[{normal_host}]" if ":" in normal_host else normal_host
    # URL origins normalize default ports; doing so also makes browser Origin
    # comparisons work when a proxy explicitly sends :80 or :443.
    if port is None or (scheme.lower(), port) in {("http", 80), ("https", 443)}:
        return f"{scheme.lower()}://{host_for_url}"
    return f"{scheme.lower()}://{host_for_url}:{port}"


def normalize_origin(value: str) -> str | None:
    """Normalize a complete HTTP origin, allowing only an optional trailing '/'."""
    if _has_bad_characters(value):
        return None
    try:
        parsed = urlsplit(value)
    except ValueError:
        return None
    if parsed.path not in {"", "/"} or parsed.query or parsed.fragment or not parsed.netloc:
        return None
    return _origin_from_parts(parsed.scheme, parsed.netloc)


def parse_trusted_proxy_cidrs(value: str) -> tuple[ipaddress.IPv4Network | ipaddress.IPv6Network, ...]:
    """Parse the comma-separated immediate-proxy allowlist, failing closed."""
    if not value.strip():
        return ()
    networks = []
    for item in value.split(","):
        item = item.strip()
        if not item:
            raise ValueError("trusted proxy CIDRs must not contain empty entries")
        try:
            networks.append(ipaddress.ip_network(item, strict=False))
        except ValueError as exc:
            raise ValueError(f"invalid trusted proxy CIDR: {item}") from exc
    return tuple(networks)


def _is_trusted_immediate_proxy(request: Request, networks: Iterable[ipaddress.IPv4Network | ipaddress.IPv6Network]) -> bool:
    if request.client is None:
        return False
    try:
        client = ipaddress.ip_address(request.client.host)
    except ValueError:
        return False
    return any(client in network for network in networks)


def _single_header(request: Request, name: str) -> str | None:
    values = request.headers.getlist(name)
    if len(values) != 1 or _has_control_characters(values[0]):
        return None
    return values[0].strip()


def _split_unquoted(value: str, delimiter: str) -> list[str] | None:
    """Split a Forwarded header delimiter without treating quoted text as one."""
    pieces, current, quoted, escaped = [], [], False, False
    for char in value:
        if escaped:
            current.append(char)
            escaped = False
        elif char == "\\" and quoted:
            current.append(char)
            escaped = True
        elif char == '"':
            current.append(char)
            quoted = not quoted
        elif char == delimiter and not quoted:
            pieces.append("".join(current))
            current = []
        else:
            current.append(char)
    if quoted or escaped:
        return None
    pieces.append("".join(current))
    return pieces


def _forwarded_value(value: str) -> str | None:
    """Decode the limited quoted-string form needed for RFC 7239 host values."""
    if not value.startswith('"'):
        return value if '"' not in value and "\\" not in value else None
    if len(value) < 2 or not value.endswith('"'):
        return None
    decoded, escaped = [], False
    for char in value[1:-1]:
        if escaped:
            # Restrict quoted-pairs to the two characters that need escaping;
            # accepting arbitrary escapes creates alternate authority spellings.
            if char not in {'"', "\\"}:
                return None
            decoded.append(char)
            escaped = False
        elif char == "\\":
            escaped = True
        elif char == '"':
            return None
        else:
            decoded.append(char)
    return "".join(decoded) if not escaped else None


def _forwarded_origin(value: str) -> str | None:
    """Read only the rightmost (immediate proxy) RFC 7239 element."""
    if _has_control_characters(value):
        return None
    elements = _split_unquoted(value, ",")
    if not elements:
        return None
    element = elements[-1].strip()
    if not element:
        return None
    parameter_values = _split_unquoted(element, ";")
    if not parameter_values:
        return None
    parameters: dict[str, str] = {}
    for parameter in parameter_values:
        name, separator, raw_value = parameter.strip().partition("=")
        name = name.strip().lower()
        decoded = _forwarded_value(raw_value.strip())
        if not separator or not name or not decoded or name in parameters:
            return None
        parameters[name] = decoded
    proto = parameters.get("proto")
    host = parameters.get("host")
    return _origin_from_parts(proto, host) if proto and host else None


def forwarded_origin(request: Request, trusted_proxy_cidrs: str) -> str | None:
    """Return a valid forwarded origin only from an allowlisted peer address."""
    networks = parse_trusted_proxy_cidrs(trusted_proxy_cidrs)
    if not networks or not _is_trusted_immediate_proxy(request, networks):
        return None

    forwarded = request.headers.getlist("forwarded")
    if forwarded:
        return _forwarded_origin(forwarded[0]) if len(forwarded) == 1 else None

    proto = _single_header(request, "x-forwarded-proto")
    host = _single_header(request, "x-forwarded-host")
    if proto is None or host is None or "," in proto or "," in host:
        return None
    return _origin_from_parts(proto, host)


def effective_origin(request: Request, public_base_url: str, trusted_proxy_cidrs: str) -> str | None:
    """Choose configured, trusted-forwarded, then a validated direct origin."""
    if public_base_url:
        # Settings validates this at startup; never substitute an untrusted
        # request Host if a direct Settings construction bypassed validation.
        return normalize_origin(public_base_url)
    return forwarded_origin(request, trusted_proxy_cidrs) or normalize_origin(str(request.base_url))
