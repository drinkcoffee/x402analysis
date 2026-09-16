"""Passive infrastructure analysis for facilitator hostnames.

Methodology follows domain_check.py (../domain-check/domain_check.py):
  - WHOIS via RDAP (HTTPS/JSON), the IANA bootstrap registry resolves which
    RDAP server to query for a given TLD.
  - SSL/TLS certificate inspection via a plain TCP+TLS handshake, reading the
    subject/issuer/SAN fields out of the peer certificate.
  - IP geolocation via ip-api.com (free, no key). Unlike domain_check.py's
    one-IP-at-a-time calls, this module batches all IPs into a single POST to
    ip-api.com/batch (up to 100 per request) since a full facilitator sweep
    touches dozens of hosts at once.

Everything here is read-only/passive: DNS resolution, a TLS handshake, and
plain GET/POST requests to public WHOIS and geolocation services. No requests
are sent to the facilitators themselves.
"""

from __future__ import annotations

import socket
import ssl
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
from typing import Any, Optional
from urllib.parse import urlparse

import requests

# A few well-known multi-label public suffixes. Not a full public-suffix-list
# implementation - just enough to avoid mis-splitting the handful of ccTLD
# patterns likely to show up among facilitator domains.
_MULTI_LABEL_SUFFIXES = {
    "co.uk", "org.uk", "gov.uk", "ac.uk", "co.jp", "co.nz", "co.in",
    "com.au", "com.br", "co.kr", "com.cn",
}


def registrable_domain(hostname: str) -> str:
    labels = hostname.lower().rstrip(".").split(".")
    if len(labels) <= 2:
        return hostname.lower()
    if ".".join(labels[-2:]) in _MULTI_LABEL_SUFFIXES and len(labels) > 2:
        return ".".join(labels[-3:])
    return ".".join(labels[-2:])


def hostnames_for_facilitator(entry: dict[str, Any]) -> list[tuple[str, str]]:
    """Unique (role, hostname) pairs worth analyzing for one registry entry."""
    hosts: list[tuple[str, str]] = []
    seen: set[str] = set()
    for role, url in (("api", entry.get("base_url")), ("docs", entry.get("docs_url"))):
        if not url:
            continue
        host = urlparse(url).hostname
        if host and host not in seen:
            seen.add(host)
            hosts.append((role, host))
    return hosts


def parse_hostname(value: str) -> Optional[str]:
    """Hostname from either a full URL or a bare domain (with or without a
    path)."""
    value = (value or "").strip()
    if not value:
        return None
    if "//" not in value:
        value = f"//{value}"
    return urlparse(value).hostname


# ── DNS / IP ──────────────────────────────────────────────────────────────


def resolve_ip(hostname: str) -> Optional[str]:
    try:
        return socket.gethostbyname(hostname)
    except OSError:
        return None


def reverse_dns(ip: str) -> Optional[str]:
    try:
        return socket.gethostbyaddr(ip)[0]
    except OSError:
        return None


# ── SSL / TLS ─────────────────────────────────────────────────────────────


def check_ssl(hostname: str, timeout: float = 8.0) -> dict[str, Any]:
    ctx = ssl.create_default_context()
    try:
        with socket.create_connection((hostname, 443), timeout=timeout) as sock:
            with ctx.wrap_socket(sock, server_hostname=hostname) as ssock:
                cert = ssock.getpeercert()
    except Exception as exc:  # noqa: BLE001 - surfaced to the report, not raised
        return {"valid": False, "error": str(exc)}

    subject = dict(x[0] for x in cert.get("subject", []))
    issuer = dict(x[0] for x in cert.get("issuer", []))
    san_list = [v for t, v in cert.get("subjectAltName", []) if t == "DNS"]

    not_after = cert.get("notAfter")
    expiry_dt = None
    days_left = None
    if not_after:
        try:
            expiry_dt = datetime.strptime(not_after, "%b %d %H:%M:%S %Y %Z").replace(
                tzinfo=timezone.utc
            )
            days_left = (expiry_dt - datetime.now(timezone.utc)).days
        except ValueError:
            pass

    return {
        "valid": True,
        # "Names derived from subject names": commonName + organizationName
        # (most facilitators use DV certs, so O is often absent).
        "subject_cn": subject.get("commonName"),
        "subject_o": subject.get("organizationName"),
        # "Locations derived from subject names": only present on OV/EV certs.
        "subject_locality": subject.get("localityName"),
        "subject_state": subject.get("stateOrProvinceName"),
        "subject_country": subject.get("countryName"),
        "issuer_o": issuer.get("organizationName"),
        "issuer_cn": issuer.get("commonName"),
        "san": san_list,
        "not_after": expiry_dt.isoformat() if expiry_dt else None,
        "days_until_expiry": days_left,
    }


