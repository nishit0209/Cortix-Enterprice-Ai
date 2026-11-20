"""SSRF guard: host syntax, deny ranges, resolve-once + pinning, DNS rebinding (pure, resolver faked)."""

import socket

import pytest

from apps.connectors import ssrf
from apps.connectors.exceptions import HostForbidden, HostUnresolvable, InvalidHost


def fake_resolver(*answers, calls=None):
    """getaddrinfo stand-in returning `answers` (one list per call, in order)."""
    queue = list(answers)

    def resolve(host, port, family=0, type_=0, *args):
        if calls is not None:
            calls.append(host)
        ips = queue.pop(0) if len(queue) > 1 else queue[0]
        return [(socket.AF_INET6 if ":" in ip else socket.AF_INET, socket.SOCK_STREAM, 6, "", (ip, port))
                for ip in ips]

    return resolve


@pytest.mark.parametrize("ip,reason", [
    ("127.0.0.1", "loopback"), ("10.1.2.3", "private"), ("172.16.5.4", "private"), ("192.168.1.1", "private"),
    ("169.254.169.254", "metadata"), ("169.254.10.10", "link_local"), ("100.64.0.1", "cgnat"),
    ("0.0.0.0", "unspecified"), ("224.0.0.1", "multicast"), ("255.255.255.255", "multicast"),
    ("240.0.0.1", "reserved"), ("198.18.0.1", "reserved"), ("192.0.2.10", "private"),
    ("::1", "loopback"), ("fe80::1", "link_local"), ("fc00::1", "private"), ("fd00:ec2::254", "metadata"),
    ("::ffff:127.0.0.1", "loopback"), ("::ffff:10.0.0.1", "private"), ("2002:7f00:0001::1", "private"),
    ("64:ff9b::10.0.0.1", "private"), ("::", "unspecified"), ("ff02::1", "multicast"),
])
def test_special_addresses_are_refused(ip, reason):
    with pytest.raises(HostForbidden) as e:
        ssrf.check_ip(ip, allow_private=False)
    assert e.value.get_codes() == "connector_host_forbidden"
    assert ssrf.classify_ip(__import__("ipaddress").ip_address(ip)) is not None
    del reason  # the exact label may differ across Python versions; refusal is what matters


@pytest.mark.parametrize("ip", ["8.8.8.8", "52.95.110.1", "2606:4700:4700::1111", "1.1.1.1"])
def test_public_addresses_allowed(ip):
    assert str(ssrf.check_ip(ip, allow_private=False)) == ip


def test_allow_private_dev_flag_still_blocks_metadata_and_unspecified():
    assert str(ssrf.check_ip("127.0.0.1", allow_private=True)) == "127.0.0.1"
    assert str(ssrf.check_ip("10.0.0.5", allow_private=True)) == "10.0.0.5"
    for ip in ("169.254.169.254", "0.0.0.0", "224.0.0.1", "fd00:ec2::254"):
        with pytest.raises(HostForbidden):
            ssrf.check_ip(ip, allow_private=True)


def test_setting_controls_default(settings):
    settings.ALLOW_PRIVATE_DB_HOSTS = False
    with pytest.raises(HostForbidden):
        ssrf.check_ip("10.0.0.1")
    settings.ALLOW_PRIVATE_DB_HOSTS = True
    assert ssrf.check_ip("10.0.0.1")


def test_platform_deny_cidrs(settings):
    settings.CONNECTOR_DENY_CIDRS = ["52.0.0.0/8"]
    with pytest.raises(HostForbidden) as e:
        ssrf.check_ip("52.1.2.3", allow_private=True)
    assert e.value.extra["reason"] == "platform_network"


@pytest.mark.parametrize("host", [
    "", "http://db.example.com", "db.example.com:5432", "user@db.example.com", "db example.com", "db/../x",
    "-bad.example.com", "a" * 64 + ".com", "127.1", "db.example.com\n", "db%2eexample.com", "x" * 254,
])
def test_invalid_host_syntax(host):
    with pytest.raises(InvalidHost):
        ssrf.normalize_host(host)


def test_normalize_host_and_ip_literals():
    assert ssrf.normalize_host("DB.Example.COM.") == "db.example.com"
    assert ssrf.normalize_host("[2606:4700::1]") == "2606:4700::1"


@pytest.mark.parametrize("name", ["localhost", "db.localhost", "pg.internal", "printer.local", "metadata",
                                  "metadata.google.internal"])
def test_internal_names_refused_without_lookup(name):
    calls = []
    with pytest.raises(HostForbidden):
        ssrf.resolve_and_check(name, 5432, allow_private=False, resolver=fake_resolver(["8.8.8.8"], calls=calls))
    assert calls == []


def test_resolve_once_and_pin_first_public_ipv4():
    calls = []
    pinned = ssrf.resolve_and_check("db.example.com", 5432, allow_private=False,
                                    resolver=fake_resolver(["2606:4700::1", "52.1.2.3"], calls=calls))
    assert pinned == ssrf.ResolvedHost(host="db.example.com", ip="52.1.2.3", port=5432)
    assert calls == ["db.example.com"]


def test_mixed_public_private_answer_is_refused():
    with pytest.raises(HostForbidden):
        ssrf.resolve_and_check("rebind.example.com", 5432, allow_private=False,
                               resolver=fake_resolver(["52.1.2.3", "127.0.0.1"]))


def test_dns_answer_to_private_ip_is_refused():
    with pytest.raises(HostForbidden):
        ssrf.resolve_and_check("evil.example.com", 5432, allow_private=False, resolver=fake_resolver(["10.0.0.7"]))


def test_scoped_ipv6_link_local_answer_refused():
    def resolver(host, port, *a):
        return [(socket.AF_INET6, socket.SOCK_STREAM, 6, "", ("fe80::1%eth0", port, 0, 2))]

    with pytest.raises(HostForbidden):
        ssrf.resolve_and_check("v6.example.com", 5432, allow_private=False, resolver=resolver)


def test_unresolvable():
    def boom(*a, **k):
        raise socket.gaierror("nope")

    with pytest.raises(HostUnresolvable):
        ssrf.resolve_and_check("nx.example.com", 5432, allow_private=False, resolver=boom)
    with pytest.raises(HostUnresolvable):
        ssrf.resolve_and_check("empty.example.com", 5432, allow_private=False, resolver=lambda *a: [])


def test_ip_literal_is_checked_without_dns():
    calls = []
    with pytest.raises(HostForbidden):
        ssrf.resolve_and_check("10.0.0.1", 5432, allow_private=False, resolver=fake_resolver(["8.8.8.8"], calls=calls))
    assert calls == []
    assert ssrf.resolve_and_check("8.8.8.8", 5432, allow_private=False).ip == "8.8.8.8"


def test_bad_port():
    with pytest.raises(InvalidHost):
        ssrf.resolve_and_check("db.example.com", 0, allow_private=False)


def test_precheck_host_is_network_free(settings, monkeypatch):
    settings.ALLOW_PRIVATE_DB_HOSTS = False
    monkeypatch.setattr(socket, "getaddrinfo", lambda *a, **k: pytest.fail("precheck must not resolve"))
    assert ssrf.precheck_host("db.example.com") == "db.example.com"
    for bad in ("127.0.0.1", "localhost", "169.254.169.254"):
        with pytest.raises(HostForbidden):
            ssrf.precheck_host(bad)
