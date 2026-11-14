import asyncio

import pytest
from app.ssrf import UnsafeUrl, check_url, is_public_ip
from app.worker import RETRY_SCHEDULE, next_delay


def resolver(mapping):
    async def r(host, port):
        return mapping[host]
    return r


def check(url, **kw):
    kw.setdefault("resolver", resolver({"good.example": ["93.184.216.34"], "evil.example": ["10.0.0.5"],
                                        "mixed.example": ["93.184.216.34", "127.0.0.1"], "partner-sim": ["172.18.0.9"]}))
    return asyncio.run(check_url(url, **kw))


@pytest.mark.parametrize("ip", ["127.0.0.1", "10.1.2.3", "172.16.0.1", "192.168.1.1", "169.254.169.254", "0.0.0.0",
                                "::1", "fe80::1", "fc00::1", "::ffff:10.0.0.1", "224.0.0.1", "100.64.0.1", "not-an-ip"])
def test_non_public_addresses_are_rejected(ip):
    assert not is_public_ip(ip)


def test_public_address_is_accepted():
    assert is_public_ip("93.184.216.34") and is_public_ip("2606:2800:220:1:248:1893:25c8:1946")


@pytest.mark.parametrize("url", [
    "http://good.example/hook",            # http is not allowed
    "https://evil.example/hook",           # resolves to a private address
    "https://mixed.example/hook",          # one of several addresses is loopback
    "https://127.0.0.1/hook", "https://169.254.169.254/latest/meta-data", "https://[::1]/hook",
    "https://user:pw@good.example/hook",   # credentials
    "ftp://good.example/x", "file:///etc/passwd", "gopher://good.example", "javascript:alert(1)",
    "https:///nohost", "https://good.example/a b", "https://good.example/\r\nX: y", "",
    "http://localhost:5432", "https://partner-sim:8004/v1/hooks/x",  # internal name, not on the allow-list
])
def test_unsafe_urls_are_refused(url):
    with pytest.raises(UnsafeUrl):
        check(url)


def test_public_https_is_accepted_and_fragment_dropped():
    assert check("https://good.example/hook#frag") == "https://good.example/hook"


def test_dev_allow_list_is_exact_host_and_port():
    allow = frozenset({"partner-sim:8004"})
    assert check("http://partner-sim:8004/v1/hooks/x", allowed_hosts=allow, allow_http=True)
    with pytest.raises(UnsafeUrl):  # same host, other port (e.g. an admin port)
        check("http://partner-sim:5432/", allowed_hosts=allow, allow_http=True)
    with pytest.raises(UnsafeUrl):  # allow-listed but http not enabled
        check("http://partner-sim:8004/x", allowed_hosts=allow, allow_http=False)


def test_retry_schedule():
    assert [next_delay(n) for n in range(1, 6)] == list(RETRY_SCHEDULE)
    assert next_delay(6) is None and next_delay(0) is None
