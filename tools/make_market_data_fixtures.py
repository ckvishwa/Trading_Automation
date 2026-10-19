"""Generate replayable market-data fixtures in the providers' documented shapes."""

import gzip
import json
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

OUT = Path("examples/fixtures")
OUT.mkdir(parents=True, exist_ok=True)

# 09:15 IST on Monday 2026-10-19 == 03:45 UTC.
SESSION_OPEN = datetime(2026, 10, 19, 3, 45, tzinfo=timezone.utc)
KEY = "NSE_INDEX|Nifty 50"


def ms(moment: datetime) -> str:
    return str(int(moment.timestamp() * 1000))


# ---------------------------------------------------------------- feed session
frames: list[dict] = [
    {
        "type": "market_info",
        "currentTs": ms(SESSION_OPEN),
        "marketInfo": {"segmentStatus": {"NSE_EQ": "NORMAL_OPEN"}},
    }
]

# Eleven ticks across two 5-minute bars, plus one deliberate duplicate and one
# deliberate out-of-order tick inside the open bar.
path = [
    (0, 25_000.0),
    (1, 25_012.5),
    (2, 25_004.0),
    (2, 25_004.0),  # duplicate: same timestamp and price
    (4, 25_020.0),
    (3, 25_008.0),  # out of order, still inside the first bar
    (5, 25_018.0),
    (6, 25_040.0),
    (7, 25_031.0),
    (9, 25_035.5),
    (10, 25_050.0),
]
for minutes, price in path:
    moment = SESSION_OPEN + timedelta(minutes=minutes)
    frames.append(
        {
            "type": "live_feed",
            "currentTs": ms(moment),
            "feeds": {
                KEY: {
                    "ltpc": {
                        "ltp": price,
                        "ltt": ms(moment),
                        "ltq": "50",
                        "cp": 24_980.0,
                    }
                }
            },
        }
    )

feed_path = OUT / "upstox-feed-index-session.jsonl"
feed_path.write_text(
    "\n".join(json.dumps(frame) for frame in frames) + "\n", encoding="utf-8"
)
print("wrote", feed_path, len(frames), "frames")

# ------------------------------------------------------- option quote feed
option_key = "NSE_FO|43309"
option_frames = []
for minutes, (bid, ask) in enumerate(
    [(120.25, 120.75), (121.0, 121.6), (119.8, 120.1)]
):
    moment = SESSION_OPEN + timedelta(minutes=minutes)
    option_frames.append(
        {
            "type": "live_feed",
            "currentTs": ms(moment),
            "feeds": {
                option_key: {
                    "fullFeed": {
                        "marketFF": {
                            "ltpc": {"ltp": (bid + ask) / 2, "ltt": ms(moment), "ltq": "75"},
                            "marketLevel": {
                                "bidAskQuote": [
                                    {"bidQ": "750", "bidP": bid, "askQ": "900", "askP": ask}
                                ]
                            },
                            "marketOHLC": {
                                "ohlc": [
                                    {
                                        "interval": "I1",
                                        "open": bid,
                                        "high": ask,
                                        "low": bid,
                                        "close": ask,
                                        "vol": 1200,
                                        "ts": ms(moment),
                                    }
                                ]
                            },
                            "oi": 1_500_000,
                            "iv": 0.1187,
                        }
                    }
                }
            },
        }
    )
option_path = OUT / "upstox-feed-option-quotes.jsonl"
option_path.write_text(
    "\n".join(json.dumps(frame) for frame in option_frames) + "\n", encoding="utf-8"
)
print("wrote", option_path, len(option_frames), "frames")

# ------------------------------------------------------- instrument master
# Record shape copied from the REAL downloaded NSE file (2026-10-19, sha256
# d6670c14...), not from the documentation: tick_size in paise, lot_size 65,
# and the asset_* fields the docs do not mention. Values are synthetic; the
# shape is not.
OPTION_TOKENS = {"ce_near": 43309, "pe_near": 43310, "ce_far": 43311}


def expiry_millis(year: int, month: int, day: int) -> int:
    # The real file stamps expiry at 23:59:59 IST, i.e. 18:29:59Z.
    return int(
        datetime(year, month, day, 18, 29, 59, tzinfo=timezone.utc).timestamp() * 1000
    )


