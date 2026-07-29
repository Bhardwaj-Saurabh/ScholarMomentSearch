"""Component 24 (DESIGN.md §3e) — SSRF guard on document fetch.

The hole: `doc_pipeline._download` was a bare `urllib.request.urlopen(uri)`.
No IP/host restrictions, redirects followed silently, no size cap, and
`Content-Type` read only to GUESS a file extension rather than to reject
non-documents. Because the fetched bytes are parsed, embedded under the
caller's tenant and served back through `/api/ask`, an SSRF here is also an
exfiltration channel — the attacker reads the response.

On Fly this reaches the private 6PN mesh (`clip.process...internal:8001`),
`redis:6379`, and the cloud metadata endpoint. Hence Phase 0: closed before
the first deploy, not after.

No test here touches the network: DNS is stubbed via `socket.getaddrinfo` and
HTTP via `urlguard._urlopen_no_redirect`.
"""
from __future__ import annotations

import io
import socket

import pytest

from src.ingest import urlguard


def _stub_dns(monkeypatch, ip: str):
    """Point every hostname lookup at one IP."""
    family = socket.AF_INET6 if ":" in ip else socket.AF_INET
    monkeypatch.setattr(socket, "getaddrinfo",
                        lambda *a, **k: [(family, socket.SOCK_STREAM, 6, "", (ip, 443))])


# ── Scheme handling ──────────────────────────────────────────────────────────

@pytest.mark.parametrize("url", [
    "ftp://example.com/x.pdf",
    "file:///etc/passwd",
    "gopher://example.com/x",
    "data:application/pdf;base64,AAAA",
    "//example.com/x.pdf",     # scheme-relative, no scheme at all
])
def test_rejects_non_http_schemes(url):
    with pytest.raises(urlguard.BlockedUrlError):
        urlguard.validate_url(url)


def test_rejects_url_with_no_host():
    with pytest.raises(urlguard.BlockedUrlError):
        urlguard.validate_url("http:///nohost.pdf")


# ── Literal-IP blocking (no DNS involved) ────────────────────────────────────

@pytest.mark.parametrize("host", [
    "127.0.0.1",            # loopback
    "0.0.0.0",              # unspecified
    "10.0.0.5",             # RFC1918
    "172.16.3.4",           # RFC1918
    "192.168.1.1",          # RFC1918
    "169.254.169.254",      # cloud metadata / link-local -- the classic
    "100.64.0.1",           # CGNAT shared address space
    "[::1]",                # IPv6 loopback
    "[fd00::1]",            # IPv6 unique-local (Fly's 6PN mesh lives here)
    "[fe80::1]",            # IPv6 link-local
])
def test_rejects_private_and_special_literal_ips(host):
    with pytest.raises(urlguard.BlockedUrlError):
        urlguard.validate_url(f"http://{host}/x.pdf")


def test_allows_public_literal_ip():
    urlguard.validate_url("https://93.184.216.34/x.pdf")  # must not raise


# ── DNS-resolved blocking (the internal-hostname case) ───────────────────────

@pytest.mark.parametrize("host", ["redis", "clip.process.momentsearch.internal",
                                  "localhost", "evil.example.com"])
def test_rejects_hostname_resolving_to_private_ip(monkeypatch, host):
    _stub_dns(monkeypatch, "10.0.1.7")
    with pytest.raises(urlguard.BlockedUrlError):
        urlguard.validate_url(f"http://{host}/x.pdf")


def test_rejects_hostname_resolving_to_metadata_ip(monkeypatch):
    _stub_dns(monkeypatch, "169.254.169.254")
    with pytest.raises(urlguard.BlockedUrlError):
        urlguard.validate_url("http://totally-innocent.example.com/x.pdf")


def test_allows_hostname_resolving_to_public_ip(monkeypatch):
    _stub_dns(monkeypatch, "151.101.1.140")
    urlguard.validate_url("https://arxiv.org/pdf/1706.03762")  # must not raise


def test_rejects_when_any_resolved_ip_is_private(monkeypatch):
    """A hostname with several A records is only as safe as its worst one —
    round-robin DNS must not let an internal address through on some attempts."""
    monkeypatch.setattr(socket, "getaddrinfo", lambda *a, **k: [
        (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("151.101.1.140", 443)),
        (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("10.0.0.9", 443)),
    ])
    with pytest.raises(urlguard.BlockedUrlError):
        urlguard.validate_url("https://mixed.example.com/x.pdf")


