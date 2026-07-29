"""SSRF-safe URL fetching for user-supplied document URIs — DESIGN.md §3e
component 24.

`POST /admin/documents` lets a caller name an arbitrary http(s) URL, which a
worker then downloads. Before this module that was a bare
`urllib.request.urlopen(uri)`: any host, any IP, redirects followed silently,
no size cap, and `Content-Type` consulted only to guess a file extension.

That is worse here than a generic SSRF, because the fetched bytes don't just
get discarded — they are parsed, embedded into the caller's tenant, and served
back through `/api/ask`. The attacker reads the response, so the SSRF doubles
as an exfiltration channel. On Fly the reachable targets include the private
6PN mesh (`clip.process.momentsearch.internal:8001`, `fd00::/8`), `redis:6379`,
and the `169.254.169.254` metadata endpoint.

Defences here, in order of what they stop:
  - scheme allowlist (http/https only — no file://, gopher://, data:)
  - every resolved IP checked against private/loopback/link-local/CGNAT/
    reserved/multicast ranges, for literal-IP hosts AND DNS names
  - redirects followed MANUALLY, re-validating each hop, because an allowed
    public host is free to 302 into internal space
  - size cap enforced DURING streaming, with the partial file removed
  - content-type allowlist actually enforced

KNOWN RESIDUAL LIMITATIONS, disclosed rather than papered over:

  1. DNS rebinding. This validates the IPs a hostname resolves to, then hands
     the URL to urllib, which resolves it AGAIN. A DNS entry that changes
     between those two lookups would slip past. Closing it properly means
     pinning the connection to the already-validated IP while preserving TLS
     SNI/cert validation against the original hostname — a custom transport,
     materially more machinery than this component. Exposure is narrow: the
     attacker must control authoritative DNS with a near-zero TTL and win a
     race on the worker.
  2. No destination-port restriction. Any port on a publicly-routable address
     is allowed. Harmless alone, but it is the multiplier on (1): a successful
     rebind could reach `:6379` or `:8001` rather than only `:80`/`:443`.
     Restricting to http(s) ports would shrink that blast radius and is the
     cheapest follow-up if (1) is ever closed.
  3. Content-type is permissive by design — `application/octet-stream` and a
     MISSING header both pass, because academic hosts legitimately do both.
     The IP allowlist, not the content-type check, is the primary control
     here; the content-type check only rejects the affirmative
     "this is HTML/JSON/an image" case.
"""
from __future__ import annotations

import ipaddress
import socket
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urljoin, urlparse

_ALLOWED_SCHEMES = ("http", "https")
_MAX_REDIRECTS = 5
_DEFAULT_MAX_BYTES = 100 * 1024 * 1024      # 100 MB — generous for a paper/deck
_CHUNK = 1 << 16

# Deliberately permissive on octet-stream: plenty of academic hosts serve a
# real PDF that way. A missing Content-Type is also allowed — the parser is
# the real arbiter of whether the bytes are a usable document. What this
# blocks is the affirmative "this is HTML/JSON/an image" case, which is what
# an SSRF response body looks like.
_ALLOWED_CONTENT_TYPES = (
    "application/pdf",
    "application/x-pdf",
    "application/vnd.openxmlformats-officedocument.presentationml.presentation",
    "application/vnd.ms-powerpoint",
    "application/octet-stream",
    "binary/octet-stream",
)


class BlockedUrlError(ValueError):
    """A URL (or one of its redirect hops, or its response) was refused."""


@dataclass(frozen=True)
class Fetched:
    """The written file plus the server's Content-Type. The caller needs the
    latter to pick a file extension: `deck.parse_deck` dispatches on suffix
    and rejects anything but .pdf/.pptx, so a suffix-less URL serving a PPTX
    would break if we only ever looked at the URL path."""
    path: Path
    content_type: str | None


def _ip_is_blocked(ip: ipaddress.IPv4Address | ipaddress.IPv6Address) -> bool:
    if (ip.is_private or ip.is_loopback or ip.is_link_local or ip.is_reserved
            or ip.is_multicast or ip.is_unspecified):
        return True
    # RFC 6598 carrier-grade NAT — not covered by is_private, and routable to
    # infrastructure on some providers.
    if ip.version == 4 and ip in ipaddress.ip_network("100.64.0.0/10"):
        return True
    # IPv4-mapped IPv6 (::ffff:10.0.0.1) would otherwise dodge the v4 checks.
    if ip.version == 6:
        mapped = getattr(ip, "ipv4_mapped", None)
        if mapped is not None and _ip_is_blocked(mapped):
            return True
    return False