def option_record(token: int, strike: int, kind: str, expiry: int, label: str) -> dict:
    return {
        "asset_key": KEY,
        "asset_symbol": "NIFTY",
        "asset_type": "INDEX",
        "exchange": "NSE",
        "exchange_token": str(token),
        "expiry": expiry,
        "freeze_quantity": 3510.0,
        "instrument_key": f"NSE_FO|{token}",
        "instrument_type": kind,
        "lot_size": 65,
        "minimum_lot": 65,
        "name": "NIFTY",
        "qty_multiplier": 1.0,
        "segment": "NSE_FO",
        "strike_price": float(strike),
        # PAISE. 5.0 == Rs 0.05. Shipping this as rupees would round every
        # option price a hundredfold wrong.
        "tick_size": 5.0,
        "trading_symbol": f"NIFTY {strike} {kind} {label}",
        "underlying_key": KEY,
        "underlying_symbol": "NIFTY",
        "underlying_type": "INDEX",
        "weekly": True,
    }


NEAR_EXPIRY = expiry_millis(2026, 10, 27)
FAR_EXPIRY = expiry_millis(2026, 11, 3)

instruments = [
    {
        # Verified verbatim from the real file: the key spells the index name,
        # while trading_symbol is the derivatives ticker.
        "cas_eligible": True,
        "exchange": "NSE",
        "exchange_token": "26000",
        "instrument_key": KEY,
        "instrument_type": "INDEX",
        "name": "Nifty 50",
        "segment": "NSE_INDEX",
        "trading_symbol": "NIFTY",
    },
    option_record(OPTION_TOKENS["ce_near"], 24000, "CE", NEAR_EXPIRY, "27 OCT 26"),
    option_record(OPTION_TOKENS["pe_near"], 24000, "PE", NEAR_EXPIRY, "27 OCT 26"),
    option_record(OPTION_TOKENS["ce_far"], 24100, "CE", FAR_EXPIRY, "03 NOV 26"),
]
instrument_path = OUT / "upstox-instruments-sample.json.gz"
instrument_path.write_bytes(gzip.compress(json.dumps(instruments).encode("utf-8")))
print("wrote", instrument_path, len(instruments), "records")

# ------------------------------------------------------- FYERS symbol master
fyers_master = {
    "NSE:NIFTY50-INDEX": {
        "fyToken": "101000000026000",
        "exSymbol": "NIFTY 50",
        "symDetails": "NIFTY 50 INDEX",
        "symTicker": "NSE:NIFTY50-INDEX",
        "exchange": 10,
        "segment": 10,
        "exToken": 26000,
        "optType": "XX",
        "minLotSize": 1,
        "tickSize": 0.05,
        "tradingSession": "09:15-15:30",
        "lastUpdate": "2026-10-19",
        "tradeStatus": 1,
        "exchangeName": "NSE",
        "strikePrice": -1.0,
    },
    "NSE:NIFTY26O2924000CE": {
        "fyToken": "101260929024000",
        "exSymbol": "NIFTY",
        "symDetails": "NIFTY 29 Oct 2026 CE 24000.00",
        "symTicker": "NSE:NIFTY26O2924000CE",
        "exchange": 10,
        "segment": 11,
        "exToken": 43309,
        "optType": "CE",
        "underSym": "NIFTY",
        "underFyTok": "101000000026000",
        "minLotSize": 75,
        "tickSize": 0.05,
        "expiryDate": str(int(datetime(2026, 10, 29, 10, 0, tzinfo=timezone.utc).timestamp())),
        "strikePrice": 24000.0,
        "qtyFreeze": "1800",
        "tradeStatus": 1,
        "exchangeName": "NSE",
        "lastUpdate": "2026-10-19",
    },
    "NSE:NIFTY26O2924000PE": {
        "fyToken": "101260929024001",
        "exSymbol": "NIFTY",
        "symDetails": "NIFTY 29 Oct 2026 PE 24000.00",
        "symTicker": "NSE:NIFTY26O2924000PE",
        "exchange": 10,
        "segment": 11,
        "exToken": 43310,
        "optType": "PE",
        "underSym": "NIFTY",
        "underFyTok": "101000000026000",
        "minLotSize": 75,
        "tickSize": 0.05,
        "expiryDate": str(int(datetime(2026, 10, 29, 10, 0, tzinfo=timezone.utc).timestamp())),
        "strikePrice": 24000.0,
        "tradeStatus": 0,
        "exchangeName": "NSE",
        "lastUpdate": "2026-10-19",
    },
}
fyers_path = OUT / "fyers-symbol-master-sample.json"
fyers_path.write_text(json.dumps(fyers_master, indent=2) + "\n", encoding="utf-8")
print("wrote", fyers_path, len(fyers_master), "records")