# ── WHOIS / RDAP ──────────────────────────────────────────────────────────


def _parse_rdap_datetime(value: Optional[str]) -> Optional[datetime]:
    if not value:
        return None
    for fmt in ("%Y-%m-%dT%H:%M:%SZ", "%Y-%m-%dT%H:%M:%S%z", "%Y-%m-%dT%H:%M:%S.%fZ"):
        try:
            dt = datetime.strptime(value, fmt)
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
            return dt
        except ValueError:
            continue
    return None


def _rdap_base_url(tld: str) -> str:
    bootstrap = requests.get("https://data.iana.org/rdap/dns.json", timeout=10).json()
    for tlds, servers in bootstrap.get("services", []):
        if tld.lower() in [t.lower() for t in tlds]:
            return servers[0].rstrip("/")
    raise ValueError(f"No RDAP server found for .{tld}")


def rdap_lookup(domain: str, timeout: float = 15.0) -> dict[str, Any]:
    """WHOIS via RDAP. Returns registrar/registrant names, country, dates."""
    try:
        tld = domain.rsplit(".", 1)[-1]
        base = _rdap_base_url(tld)
        url = f"{base}/domain/{domain}"
        headers = {"Accept": "application/rdap+json, application/json"}
        r = requests.get(url, headers=headers, timeout=timeout, allow_redirects=True)
        r.raise_for_status()
        data = r.json()
    except Exception as exc:  # noqa: BLE001
        return {"error": str(exc)}

    creation = expiration = updated = None
    for event in data.get("events", []):
        action = event.get("eventAction", "")
        dt = _parse_rdap_datetime(event.get("eventDate"))
        if action == "registration":
            creation = dt
        elif action == "expiration":
            expiration = dt
        elif action in ("last changed", "last update of RDAP database"):
            updated = dt

    registrar_name = None
    registrant_name = None
    registrant_country = None
    for entity in data.get("entities", []):
        roles = entity.get("roles", [])
        vcard = entity.get("vcardArray", [None, []])[1]
        if "registrar" in roles and not registrar_name:
            for field in vcard:
                if field[0] == "fn":
                    registrar_name = field[3]
        if "registrant" in roles:
            for field in vcard:
                if field[0] == "fn" and not registrant_name:
                    registrant_name = field[3]
                if field[0] == "adr" and not registrant_country:
                    adr = field[3]
                    if isinstance(adr, list) and len(adr) >= 7:
                        registrant_country = adr[6] or None

    now = datetime.now(timezone.utc)
    age_days = (now - creation).days if creation else None

    return {
        # "Names ... from whois"
        "registrar_name": registrar_name,
        "registrant_name": registrant_name,
        # "locations ... from whois"
        "registrant_country": registrant_country,
        "creation_date": creation.isoformat() if creation else None,
        "age_days": age_days,
        "status": data.get("status", []),
    }


# ── IP geolocation (batched) ────────────────────────────────────────────


def geolocate_batch(ips: list[str], timeout: float = 15.0) -> dict[str, dict[str, Any]]:
    """One batched ip-api.com lookup for up to 100 unique IPs at a time."""
    unique = sorted(set(ips))
    results: dict[str, dict[str, Any]] = {}
    fields = "status,message,query,country,regionName,city,isp,org,as,hosting"
    for i in range(0, len(unique), 100):
        chunk = unique[i : i + 100]
        try:
            r = requests.post(
                f"http://ip-api.com/batch?fields={fields}",
                json=chunk,
                timeout=timeout,
            )
            r.raise_for_status()
            for row in r.json():
                results[row.get("query")] = row
        except Exception as exc:  # noqa: BLE001
            for ip in chunk:
                results[ip] = {"status": "fail", "message": str(exc)}
    return results


