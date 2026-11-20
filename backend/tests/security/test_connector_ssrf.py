"""PostgreSQL connector SSRF guard (DATA_ENGINEERING_DESIGN §2 rule 1, SECURITY_ARCHITECTURE §8; P3 AC2).

A data source whose host is (or resolves to) loopback, RFC 1918, link-local (incl. 169.254.169.254), CGNAT,
ULA, multicast or unspecified addresses must be refused with `connector_host_forbidden` — at save time (4xx) or
by `POST /data-sources/{id}/test` (failed job) — and NO connection may be attempted to it. Resolution happens
once and the connection is pinned to the resolved IP (`hostaddr`), which defeats DNS rebinding.

Name resolution is faked (socket.getaddrinfo) and every psycopg connection to the probe database is captured
and refused, so nothing ever leaves the process. Skips while the data-sources API is not merged.
"""

from __future__ import annotations

import ipaddress
import socket

import pytest

from testing.client import api_client_for, requires_tenant_route, route_exists
from testing.flows import job_outcome
from testing.network import ConnectSpy, Resolver

PROBE_DB = ConnectSpy.PROBE_DB
FORBIDDEN_CODE = "connector_host_forbidden"
needs_sources = requires_tenant_route("data-sources", "Data Engineering")


def _forbidden(ip: str) -> bool:
    addr = ipaddress.ip_address(ip.split("%")[0])
    if isinstance(addr, ipaddress.IPv6Address) and addr.ipv4_mapped:
        addr = addr.ipv4_mapped
    return not addr.is_global or addr.is_multicast


@pytest.fixture
def sources(two_orgs, settings, monkeypatch, fake_redis, django_capture_on_commit_callbacks):
    a = two_orgs.a
    base = f"/api/v1/orgs/{a.slug}/data-sources"
    if not route_exists(base):
        pytest.skip("POST /orgs/<org>/data-sources not implemented yet (Data Engineering, wave 2)")
    settings.ALLOW_PRIVATE_DB_HOSTS = (
        False  # test settings enable it for the local warehouse; prod default off
    )
    client = api_client_for(a.user("data_engineer"))
    resolver = Resolver().install(monkeypatch)
    spy = ConnectSpy().install(monkeypatch)

    def attempt(host: str) -> tuple[int, str | None]:
        """Create + test a source for `host`. Returns (status_of_create, refusal code or None)."""
        body = {
            "name": f"probe {host}",
            "type": "postgresql",
            "config": {
                "host": host,
                "port": 5999,
                "dbname": PROBE_DB,
                "sslmode": "require",
                "schema": "public",
            },
            "username": "nexora_ro",
            "password": "synthetic-not-a-secret",
        }
        created = client.post(base, body, format="json")
        if created.status_code >= 400:
            assert created.status_code < 500, created.content[:300]
            return created.status_code, created.json().get("code")
        assert created.status_code in (200, 201), created.content[:300]
        source_id = created.json()["id"]
        with django_capture_on_commit_callbacks(execute=True):
            tested = client.post(f"{base}/{source_id}/test", {}, format="json")
        if tested.status_code >= 400:
            assert tested.status_code < 500, tested.content[:300]
            return tested.status_code, tested.json().get("code")
        assert tested.status_code == 202, tested.content[:300]
        job = job_outcome(client, a.slug, tested.json()["job_id"])
        if job["status"] == "failed":
            return 202, (job.get("error") or {}).get("code")
        return 202, None

    attempt.resolver = resolver
    attempt.spy = spy
    return attempt


LITERAL_HOSTS = [
    "127.0.0.1",
    "127.1.2.3",
    "localhost",
    "0.0.0.0",
    "10.0.0.5",
    "172.16.4.2",
    "192.168.1.10",
    "169.254.169.254",
    "100.64.0.1",
    "224.0.0.1",
    "::1",
    "::",
    "fe80::1",
    "fc00::1",
    "fd12:3456::1",
    "::ffff:127.0.0.1",
    "::ffff:10.0.0.1",
]
# Legacy/obfuscated IPv4 spellings that libc resolvers turn into 127.0.0.1. Refusing them as an invalid host is
# as good as `connector_host_forbidden`; connecting is not.
OBFUSCATED_HOSTS = ["2130706433", "0x7f000001", "0177.0.0.1", "127.1", "[::1]", "fe80::1%25eth0"]
INVALID_HOST_CODES = {FORBIDDEN_CODE, "validation_error", "invalid_host", "connector_invalid_config"}