# ------------------------------------------------------- FYERS history
base = int(SESSION_OPEN.timestamp())
history = {
    "s": "ok",
    "symbol": "NSE:NIFTY26O2924000CE",
    "resolution": "5",
    "schema_version": 1,
    "columns": [
        "timestamp",
        "open",
        "high",
        "low",
        "close",
        "volume",
        "open_interest",
        "iv",
        "delta",
        "gamma",
        "theta",
        "vega",
    ],
    "candles": [
        [base, 120.0, 122.5, 119.5, 121.0, 45_000, 1_500_000, 11.87, 0.52, 0.0004, -8.1, 12.4],
        [base + 300, 121.0, 124.0, 120.8, 123.5, 51_000, None, None, None, None, None, None],
        [base + 600, 123.5, 123.9, 121.0, 121.4, 38_500, 1_512_000, 11.4, 0.5, 0.0004, -8.4, 12.1],
    ],
}
history_path = OUT / "fyers-expired-option-history.json"
history_path.write_text(json.dumps(history, indent=2) + "\n", encoding="utf-8")
print("wrote", history_path, len(history["candles"]), "candles")

readme = OUT / "README.md"
readme.write_text(
    """# Replayable market-data fixtures

Captured-shape fixtures that let the whole market-data path run offline: feed
decoding, normalization, candle construction and historical parsing. They are
**synthetic**, in each provider's documented payload shape, and everything built
from them carries mock or fixture provenance.

| File | Shape | Used for |
|---|---|---|
| `upstox-feed-index-session.jsonl` | Decoded Upstox V3 `market_info` + `live_feed` frames, `ltpc` mode | Index tick stream, including one duplicate tick and one out-of-order tick inside the open bar |
| `upstox-feed-option-quotes.jsonl` | Decoded `live_feed` frames, `full` mode with `marketLevel.bidAskQuote` | Option bid/ask normalization |
| `upstox-instruments-sample.json.gz` | Gzipped JSON array, instrument-file record shape | Instrument master parsing, expiry and option-chain queries |
| `fyers-symbol-master-sample.json` | JSON object keyed by `symTicker` | FYERS symbol master parsing, including an inactive `tradeStatus` row |
| `fyers-expired-option-history.json` | `s`/`columns`/`candles` history response | Expired-F&O parsing with null OI and Greeks on one candle |
| `upstox-feed-session.binpb` | **Real Protobuf wire bytes**, length-prefixed (4-byte big-endian) | End-to-end replay through the genuine decoder: a status frame, a snapshot, an LTPC price path with a duplicate and an out-of-order tick, a deliberately malformed frame, and one five-level option quote |

`upstox-feed-session.binpb` holds genuine Protobuf wire bytes, serialized with
the classes generated from Upstox's official schema, and is what the end-to-end
replay test runs. Protobuf messages are not self-delimiting -- concatenating two
yields a third valid message with later fields overwriting earlier ones -- so
each frame carries a 4-byte big-endian length prefix.

The `.jsonl` files hold the **decoded** structure instead, read by
`JsonFeedDecoder`. They stay useful because protobuf's own JSON mapping produces
exactly that shape, so a hand-written case can be added without serializing it.

The instrument fixture's record shape is copied from the real downloaded NSE
file: `tick_size` in paise, `lot_size` 65, and the `asset_*` fields the
documentation does not mention.

Timestamps are anchored to 09:15 IST on Monday 2026-10-19 so bars line up with
the engine's session-open grid. Regenerate with the script referenced in
`docs/TEST_PLAN.md`.
""",
    encoding="utf-8",
)
print("wrote", readme)


