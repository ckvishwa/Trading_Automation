"""The live smoke test must fail closed, and the engine must stay order-free.

These run offline and never open a socket. The point of the first group is that
an unauthenticated run reports BLOCKED with a non-zero exit code, so it can
never be mistaken for a verified live connection.
"""

from __future__ import annotations

import builtins
import json
import re
from pathlib import Path

import pytest

from app.config import PROJECT_ROOT, Settings, get_settings
from tools import upstox_live_smoke as smoke


@pytest.fixture(autouse=True)
def _no_ambient_credentials(monkeypatch):
    for name in ("UPSTOX_ACCESS_TOKEN", "UPSTOX_API_KEY", "FYERS_ACCESS_TOKEN"):
        monkeypatch.delenv(name, raising=False)
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


def run(capsys, argv=None, settings=None) -> tuple[int, dict]:
    if settings is not None:
        get_settings.cache_clear()
        smoke.get_settings = lambda: settings  # type: ignore[assignment]
    try:
        code = smoke.main(argv or [])
    finally:
        smoke.get_settings = get_settings  # type: ignore[assignment]
    return code, json.loads(capsys.readouterr().out)


def settings_with(**overrides) -> Settings:
    base = {
        "WEBHOOK_TOKEN": "w" * 40,
        "TRADING_MODE": "PAPER",
        "_env_file": None,
    }
    base.update(overrides)
    return Settings(**base)


# --- fail closed without credentials ------------------------------------------


def test_the_script_is_read_only():
    assert smoke.READ_ONLY is True


def test_no_credentials_reports_blocked_with_a_nonzero_exit(capsys):
    code, payload = run(capsys, settings=settings_with())
    assert code == smoke.EXIT_BLOCKED != 0
    assert payload["status"] == "LIVE BLOCKED"
    assert "UPSTOX_ACCESS_TOKEN" in payload["reason"]
    assert payload["verified_against_live_account"] is False
    assert payload["orders_placed"] == 0
    assert payload["read_only"] is True


def test_the_blocked_message_tells_the_owner_not_to_paste_the_token(capsys):
    _, payload = run(capsys, settings=settings_with())
    reason = payload["reason"].lower()
    assert "do not paste" in reason
    assert "chat" in reason and "log" in reason


def test_blocked_is_never_reported_as_verified(capsys):
    _, payload = run(capsys, settings=settings_with())
    assert payload["status"] != smoke.STATUS_VERIFIED
    assert "LIVE VERIFIED" not in json.dumps(payload)


def test_a_missing_websocket_dependency_reports_blocked(capsys, monkeypatch):
    real_import = builtins.__import__

    def fail_on_websockets(name, *args, **kwargs):
        if name == "websockets":
            raise ImportError("no websockets here")
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", fail_on_websockets)
    code, payload = run(capsys, settings=settings_with(UPSTOX_ACCESS_TOKEN="t" * 40))
    assert code == smoke.EXIT_BLOCKED
    assert "websockets" in payload["reason"]
    assert ".[live]" in payload["next_step"]


def test_an_unverified_symbol_is_blocked_rather_than_guessed(capsys):
    code, payload = run(
        capsys,
        argv=["--symbol", "NSE:NIFTYNEXT50"],
        settings=settings_with(UPSTOX_ACCESS_TOKEN="t" * 40),
    )
    assert code == smoke.EXIT_BLOCKED
    assert "no verified instrument key" in payload["reason"]
    assert "--instrument-key" in payload["reason"]


def test_the_authorize_endpoint_is_the_documented_one():
    assert smoke.AUTHORIZE_URL.startswith("https://api.upstox.com/v3/feed/market-data-feed")


def test_credentials_are_registered_as_log_secrets():
    configured = settings_with(
        UPSTOX_ACCESS_TOKEN="a" * 40,
        UPSTOX_API_KEY="b" * 40,
        FYERS_ACCESS_TOKEN="c" * 40,
    )
    assert set(configured.secrets) == {"w" * 40, "a" * 40, "b" * 40, "c" * 40}
    # Empty values are not registered: an empty secret would redact everything.
    assert "" not in settings_with().secrets


def test_an_authorize_failure_is_reported_as_failed_not_blocked(capsys, monkeypatch):
    import urllib.error

    def explode(token, timeout=30.0):
        raise urllib.error.URLError("connection refused")

    monkeypatch.setattr(smoke, "authorized_url", explode)
    monkeypatch.setattr(smoke, "websockets", __import__("json"), raising=False)
    code, payload = run(capsys, settings=settings_with(UPSTOX_ACCESS_TOKEN="t" * 40))
    # FAILED, not BLOCKED: credentials existed and the attempt was made.
    assert code == smoke.EXIT_FAILED
    assert payload["status"] == "LIVE FAILED"
    assert payload["stage"] == "authorize"
    assert payload["verified_against_live_account"] is False


def test_an_authorize_response_without_a_url_is_an_error(monkeypatch):
    from app.market_data.protocols import MarketDataError

    class FakeResponse:
        def read(self):
            return json.dumps({"status": "success", "data": {}}).encode()

        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

    monkeypatch.setattr(smoke.urllib.request, "urlopen", lambda *a, **k: FakeResponse())
    with pytest.raises(MarketDataError, match="no authorized redirect URI"):
        smoke.authorized_url("t" * 40)


# --- the engine must contain no order-placement code --------------------------