def test_rejects_unresolvable_host(monkeypatch):
    def _boom(*a, **k):
        raise socket.gaierror("no such host")
    monkeypatch.setattr(socket, "getaddrinfo", _boom)
    with pytest.raises(urlguard.BlockedUrlError):
        urlguard.validate_url("https://nope.invalid/x.pdf")


# ── Redirect hops must be re-validated ───────────────────────────────────────

class _FakeResp:
    def __init__(self, body=b"%PDF-1.4 ok", status=200, headers=None):
        self._buf = io.BytesIO(body)
        self.status = status
        self.closed = False
        # `is None`, not `or` -- an explicitly-empty headers dict is a real
        # test case (a server that sends no Content-Type at all) and must not
        # silently fall back to the default.
        self._headers = {"Content-Type": "application/pdf"} if headers is None else headers

    def close(self):
        self.closed = True

    def getheader(self, name, default=None):
        return self._headers.get(name, default)

    def read(self, n=-1):
        return self._buf.read(n)

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


def test_redirect_into_internal_space_is_rejected(monkeypatch, tmp_path):
    """The single most important case: a public, allowed host 302s to an
    internal address. Validating only the ORIGINAL url would sail right past
    this, which is why redirects are followed manually and re-validated."""
    _stub_dns(monkeypatch, "151.101.1.140")
    hops = []

    def _fake_open(url, timeout):
        hops.append(url)
        if len(hops) == 1:
            return _FakeResp(status=302, headers={"Location": "http://169.254.169.254/latest/meta-data/"})
        raise AssertionError(f"followed the redirect to {url} — SSRF not blocked")

    monkeypatch.setattr(urlguard, "_urlopen_no_redirect", _fake_open)
    with pytest.raises(urlguard.BlockedUrlError):
        urlguard.download_to("https://public.example.com/paper.pdf", tmp_path / "out.pdf")


def test_redirect_to_public_host_is_allowed(monkeypatch, tmp_path):
    _stub_dns(monkeypatch, "151.101.1.140")
    hops = []

    def _fake_open(url, timeout):
        hops.append(url)
        if len(hops) == 1:
            return _FakeResp(status=301, headers={"Location": "https://cdn.example.com/paper.pdf"})
        return _FakeResp()

    monkeypatch.setattr(urlguard, "_urlopen_no_redirect", _fake_open)
    got = urlguard.download_to("https://public.example.com/paper.pdf", tmp_path / "out.pdf")
    assert got.path.read_bytes() == b"%PDF-1.4 ok"


def test_redirect_loop_is_bounded(monkeypatch, tmp_path):
    _stub_dns(monkeypatch, "151.101.1.140")
    monkeypatch.setattr(urlguard, "_urlopen_no_redirect", lambda url, timeout: _FakeResp(
        status=302, headers={"Location": "https://public.example.com/again.pdf"}))
    with pytest.raises(urlguard.BlockedUrlError):
        urlguard.download_to("https://public.example.com/paper.pdf", tmp_path / "out.pdf")


# ── Size cap + content-type enforcement ──────────────────────────────────────

def test_oversized_body_is_rejected_while_streaming(monkeypatch, tmp_path):
    """The cap must bite DURING the read loop, not after — the old code read
    to EOF with no limit, so an endless response would fill the worker disk."""
    _stub_dns(monkeypatch, "151.101.1.140")
    monkeypatch.setattr(urlguard, "_urlopen_no_redirect",
                        lambda url, timeout: _FakeResp(body=b"x" * 5000))
    with pytest.raises(urlguard.BlockedUrlError):
        urlguard.download_to("https://public.example.com/big.pdf", tmp_path / "out.pdf",
                             max_bytes=1000)


def test_oversized_body_leaves_no_partial_file(monkeypatch, tmp_path):
    _stub_dns(monkeypatch, "151.101.1.140")
    monkeypatch.setattr(urlguard, "_urlopen_no_redirect",
                        lambda url, timeout: _FakeResp(body=b"x" * 5000))
    dest = tmp_path / "out.pdf"
    with pytest.raises(urlguard.BlockedUrlError):
        urlguard.download_to("https://public.example.com/big.pdf", dest, max_bytes=1000)
    assert not dest.exists(), "a rejected oversized download must not leave a partial file"


