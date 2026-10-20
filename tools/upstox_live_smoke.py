"""Read-only Upstox V3 market-data smoke test.

Run: `python tools/upstox_live_smoke.py [--seconds 30] [--mode ltpc]`

What it does: authorizes, opens one websocket, subscribes to the index, decodes
frames with the vendored official schema, normalizes them, builds candles, and
prints a JSON summary. What it does not do, and cannot be made to do: place,
modify or cancel anything. There is no order code in this file or anywhere it
imports from, and `READ_ONLY` is asserted before the socket is opened.

Credentials come from the environment or `.env` only. The preferred one is the
free, read-only **Analytics Token** (`UPSTOX_ANALYTICS_TOKEN`), which reaches the
market-data websocket without static-IP whitelisting; a normal
`UPSTOX_ACCESS_TOKEN` is accepted as a fallback. They are never printed, never
logged (the engine's log redactor is installed with them), and must never be
pasted into a terminal that is being recorded or into a chat. TLS verification
is left enabled throughout.

The result is one of the explicit `VerificationState` values. A connected socket
is **not** success: only validated ticks with a completed candle reach
`LIVE_STREAM_VERIFIED`, and a bar-for-bar match against Upstox historical candles
reaches `HISTORICAL_PARITY_VERIFIED`. No credential is `AUTHENTICATION_BLOCKED`
with exit code 2, deliberately not a success.

Exit codes: 0 verified, 2 blocked (no credentials, or the websocket dependency
is absent), 3 the live attempt failed.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
import urllib.error
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from app.config import get_settings  # noqa: E402
from app.logging_setup import configure_logging  # noqa: E402
from app.market_data.candle_builder import CandleBuilder, TickRejected  # noqa: E402
from app.market_data.health import FeedWatchdog  # noqa: E402
from app.market_data.protocols import MarketDataError  # noqa: E402
from app.market_data.providers import upstox  # noqa: E402
from app.market_data.session import Segment, SessionCalendar  # noqa: E402
from app.market_data.verification import (  # noqa: E402
    FeedVerification,
    VerificationState,
    compare_candles,
)

# This script is read-only by construction. Nothing below places an order, and
# the engine exposes no method that could.
READ_ONLY = True

AUTHORIZE_URL = "https://api.upstox.com/v3/feed/market-data-feed/authorize"
STATUS_VERIFIED = "LIVE VERIFIED"
STATUS_BLOCKED = "LIVE BLOCKED"
STATUS_FAILED = "LIVE FAILED"

EXIT_VERIFIED = 0
EXIT_BLOCKED = 2
EXIT_FAILED = 3

HISTORICAL_V3_PATH = "https://api.upstox.com/v3/historical-candle"


def blocked(reason: str, **extra) -> dict:
    return {
        "status": STATUS_BLOCKED,
        "state": VerificationState.AUTHENTICATION_BLOCKED.value,
        "reason": reason,
        "read_only": READ_ONLY,
        "orders_placed": 0,
        "verified_against_live_account": False,
        **extra,
    }


def authorized_url(access_token: str, timeout: float = 30.0) -> str:
    """Ask Upstox for the authorized websocket URL.

    The documented flow answers the websocket endpoint with an HTTP 302 that the
    client must follow; this REST call returns the redirect target directly,
    which avoids depending on a websocket library's redirect behaviour. The
    returned URL is one-time and signed, so it is used immediately and never
    logged.
    """
    request = urllib.request.Request(
        AUTHORIZE_URL,
        headers={
            "Authorization": f"Bearer {access_token}",
            "Accept": "application/json",
        },
    )
    with urllib.request.urlopen(request, timeout=timeout) as response:  # noqa: S310
        payload = json.loads(response.read())
    url = (payload.get("data") or {}).get("authorized_redirect_uri") or (
        payload.get("data") or {}
    ).get("authorizedRedirectUri")
    if not url:
        raise MarketDataError(
            "the authorize response carried no authorized redirect URI; "
            f"keys were {sorted(payload.get('data') or payload)}"
        )
    return url


def fetch_historical_5m(
    access_token: str, instrument_key: str, on: "datetime", timeout: float = 30.0
) -> list:
    """Fetch official 5-minute historical candles for a single day.

    This is the canonical source for parity: a live stream cannot prove its own
    completeness (a dropped update leaves no trace), so built candles are checked
    against these. The v3 path puts to_date before from_date, and the pipe in an
    instrument key is URL-encoded.
    """
    day = on.astimezone(upstox.IST).date().isoformat()
    encoded = instrument_key.replace("|", "%7C")
    url = f"{HISTORICAL_V3_PATH}/{encoded}/minutes/5/{day}/{day}"
    request = urllib.request.Request(
        url,
        headers={"Authorization": f"Bearer {access_token}", "Accept": "application/json"},
    )
    with urllib.request.urlopen(request, timeout=timeout) as response:  # noqa: S310
        payload = json.loads(response.read())
    rows = (payload.get("data") or {}).get("candles") or []
    from app.market_data.contracts import Candle, Provenance

    provenance = Provenance(
        provider="upstox",
        fetched_at=datetime.now(tz=timezone.utc),
        endpoint=HISTORICAL_V3_PATH,
        is_mock=False,
    )
    candles = []
    for row in rows:
        # [timestamp, open, high, low, close, volume, open_interest]; timestamp
        # is the bar's START, ISO 8601 with a +05:30 offset.
        start = datetime.fromisoformat(row[0]).astimezone(timezone.utc)
        candles.append(
            Candle(
                symbol=instrument_key,
                timeframe="5m",
                open_time=start,
                close_time=start + timedelta(minutes=5),
                open=float(row[1]),
                high=float(row[2]),
                low=float(row[3]),
                close=float(row[4]),
                volume=int(row[5]) if len(row) > 5 and row[5] is not None else None,
                provenance=provenance,
            )
        )
    return candles


async def consume(
    url: str,
    *,
    instrument_keys: tuple[str, ...],
    mode: upstox.FeedMode,
    seconds: float,
    session_open,
    symbol: str,
    verification: FeedVerification,
    market_open: bool,
):
    import websockets

    decoder = upstox.ProtobufFeedDecoder()
    normalizer = upstox.UpstoxFeedNormalizer(key_to_symbol=lambda key: symbol)
    builder = CandleBuilder(
        symbol, "5m", session_open, provider_name="upstox", is_mock=False
    )
    subscription = upstox.UpstoxSubscription()
    watchdog = FeedWatchdog(silence_limit=timedelta(seconds=30))
    verification.market_open = market_open

    counts = {
        "frames": 0,
        "status_messages": 0,
        "ticks": 0,
        "quotes": 0,
        "malformed": 0,
        "rejected_updates": 0,
        "candles_completed": 0,
    }
    segment_status: dict[str, str] = {}
    first_tick: dict | None = None
    last_tick: dict | None = None
    built: list = []
    deadline = datetime.now(tz=timezone.utc) + timedelta(seconds=seconds)

    # The socket opened. TLS verification is on (websockets verifies by default;
    # nothing here disables it).
    async with websockets.connect(url, max_size=None) as socket:
        verification.socket_connected = True
        watchdog.on_connect(datetime.now(tz=timezone.utc))
        request = subscription.subscribe(instrument_keys, mode)
        await socket.send(upstox.UpstoxSubscription.encode(request))

        while datetime.now(tz=timezone.utc) < deadline:
            remaining = (deadline - datetime.now(tz=timezone.utc)).total_seconds()
            try:
                frame = await asyncio.wait_for(socket.recv(), timeout=max(0.1, remaining))
            except asyncio.TimeoutError:
                break
            if isinstance(frame, str):
                counts["malformed"] += 1
                continue
            counts["frames"] += 1
            watchdog.on_message(datetime.now(tz=timezone.utc))

            try:
                message = decoder.decode(frame)
            except MarketDataError:
                counts["malformed"] += 1
                continue

            status = (message.get("marketInfo") or {}).get("segmentStatus") or {}
            if status:
                counts["status_messages"] += 1
                segment_status.update({str(k): str(v) for k, v in status.items()})
                continue

            try:
                updates = normalizer.normalize_message(message)
            except MarketDataError:
                # A tick that arrived but failed validation: the data is invalid,
                # which is not the same as no data.
                counts["rejected_updates"] += 1
                verification.invalid_ticks += 1
                continue

            for update in updates:
                if update.quote is not None:
                    counts["quotes"] += 1
                if update.tick is None:
                    continue
                counts["ticks"] += 1
                verification.valid_ticks += 1
                sample = {
                    "exchange_time": update.tick.exchange_time.isoformat(),
                    "last_price": update.tick.last_price,
                }
                first_tick = first_tick or sample
                last_tick = sample
                try:
                    completed = builder.add(update.tick)
                except TickRejected:
                    continue
                if completed is not None:
                    counts["candles_completed"] += 1
                    verification.completed_candles += 1
                    built.append(completed)

        await socket.send(
            upstox.UpstoxSubscription.encode(subscription.unsubscribe(instrument_keys))
        )

    details = {
        "read_only": READ_ONLY,
        "orders_placed": 0,
        "mode": mode.value,
        "instruments": list(instrument_keys),
        "counts": counts,
        "segment_status": segment_status,
        "first_tick": first_tick,
        "last_tick": last_tick,
        "builder_stats": vars(builder.stats),
        "feed_state": watchdog.state(datetime.now(tz=timezone.utc)).value,
        "note": "No order was placed; this engine has no order-placement code.",
    }
    return details, built


def main(argv: list[str] | None = None) -> int:
    assert READ_ONLY, "this script must stay read-only"

    parser = argparse.ArgumentParser()
    parser.add_argument("--seconds", type=float, default=30.0)
    parser.add_argument("--symbol", default="NSE:NIFTY")
    parser.add_argument(
        "--mode",
        default=upstox.FeedMode.LTPC.value,
        choices=[item.value for item in upstox.FeedMode],
    )
    parser.add_argument(
        "--instrument-key",
        action="append",
        default=None,
        help="repeatable; defaults to the verified key for --symbol",
    )
    args = parser.parse_args(argv)

    settings = get_settings()
    # Installed before anything else so that even an exception traceback cannot
    # carry a credential into the log.
    configure_logging(secrets=settings.secrets)

    if not settings.has_upstox_credentials:
        print(
            json.dumps(
                blocked(
                    "No Upstox market-data credential is set. Prefer the free, "
                    "read-only Analytics Token in UPSTOX_ANALYTICS_TOKEN "
                    "(UPSTOX_ACCESS_TOKEN is accepted as a fallback). Set it in "
                    "your own shell or .env; do not paste it into a chat, a log, "
                    "or a recorded terminal.",
                    next_step="Generate an Analytics Token on the Upstox Developer "
                    "Apps page (Analytics tab), export UPSTOX_ANALYTICS_TOKEN, and "
                    "rerun during an authorized market session.",
                ),
                indent=2,
            )
        )
        return EXIT_BLOCKED

    token = settings.upstox_market_data_token
    token_kind = settings.upstox_token_kind

    try:
        import websockets  # noqa: F401
    except ImportError:
        print(json.dumps(blocked(
            "the `websockets` package is not installed",
            next_step='pip install -e ".[live]"',
        ), indent=2))
        return EXIT_BLOCKED

    keys = tuple(args.instrument_key or ())
    if not keys:
        verified = upstox.VERIFIED_INDEX_KEYS.get(args.symbol.upper())
        if not verified:
            print(json.dumps(blocked(
                f"no verified instrument key for {args.symbol!r}; pass "
                "--instrument-key explicitly rather than letting the engine "
                "construct one"
            ), indent=2))
            return EXIT_BLOCKED
        keys = (verified,)

    calendar = SessionCalendar.load(settings.holiday_file)
    now = datetime.now(tz=timezone.utc)
    session_open = calendar.segment_open(Segment.FO, on=now.astimezone().date())
    session = calendar.classify(now, Segment.FO)
    market_open = session.is_open

    verification = FeedVerification(has_credential=True)
    verification.note(f"credential kind: {token_kind}")
    verification.note(f"session at start: {session.state.value}")

    def emit(payload: dict, exit_code: int) -> int:
        payload = {
            "status": {
                VerificationState.HISTORICAL_PARITY_VERIFIED.value: STATUS_VERIFIED,
                VerificationState.LIVE_STREAM_VERIFIED.value: STATUS_VERIFIED,
            }.get(payload.get("state"), payload.get("status", STATUS_FAILED)),
            "schema_sha256": upstox.ProtobufFeedDecoder().schema_lock["sha256"],
            **payload,
        }
        print(json.dumps(payload, indent=2, default=str))
        return exit_code

    try:
        url = authorized_url(token)
    except (urllib.error.URLError, urllib.error.HTTPError, MarketDataError) as exc:
        # A credential existed and the provider rejected the authorize call.
        verification.authenticated = False
        verification.note(f"authorize error: {type(exc).__name__}")
        return emit(
            {
                "status": STATUS_FAILED,
                "stage": "authorize",
                # str(exc) cannot contain the token: it is sent as a header, and
                # the redactor is installed regardless.
                "error": str(exc),
                **verification.as_dict(),
            },
            EXIT_FAILED,
        )
    verification.authenticated = True

    try:
        details, built = asyncio.run(
            consume(
                url,
                instrument_keys=keys,
                mode=upstox.FeedMode(args.mode),
                seconds=args.seconds,
                session_open=session_open,
                symbol=args.symbol,
                verification=verification,
                market_open=market_open,
            )
        )
    except Exception as exc:  # noqa: BLE001
        return emit(
            {
                "status": STATUS_FAILED,
                "stage": "stream",
                "error": f"{type(exc).__name__}: {exc}",
                **verification.as_dict(),
            },
            EXIT_FAILED,
        )

    # Historical parity: compare the candles just built against Upstox's own
    # historical record for the same day. Built candles are the subject; the
    # historical candles are canonical.
    if built:
        try:
            historical = fetch_historical_5m(token, keys[0], now)
            verification.parity = compare_candles(built, historical)
            verification.note(f"historical candles fetched: {len(historical)}")
        except (urllib.error.URLError, urllib.error.HTTPError, MarketDataError) as exc:
            verification.note(f"historical fetch failed: {type(exc).__name__}")

    state = verification.state()
    exit_code = EXIT_VERIFIED if verification.verified else (
        EXIT_BLOCKED if state is VerificationState.AUTHENTICATION_BLOCKED else EXIT_FAILED
    )
    return emit({**details, **verification.as_dict()}, exit_code)


if __name__ == "__main__":
    sys.exit(main())