# Deliberately broad: anything that looks like an order endpoint or an order
# call. The guarantee is "there is none", so a near-miss should fail loudly and
# be renamed rather than be allowed through by a narrow pattern.
ORDER_PATTERNS = (
    r"\bplace_order\b",
    r"\bplaceOrder\b",
    r"\bmodify_order\b",
    r"\bcancel_order\b",
    r"/order/place",
    r"/v2/order",
    r"/v3/order",
    r"\border/multi\b",
    r"\bgtt\b",
)

SEARCH_ROOTS = ("app", "tools")
ALLOWED_MENTIONS = {
    # Prose that documents the absence, or the rate-limit table that records
    # Upstox's published order-placement limits without calling anything.
    "order_placement",
    "order placement",
    "no order",
    "order-placement",
}


def source_files() -> list[Path]:
    found: list[Path] = []
    for root in SEARCH_ROOTS:
        found.extend(sorted((PROJECT_ROOT / root).rglob("*.py")))
    return found


def test_the_search_actually_finds_source_files():
    # A guard that silently searches nothing proves nothing.
    files = source_files()
    assert len(files) > 20
    assert any(path.name == "tradingview_webhook.py" for path in files)


@pytest.mark.parametrize("pattern", ORDER_PATTERNS)
def test_no_order_placement_code_exists_anywhere(pattern):
    offenders = []
    compiled = re.compile(pattern, re.IGNORECASE)
    for path in source_files():
        if path.name == "test_live_readiness.py":
            continue
        for number, line in enumerate(
            path.read_text(encoding="utf-8").splitlines(), start=1
        ):
            if not compiled.search(line):
                continue
            stripped = line.strip()
            is_comment = stripped.startswith("#") or stripped.startswith('"')
            if is_comment and any(allowed in line.lower() for allowed in ALLOWED_MENTIONS):
                continue
            if any(allowed in line.lower() for allowed in ALLOWED_MENTIONS):
                continue
            offenders.append(f"{path.relative_to(PROJECT_ROOT)}:{number}: {stripped}")
    assert not offenders, "order-placement code found:\n" + "\n".join(offenders)


def test_the_live_smoke_test_imports_nothing_that_could_trade():
    text = (PROJECT_ROOT / "tools" / "upstox_live_smoke.py").read_text("utf-8")
    assert "READ_ONLY = True" in text
    assert "orders_placed" in text
    for forbidden in ("place_order", "/order", "quantity=", "transaction_type"):
        assert forbidden not in text, forbidden


# --- Analytics Token and explicit verification states --------------------------


def test_the_analytics_token_is_preferred_over_the_access_token():
    configured = settings_with(
        UPSTOX_ANALYTICS_TOKEN="n" * 40, UPSTOX_ACCESS_TOKEN="a" * 40
    )
    assert configured.upstox_token_kind == "analytics"
    assert configured.upstox_market_data_token == "n" * 40


def test_the_access_token_is_the_documented_fallback():
    configured = settings_with(UPSTOX_ACCESS_TOKEN="a" * 40)
    assert configured.upstox_token_kind == "access"
    assert configured.upstox_market_data_token == "a" * 40


def test_both_tokens_are_registered_as_log_secrets():
    configured = settings_with(
        UPSTOX_ANALYTICS_TOKEN="n" * 40, UPSTOX_ACCESS_TOKEN="a" * 40
    )
    assert "n" * 40 in configured.secrets
    assert "a" * 40 in configured.secrets


def test_the_blocked_message_points_at_the_analytics_token(capsys):
    code, payload = run(capsys, settings=settings_with())
    assert code == smoke.EXIT_BLOCKED
    assert payload["state"] == "AUTHENTICATION_BLOCKED"
    assert "UPSTOX_ANALYTICS_TOKEN" in payload["reason"]
    assert "read-only" in payload["reason"]


def test_the_authorize_endpoint_is_the_v3_market_data_feed_path():
    assert smoke.AUTHORIZE_URL == (
        "https://api.upstox.com/v3/feed/market-data-feed/authorize"
    )
    assert smoke.HISTORICAL_V3_PATH == "https://api.upstox.com/v3/historical-candle"


def test_an_authorize_failure_reports_authentication_failed_state(capsys, monkeypatch):
    import urllib.error

    monkeypatch.setattr(
        smoke, "authorized_url", lambda *a, **k: (_ for _ in ()).throw(
            urllib.error.URLError("refused")
        )
    )
    monkeypatch.setattr(smoke, "websockets", __import__("json"), raising=False)
    code, payload = run(capsys, settings=settings_with(UPSTOX_ANALYTICS_TOKEN="t" * 40))
    assert code == smoke.EXIT_FAILED
    assert payload["state"] == "AUTHENTICATION_FAILED"
    assert payload["verified_against_live_account"] is False


def test_the_tls_verification_is_not_disabled_anywhere():
    text = (PROJECT_ROOT / "tools" / "upstox_live_smoke.py").read_text("utf-8")
    # No sslcontext that turns off verification, no CERT_NONE, no verify=False.
    for forbidden in ("CERT_NONE", "verify=False", "check_hostname = False", "ssl._create_unverified"):
        assert forbidden not in text, forbidden


def test_the_signed_url_and_token_are_not_printed(capsys, monkeypatch):
    # A failure must not echo the token; the signed URL is one-time and also not
    # logged. str(exc) on the authorize error carries neither.
    import urllib.error

    secret = "z" * 48
    monkeypatch.setattr(
        smoke, "authorized_url",
        lambda *a, **k: (_ for _ in ()).throw(urllib.error.URLError("boom")),
    )
    monkeypatch.setattr(smoke, "websockets", __import__("json"), raising=False)
    _, payload = run(capsys, settings=settings_with(UPSTOX_ANALYTICS_TOKEN=secret))
    assert secret not in json.dumps(payload)