@needs_sources
@pytest.mark.django_db
@pytest.mark.ac("P3-AC2")
@pytest.mark.parametrize("host", LITERAL_HOSTS)
def test_private_and_special_hosts_are_refused(sources, host):
    status, code = sources(host)
    assert code == FORBIDDEN_CODE, f"{host}: status {status}, code {code}"
    assert sources.spy.attempts == [], f"connection attempted for {host}: {sources.spy.attempts}"


@needs_sources
@pytest.mark.django_db
@pytest.mark.ac("P3-AC2")
@pytest.mark.parametrize("host", OBFUSCATED_HOSTS)
def test_obfuscated_loopback_spellings_are_refused(sources, host):
    status, code = sources(host)
    assert code in INVALID_HOST_CODES, f"{host}: status {status}, code {code}"
    assert sources.spy.attempts == [], f"connection attempted for {host}: {sources.spy.attempts}"


@needs_sources
@pytest.mark.django_db
@pytest.mark.ac("P3-AC2")
@pytest.mark.parametrize(
    ("name", "ip"),
    [
        ("db.internal.example.test", "10.20.30.40"),
        ("meta.example.test", "169.254.169.254"),
        ("v6.example.test", "fd00::5"),
        ("loop.example.test", "127.0.0.1"),
    ],
)
def test_names_resolving_to_private_addresses_are_refused(sources, name, ip):
    sources.resolver.answers[name] = [ip]
    status, code = sources(name)
    assert code == FORBIDDEN_CODE, f"{name}->{ip}: status {status}, code {code}"
    assert sources.spy.attempts == []


@needs_sources
@pytest.mark.django_db
@pytest.mark.ac("P3-AC2")
def test_dns_rebinding_is_defeated_by_ip_pinning(sources):
    """First answer public, every later answer loopback: the connection must use the FIRST (checked) IP."""
    name = "rebind.example.test"
    sources.resolver.answers[name] = ["8.8.8.8", "127.0.0.1"]
    _status, code = sources(name)
    assert sources.resolver.lookups, "the host was never resolved by the SSRF guard"
    for attempt in sources.spy.attempts:
        hostaddr = attempt.get("hostaddr")
        assert hostaddr, f"connection not pinned to the resolved IP (no hostaddr): {attempt}"
        assert not _forbidden(hostaddr), f"rebinding reached {hostaddr}: {attempt}"
        assert attempt.get("host") == name, "keep host=<name> for TLS SNI/verification next to hostaddr"
    if code == FORBIDDEN_CODE:  # also acceptable: refusing because a later lookup disagreed
        assert all(not _forbidden(t) for t in sources.spy.targets())


def test_ssrf_helper_classification():
    for ip in (
        "127.0.0.1",
        "10.0.0.1",
        "169.254.169.254",
        "100.64.0.1",
        "fe80::1",
        "fc00::1",
        "::ffff:10.0.0.1",
        "224.0.0.1",
        "0.0.0.0",
    ):
        assert _forbidden(ip), ip
    for ip in ("8.8.8.8", "2001:4860:4860::8888"):
        assert not _forbidden(ip), ip


def test_connect_spy_and_resolver_self_test(monkeypatch):
    import psycopg

    spy = ConnectSpy().install(monkeypatch)
    resolver = Resolver({"x.example.test": ["8.8.8.8", "127.0.0.1"]}).install(monkeypatch)
    for call in (
        lambda: psycopg.connect(host="h", hostaddr="8.8.8.8", dbname=PROBE_DB, connect_timeout=1),
        lambda: psycopg.Connection.connect(f"host=h dbname={PROBE_DB}"),
    ):
        with pytest.raises(psycopg.OperationalError, match="QA connect spy"):
            call()
    assert spy.targets() == ["8.8.8.8", "h"]
    assert socket.getaddrinfo("x.example.test", 5432)[0][4][0] == "8.8.8.8"
    assert socket.getaddrinfo("x.example.test", 5432)[0][4][0] == "127.0.0.1"
    assert resolver.lookups == ["x.example.test", "x.example.test"]
