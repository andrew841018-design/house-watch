"""R2-10：憑證驗證永遠開著；只有設定列出的主機清掉 VERIFY_X509_STRICT 這一個旗標。"""

import re
import ssl
from pathlib import Path

import httpx

from house_watch import http as http_module
from house_watch.http import SourcePolicy, make_ssl_context
from house_watch.settings import TlsConfig
from house_watch.sources.base import RequestSpec

SRC = Path(__file__).resolve().parents[2] / "src" / "house_watch"
BYPASS = re.compile(
    r"verify\s*=\s*False|CERT_NONE|check_hostname\s*=\s*False|_create_unverified_context"
)


def test_default_context_verifies_strictly():
    context = make_ssl_context()
    assert context.verify_mode == ssl.CERT_REQUIRED
    assert context.check_hostname
    assert context.verify_flags & ssl.VERIFY_X509_STRICT


def test_relaxed_context_only_drops_the_strict_flag():
    strict = make_ssl_context()
    relaxed = make_ssl_context(relax_strict=True)
    assert relaxed.verify_mode == ssl.CERT_REQUIRED
    assert relaxed.check_hostname
    assert not relaxed.verify_flags & ssl.VERIFY_X509_STRICT
    assert relaxed.verify_flags == strict.verify_flags & ~ssl.VERIFY_X509_STRICT


def test_relaxation_applies_only_to_listed_hosts(make_fetcher, monkeypatch):
    calls: list[bool] = []
    real = http_module.make_ssl_context

    def recording(*, relax_strict: bool = False):
        calls.append(relax_strict)
        return real(relax_strict=relax_strict)

    monkeypatch.setattr(http_module, "make_ssl_context", recording)
    hosts = frozenset({"a.example", "gov.example"})
    fetcher = make_fetcher(
        lambda request: httpx.Response(200),
        policies={"x": SourcePolicy(hosts)},
        tls=TlsConfig(relax_strict_hosts=["gov.example"]),
    )
    fetcher.fetch(RequestSpec.get("https://a.example/"), source="x", deadline=1e9)
    fetcher.fetch(RequestSpec.get("https://gov.example/"), source="x", deadline=1e9)
    assert calls == [False, True]


def test_no_code_turns_off_certificate_verification():
    offenders = []
    for path in sorted(SRC.rglob("*.py")):
        for number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
            if BYPASS.search(line):
                offenders.append(f"{path.relative_to(SRC)}:{number}")
    assert offenders == []