@pytest.mark.parametrize("ctype", ["text/html", "application/json", "image/png",
                                   "text/plain"])
def test_rejects_non_document_content_type(monkeypatch, tmp_path, ctype):
    """Previously Content-Type was only used to guess an extension, so an SSRF
    response body (HTML, JSON credentials) was happily parsed and embedded."""
    _stub_dns(monkeypatch, "151.101.1.140")
    monkeypatch.setattr(urlguard, "_urlopen_no_redirect",
                        lambda url, timeout: _FakeResp(headers={"Content-Type": ctype}))
    with pytest.raises(urlguard.BlockedUrlError):
        urlguard.download_to("https://public.example.com/x.pdf", tmp_path / "out.pdf")


@pytest.mark.parametrize("ctype", [
    "application/pdf",
    "application/pdf; charset=binary",
    "application/vnd.openxmlformats-officedocument.presentationml.presentation",
    "application/octet-stream",   # many academic hosts serve PDFs as this
])
def test_allows_document_content_types(monkeypatch, tmp_path, ctype):
    _stub_dns(monkeypatch, "151.101.1.140")
    monkeypatch.setattr(urlguard, "_urlopen_no_redirect",
                        lambda url, timeout: _FakeResp(headers={"Content-Type": ctype}))
    got = urlguard.download_to("https://public.example.com/x.pdf", tmp_path / "out.pdf")
    assert got.path.exists()


def test_missing_content_type_is_allowed(monkeypatch, tmp_path):
    """Some static hosts omit it entirely; the parser is the real arbiter of
    whether the bytes are a usable document, so don't hard-fail here."""
    _stub_dns(monkeypatch, "151.101.1.140")
    monkeypatch.setattr(urlguard, "_urlopen_no_redirect",
                        lambda url, timeout: _FakeResp(headers={}))
    got = urlguard.download_to("https://public.example.com/x.pdf", tmp_path / "out.pdf")
    assert got.path.exists()
    assert got.content_type is None


def test_closes_every_response_including_redirect_hops(monkeypatch, tmp_path):
    """This runs inside a long-lived Prefect worker, so an unclosed response
    per redirect hop leaks sockets across runs (spec-guardian finding)."""
    _stub_dns(monkeypatch, "151.101.1.140")
    opened: list[_FakeResp] = []

    def _fake_open(url, timeout):
        resp = (_FakeResp(status=302, headers={"Location": "https://cdn.example.com/p.pdf"})
                if not opened else _FakeResp())
        opened.append(resp)
        return resp

    monkeypatch.setattr(urlguard, "_urlopen_no_redirect", _fake_open)
    urlguard.download_to("https://public.example.com/p.pdf", tmp_path / "out.pdf")
    assert len(opened) == 2
    assert all(r.closed for r in opened), "a redirect hop's response was left open"


def test_closes_response_on_rejection(monkeypatch, tmp_path):
    _stub_dns(monkeypatch, "151.101.1.140")
    resp = _FakeResp(headers={"Content-Type": "text/html"})
    monkeypatch.setattr(urlguard, "_urlopen_no_redirect", lambda url, timeout: resp)
    with pytest.raises(urlguard.BlockedUrlError):
        urlguard.download_to("https://public.example.com/x.pdf", tmp_path / "out.pdf")
    assert resp.closed


def test_returns_content_type_for_extension_selection(monkeypatch, tmp_path):
    """The caller needs this back: deck.parse_deck dispatches on file suffix
    and rejects anything but .pdf/.pptx, so a suffix-less URL serving a PPTX
    can only be named correctly from the Content-Type."""
    _stub_dns(monkeypatch, "151.101.1.140")
    pptx = "application/vnd.openxmlformats-officedocument.presentationml.presentation"
    monkeypatch.setattr(urlguard, "_urlopen_no_redirect",
                        lambda url, timeout: _FakeResp(headers={"Content-Type": pptx}))
    got = urlguard.download_to("https://public.example.com/talk", tmp_path / "out.bin")
    assert got.content_type == pptx
