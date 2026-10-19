"""Read-only Upstox V3 market-data smoke test.

Run: `python tools/upstox_live_smoke.py [--seconds 30] [--mode ltpc]`

What it does: authorizes, opens one websocket, subscribes to the index, decodes
frames with the vendored official schema, normalizes them, builds candles, and
prints a JSON summary. What it does not do, and cannot be made to do: place,
modify or cancel anything. There is no order code in this file or anywhere it
imports from, and `READ_ONLY` is asserted before the socket is opened.

Credentials come from the environment or `.env` only -- `UPSTOX_ACCESS_TOKEN`.
They are never printed, never logged (the engine's log redactor is installed
with them), and must never be pasted into a terminal that is being recorded or
into a chat. If the token is absent the result is **LIVE BLOCKED** with exit
code 2, which is deliberately not a success: an unauthenticated run proves
nothing about the live feed.

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


def blocked(reason: str, **extra) -> dict:
    return {
        "status": STATUS_BLOCKED,
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
    which avoids depending on a websocket library's redirect behaviour.
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


async def consume(
    url: str,
    *,
    instrument_keys: tuple[str, ...],
    mode: upstox.FeedMode,
    seconds: float,
    session_open,
    symbol: str,
) -> dict:
    import websockets

    decoder = upstox.ProtobufFeedDecoder()
    normalizer = upstox.UpstoxFeedNormalizer(key_to_symbol=lambda key: symbol)
    builder = CandleBuilder(
        symbol, "5m", session_open, provider_name="upstox", is_mock=False
    )
    subscription = upstox.UpstoxSubscription()
    watchdog = FeedWatchdog(silence_limit=timedelta(seconds=30))

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
    deadline = datetime.now(tz=timezone.utc) + timedelta(seconds=seconds)

    async with websockets.connect(url, max_size=None) as socket:
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
                # The feed is binary; a text frame is a protocol surprise worth
                # reporting rather than parsing.
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
                counts["rejected_updates"] += 1
                continue

            for update in updates:
                if update.quote is not None:
                    counts["quotes"] += 1
                if update.tick is None:
                    continue
                counts["ticks"] += 1
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

        await socket.send(
            upstox.UpstoxSubscription.encode(subscription.unsubscribe(instrument_keys))
        )

    return {
        "status": STATUS_VERIFIED if counts["ticks"] else STATUS_FAILED,
        "read_only": READ_ONLY,
        "orders_placed": 0,
        "verified_against_live_account": bool(counts["ticks"]),
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
                    "UPSTOX_ACCESS_TOKEN is not set in the environment or .env. "
                    "Set it in your own shell or .env file; do not paste it into "
                    "a chat, a log, or a recorded terminal.",
                    next_step="Obtain a token through Upstox's OAuth flow, export "
                    "UPSTOX_ACCESS_TOKEN, and rerun during market hours.",
                ),
                indent=2,
            )
        )
        return EXIT_BLOCKED

    try:
        import websockets  # noqa: F401
    except ImportError:
        print(
            json.dumps(
                blocked(
                    "the `websockets` package is not installed",
                    next_step='pip install -e ".[live]"',
                ),
                indent=2,
            )
        )
        return EXIT_BLOCKED

    keys = tuple(args.instrument_key or ())
    if not keys:
        verified = upstox.VERIFIED_INDEX_KEYS.get(args.symbol.upper())
        if not verified:
            print(
                json.dumps(
                    blocked(
                        f"no verified instrument key for {args.symbol!r}; pass "
                        "--instrument-key explicitly rather than letting the "
                        "engine construct one"
                    ),
                    indent=2,
                )
            )
            return EXIT_BLOCKED
        keys = (verified,)

    calendar = SessionCalendar.load(settings.holiday_file)
    now = datetime.now(tz=timezone.utc)
    session_open = calendar.segment_open(Segment.FO, on=now.astimezone().date())
    session = calendar.classify(now, Segment.FO)

    try:
        url = authorized_url(settings.upstox_access_token)
    except (urllib.error.URLError, urllib.error.HTTPError, MarketDataError) as exc:
        print(
            json.dumps(
                {
                    "status": STATUS_FAILED,
                    "stage": "authorize",
                    "read_only": READ_ONLY,
                    "orders_placed": 0,
                    "verified_against_live_account": False,
                    # str(exc) cannot contain the token: it is sent as a header.
                    "error": str(exc),
                },
                indent=2,
            )
        )
        return EXIT_FAILED

    try:
        result = asyncio.run(
            consume(
                url,
                instrument_keys=keys,
                mode=upstox.FeedMode(args.mode),
                seconds=args.seconds,
                session_open=session_open,
                symbol=args.symbol,
            )
        )
    except Exception as exc:  # noqa: BLE001
        print(
            json.dumps(
                {
                    "status": STATUS_FAILED,
                    "stage": "stream",
                    "read_only": READ_ONLY,
                    "orders_placed": 0,
                    "verified_against_live_account": False,
                    "error": f"{type(exc).__name__}: {exc}",
                },
                indent=2,
            )
        )
        return EXIT_FAILED

    result["session_state_at_start"] = session.state.value
    result["schema"] = upstox.ProtobufFeedDecoder().schema_lock["sha256"]
    print(json.dumps(result, indent=2, default=str))
    return EXIT_VERIFIED if result["status"] == STATUS_VERIFIED else EXIT_FAILED


if __name__ == "__main__":
    sys.exit(main())