# ── Orchestration ─────────────────────────────────────────────────────────


def analyse_host(hostname: str, timeout: float) -> dict[str, Any]:
    ip = resolve_ip(hostname)
    result: dict[str, Any] = {
        "hostname": hostname,
        "registrable_domain": registrable_domain(hostname),
        "ip": ip,
    }
    if ip:
        result["reverse_dns"] = reverse_dns(ip)
    result["ssl"] = check_ssl(hostname, timeout=timeout)
    return result


def analyse_facilitators(
    entries: dict[str, dict[str, Any]],
    timeout: float = 8.0,
    max_workers: int = 10,
) -> list[dict[str, Any]]:
    """Analyse every facilitator's URLs: IP/geo, SSL subject, and WHOIS.

    Returns a list of {id, name, hosts: [...], whois: {domain: {...}}}
    ordered to match `entries`' iteration order.
    """
    # 1. Resolve + SSL-probe every unique hostname in parallel.
    all_hosts: set[str] = set()
    for entry in entries.values():
        for _role, host in hostnames_for_facilitator(entry):
            all_hosts.add(host)

    host_results: dict[str, dict[str, Any]] = {}
    with ThreadPoolExecutor(max_workers=max_workers) as pool:
        futures = {pool.submit(analyse_host, host, timeout): host for host in all_hosts}
        for future in as_completed(futures):
            host = futures[future]
            try:
                host_results[host] = future.result()
            except Exception as exc:  # noqa: BLE001
                host_results[host] = {"hostname": host, "error": str(exc)}

    # 2. WHOIS, once per unique registrable domain.
    domains = sorted({r["registrable_domain"] for r in host_results.values() if r.get("registrable_domain")})
    whois_results: dict[str, dict[str, Any]] = {}
    with ThreadPoolExecutor(max_workers=max_workers) as pool:
        futures = {pool.submit(rdap_lookup, domain, timeout * 2): domain for domain in domains}
        for future in as_completed(futures):
            domain = futures[future]
            try:
                whois_results[domain] = future.result()
            except Exception as exc:  # noqa: BLE001
                whois_results[domain] = {"error": str(exc)}

    # 3. Geolocate every resolved IP in one batch.
    ips = [r["ip"] for r in host_results.values() if r.get("ip")]
    geo_by_ip = geolocate_batch(ips)
    for r in host_results.values():
        if r.get("ip"):
            r["geo"] = geo_by_ip.get(r["ip"])

    # 4. Assemble per-facilitator output.
    out: list[dict[str, Any]] = []
    for fid, entry in entries.items():
        facilitator_hosts = []
        for role, host in hostnames_for_facilitator(entry):
            if host not in host_results:
                continue
            hr = dict(host_results[host])
            hr["role"] = role
            facilitator_hosts.append(hr)
        facilitator_domains = sorted({h["registrable_domain"] for h in facilitator_hosts})
        out.append(
            {
                "id": fid,
                "name": entry["name"],
                "hosts": facilitator_hosts,
                "whois": {d: whois_results.get(d, {}) for d in facilitator_domains},
            }
        )
    return out


def analyse_single_domain(domain: str, timeout: float = 8.0) -> dict[str, Any]:
    """Same IP/geo + SSL subject + WHOIS analysis as analyse_facilitators,
    for one arbitrary domain rather than a registry entry."""
    host_result = analyse_host(domain, timeout=timeout)
    host_result["role"] = "domain"

    if host_result.get("ip"):
        geo_by_ip = geolocate_batch([host_result["ip"]])
        host_result["geo"] = geo_by_ip.get(host_result["ip"])

    whois_result = rdap_lookup(host_result["registrable_domain"], timeout=timeout * 2)

    return {
        "id": domain,
        "name": domain,
        "hosts": [host_result],
        "whois": {host_result["registrable_domain"]: whois_result},
    }