def _resolved_ips(host: str, port: int) -> list[str]:
    try:
        infos = socket.getaddrinfo(host, port, proto=socket.IPPROTO_TCP)
    except OSError as exc:
        raise BlockedUrlError(f"Cannot resolve host {host!r}: {exc}") from exc
    return [info[4][0] for info in infos]


def validate_url(url: str) -> None:
    """Raise BlockedUrlError unless `url` is http(s) and EVERY address it
    resolves to is publicly routable."""
    parsed = urlparse(url)
    if parsed.scheme not in _ALLOWED_SCHEMES:
        raise BlockedUrlError(
            f"Only {'/'.join(_ALLOWED_SCHEMES)} URLs are allowed, got {parsed.scheme or 'none'!r}.")
    host = parsed.hostname
    if not host:
        raise BlockedUrlError("URL has no host.")

    try:
        literal = ipaddress.ip_address(host)
    except ValueError:
        literal = None

    if literal is not None:
        candidates = [literal]
    else:
        port = parsed.port or (443 if parsed.scheme == "https" else 80)
        candidates = []
        for addr in _resolved_ips(host, port):
            try:
                candidates.append(ipaddress.ip_address(addr))
            except ValueError:
                raise BlockedUrlError(f"Unparseable address {addr!r} for host {host!r}.")
        if not candidates:
            raise BlockedUrlError(f"Host {host!r} resolved to nothing.")

    # ALL of them, not just the first: a round-robin record is only as safe as
    # its worst address.
    for ip in candidates:
        if _ip_is_blocked(ip):
            raise BlockedUrlError(
                f"Refusing to fetch {host!r}: resolves to non-public address {ip}.")


def _urlopen_no_redirect(url: str, timeout: float):
    """Open `url` WITHOUT following redirects, so each hop can be validated.
    Seam for tests to stub."""
    class _NoRedirect(urllib.request.HTTPRedirectHandler):
        def redirect_request(self, req, fp, code, msg, headers, newurl):
            return None      # urllib then returns the 3xx response as-is

    opener = urllib.request.build_opener(_NoRedirect)
    return opener.open(url, timeout=timeout)


def _content_type_ok(raw: str | None) -> bool:
    if not raw:
        return True          # absent -> let the parser decide
    return raw.split(";")[0].strip().lower() in _ALLOWED_CONTENT_TYPES


def download_to(url: str, dest: Path, *, timeout: float = 60,
                max_bytes: int = _DEFAULT_MAX_BYTES) -> Fetched:
    """Fetch `url` to `dest`, validating the URL and every redirect hop, and
    enforcing the content-type allowlist and size cap. Raises BlockedUrlError
    on any refusal, leaving no partial file behind."""
    current = url
    for _ in range(_MAX_REDIRECTS + 1):
        validate_url(current)
        resp = _urlopen_no_redirect(current, timeout)
        # Close every response, including the up-to-6 redirect hops — this runs
        # inside a long-lived Prefect worker, so leaving sockets to GC leaks
        # them across runs.
        try:
            status = getattr(resp, "status", None) or getattr(resp, "code", 200)
            if status in (301, 302, 303, 307, 308):
                location = resp.getheader("Location")
                if not location:
                    raise BlockedUrlError(f"Redirect from {current} with no Location header.")
                # Relative Location is legal; resolve against the current URL so
                # the next validate_url() sees a real absolute target. This also
                # normalizes a protocol-relative '//host/x' into a full URL, so
                # the next hop is genuinely re-validated rather than skipped.
                current = urljoin(current, location)
                continue

            content_type = resp.getheader("Content-Type")
            if not _content_type_ok(content_type):
                raise BlockedUrlError(
                    f"Refusing content-type {content_type!r} from {current} — not a document.")

            dest.parent.mkdir(parents=True, exist_ok=True)
            size = 0
            try:
                with dest.open("wb") as out:
                    while chunk := resp.read(_CHUNK):
                        size += len(chunk)
                        # Checked BEFORE the write, so what lands on disk can
                        # never exceed max_bytes even by one chunk.
                        if size > max_bytes:
                            raise BlockedUrlError(
                                f"Response from {current} exceeds the {max_bytes}-byte limit.")
                        out.write(chunk)
            except BaseException:
                dest.unlink(missing_ok=True)   # never leave a partial/oversized file
                raise
            return Fetched(path=dest, content_type=content_type)
        finally:
            close = getattr(resp, "close", None)
            if callable(close):
                close()

    raise BlockedUrlError(f"Too many redirects (>{_MAX_REDIRECTS}) starting at {url}.")
