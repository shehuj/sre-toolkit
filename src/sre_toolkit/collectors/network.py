"""Network-level health: HTTP, TCP, DNS, TLS.

All four cost nothing but egress, so they are the first thing to reach for and
run by default. No AWS credentials required.
"""

from __future__ import annotations

import socket
import ssl
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone
from typing import Any
from urllib.parse import urlparse

UTC = timezone.utc
USER_AGENT = "sre-toolkit/0.1.0 (+health-check)"


def _hostport(target: str, default_scheme: str = "https") -> tuple[str, int, str]:
    if "://" not in target:
        target = f"{default_scheme}://{target}"
    parsed = urlparse(target)
    host = parsed.hostname or ""
    port = parsed.port or (443 if parsed.scheme == "https" else 80)
    return host, port, target


def http_check(
    target: str, timeout: float = 5.0, method: str = "GET", expect: int | None = None
) -> dict[str, Any]:
    """Single HTTP probe with a phase breakdown (dns / connect / total)."""
    host, port, url = _hostport(target)
    out: dict[str, Any] = {"check": "http", "target": url, "host": host, "port": port}

    dns_started = time.perf_counter()
    try:
        addrs = socket.getaddrinfo(host, port, proto=socket.IPPROTO_TCP)
        out["dns_ms"] = round((time.perf_counter() - dns_started) * 1000, 1)
        out["resolved_ip"] = addrs[0][4][0]
    except OSError as exc:
        out.update(ok=False, error=f"DNS resolution failed: {exc}")
        return out

    connect_ms = tcp_check(host, port, timeout).get("connect_ms")
    out["connect_ms"] = connect_ms

    request = urllib.request.Request(url, method=method, headers={"User-Agent": USER_AGENT})
    started = time.perf_counter()
    try:
        with urllib.request.urlopen(request, timeout=timeout) as resp:  # noqa: S310 - user URL
            body = resp.read(65536)
            out.update(
                status=resp.status,
                total_ms=round((time.perf_counter() - started) * 1000, 1),
                body_bytes=len(body),
                server=resp.headers.get("Server", ""),
                content_type=resp.headers.get("Content-Type", ""),
                final_url=resp.geturl(),
            )
    except urllib.error.HTTPError as exc:
        out.update(
            status=exc.code,
            total_ms=round((time.perf_counter() - started) * 1000, 1),
            body_bytes=0,
            error=f"HTTP {exc.code} {exc.reason}",
        )
    except (urllib.error.URLError, socket.timeout, ssl.SSLError, OSError) as exc:
        out.update(ok=False, total_ms=round((time.perf_counter() - started) * 1000, 1),
                   error=str(getattr(exc, "reason", exc)))
        return out

    status = out.get("status", 0)
    out["ok"] = status == expect if expect else 200 <= status < 400
    return out


def tcp_check(host: str, port: int, timeout: float = 5.0) -> dict[str, Any]:
    started = time.perf_counter()
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return {
                "check": "tcp",
                "target": f"{host}:{port}",
                "ok": True,
                "connect_ms": round((time.perf_counter() - started) * 1000, 1),
            }
    except OSError as exc:
        return {"check": "tcp", "target": f"{host}:{port}", "ok": False, "error": str(exc)}


def dns_check(host: str, record_types: tuple[str, ...] = ("A", "AAAA", "CNAME")) -> dict[str, Any]:
    """Resolve a name. Uses dnspython for record detail when installed, else the
    stdlib resolver (which still answers the only question that matters during an
    incident: does this name resolve, and to what)."""
    out: dict[str, Any] = {"check": "dns", "target": host, "records": {}}
    started = time.perf_counter()
    try:
        import dns.resolver  # type: ignore

        resolver = dns.resolver.Resolver()
        resolver.lifetime = 5.0
        for rtype in record_types:
            try:
                answer = resolver.resolve(host, rtype)
                out["records"][rtype] = [r.to_text() for r in answer]
                out["ttl"] = answer.rrset.ttl if answer.rrset else None
            except Exception:  # noqa: BLE001 - absent record type is normal
                continue
        out["resolver"] = "dnspython"
    except ImportError:
        try:
            infos = socket.getaddrinfo(host, None, proto=socket.IPPROTO_TCP)
            out["records"]["A"] = sorted({i[4][0] for i in infos if i[0] == socket.AF_INET})
            v6 = sorted({i[4][0] for i in infos if i[0] == socket.AF_INET6})
            if v6:
                out["records"]["AAAA"] = v6
        except OSError as exc:
            out.update(ok=False, error=str(exc))
            return out
        out["resolver"] = "stdlib"
    out["lookup_ms"] = round((time.perf_counter() - started) * 1000, 1)
    out["ok"] = bool(out["records"])
    if not out["ok"]:
        out["error"] = "no A/AAAA/CNAME records returned"
    return out


def tls_check(target: str, timeout: float = 5.0, warn_days: int = 30) -> dict[str, Any]:
    """Certificate expiry, issuer, SANs and negotiated protocol."""
    host, port, _ = _hostport(target)
    out: dict[str, Any] = {"check": "tls", "target": f"{host}:{port}", "host": host}
    ctx = ssl.create_default_context()
    try:
        with socket.create_connection((host, port), timeout=timeout) as sock:
            with ctx.wrap_socket(sock, server_hostname=host) as tls:
                cert = tls.getpeercert() or {}
                out["protocol"] = tls.version()
                out["cipher"] = (tls.cipher() or ("",))[0]
    except ssl.SSLCertVerificationError as exc:
        out.update(ok=False, verified=False, error=f"certificate verification failed: {exc.verify_message}")
        return out
    except (OSError, ssl.SSLError) as exc:
        out.update(ok=False, error=str(exc))
        return out

    out["verified"] = True
    not_after = cert.get("notAfter")
    if not_after:
        expires = datetime.strptime(not_after, "%b %d %H:%M:%S %Y %Z").replace(tzinfo=UTC)
        days = (expires - datetime.now(UTC)).total_seconds() / 86400
        out["expires_at"] = expires.isoformat()
        out["days_remaining"] = round(days, 1)
        out["expiring_soon"] = days <= warn_days
        out["expired"] = days <= 0
    out["issuer"] = _rdn(cert.get("issuer", ()), "organizationName") or _rdn(
        cert.get("issuer", ()), "commonName"
    )
    out["subject"] = _rdn(cert.get("subject", ()), "commonName")
    out["sans"] = [v for k, v in cert.get("subjectAltName", ()) if k == "DNS"][:20]
    out["ok"] = out["verified"] and not out.get("expired", False)
    if out.get("expired"):
        out["error"] = "certificate has expired"
    return out


def _rdn(rdns: tuple, field: str) -> str:
    for group in rdns:
        for key, value in group:
            if key == field:
                return value
    return ""