# ------------------------------------------------- real Protobuf feed frames
# Length-prefixed wire bytes, so the end-to-end replay runs the genuine decoder
# rather than a JSON stand-in. Built with the generated classes, which come from
# Upstox's official schema.
from app.market_data.providers import upstox_proto as _up  # noqa: E402
from app.research.market_replay import write_length_prefixed  # noqa: E402

_INDEX_KEY = "NSE_INDEX|Nifty 50"
_OPTION_KEY = "NSE_FO|50978"


def _ms(moment: datetime) -> int:
    return int(moment.timestamp() * 1000)


def _status_frame() -> bytes:
    response = _up.FeedResponse(type=_up.Type.market_info, currentTs=_ms(SESSION_OPEN))
    response.marketInfo.segmentStatus["NSE_FO"] = _up.MarketStatus.NORMAL_OPEN
    response.marketInfo.segmentStatus["NSE_EQ"] = _up.MarketStatus.NORMAL_OPEN
    return response.SerializeToString()


def _snapshot_frame() -> bytes:
    response = _up.FeedResponse(type=_up.Type.initial_feed, currentTs=_ms(SESSION_OPEN))
    feed = response.feeds[_INDEX_KEY]
    feed.ltpc.ltp = 25_000.0
    feed.ltpc.ltt = _ms(SESSION_OPEN)
    feed.ltpc.ltq = 25
    feed.ltpc.cp = 24_980.0
    return response.SerializeToString()


def _ltpc_frame(minutes: float, price: float, quantity: int = 50) -> bytes:
    moment = SESSION_OPEN + timedelta(minutes=minutes)
    response = _up.FeedResponse(type=_up.Type.live_feed, currentTs=_ms(moment))
    feed = response.feeds[_INDEX_KEY]
    feed.ltpc.ltp = price
    feed.ltpc.ltt = _ms(moment)
    feed.ltpc.ltq = quantity
    return response.SerializeToString()


def _option_frame(minutes: float, bid: float, ask: float) -> bytes:
    moment = SESSION_OPEN + timedelta(minutes=minutes)
    response = _up.FeedResponse(type=_up.Type.live_feed, currentTs=_ms(moment))
    feed = response.feeds[_OPTION_KEY]
    feed.requestMode = _up.RequestMode.full_d5
    market = feed.fullFeed.marketFF
    market.ltpc.ltp = round((bid + ask) / 2, 2)
    market.ltpc.ltt = _ms(moment)
    market.ltpc.ltq = 65
    for level in range(5):
        market.marketLevel.bidAskQuote.add(
            bidQ=650 + level * 65,
            bidP=round(bid - level * 0.05, 2),
            askQ=780 + level * 65,
            askP=round(ask + level * 0.05, 2),
        )
    market.optionGreeks.delta = 0.52
    market.oi = 1_500_000
    market.iv = 0.1187
    market.vtt = 45_000
    return response.SerializeToString()


_frames = [_status_frame(), _snapshot_frame()]
# Same price path as the JSON fixture, so the two recordings describe one
# session: a duplicate tick, an out-of-order tick inside the open bar, and a
# three-bucket gap are all present on purpose.
for _minutes, _price in [
    (1, 25_012.5),
    (2, 25_004.0),
    (2, 25_004.0),
    (4, 25_020.0),
    (3, 25_008.0),
    (5, 25_018.0),
    (6, 25_040.0),
    (7, 25_031.0),
    (9, 25_035.5),
    (10, 25_050.0),
    (25, 25_061.0),
]:
    _frames.append(_ltpc_frame(_minutes, _price))
# One malformed frame, to prove a bad frame is counted and skipped rather than
# interpolated or fatal.
_frames.append(b"\xff\xff\xff\xff")
_frames.append(_option_frame(11, 120.25, 120.75))

_pb_path = OUT / "upstox-feed-session.binpb"
write_length_prefixed(_pb_path, _frames)
print("wrote", _pb_path, len(_frames), "length-prefixed Protobuf frames")
