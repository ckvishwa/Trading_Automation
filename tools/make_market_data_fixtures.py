"""Generate replayable market-data fixtures in the providers' documented shapes."""

import gzip
import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

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
instruments = [
    {
        "instrument_key": KEY,
        "exchange_token": "26000",
        "trading_symbol": "Nifty 50",
        "name": "NIFTY 50",
        "segment": "NSE_INDEX",
        "exchange": "NSE",
        "instrument_type": "INDEX",
    },
    {
        "instrument_key": "NSE_FO|43309",
        "exchange_token": "43309",
        "trading_symbol": "NIFTY 24000 CE 29 OCT 26",
        "name": "NIFTY",
        "segment": "NSE_FO",
        "exchange": "NSE",
        "instrument_type": "CE",
        "lot_size": 75,
        "tick_size": 0.05,
        "freeze_quantity": 1800.0,
        "minimum_lot": 75,
        "expiry": int(
            datetime(2026, 10, 29, 23, 59, 59, tzinfo=timezone.utc).timestamp() * 1000
        ),
        "strike_price": 24000.0,
        "underlying_key": KEY,
        "underlying_symbol": "NIFTY",
        "underlying_type": "INDEX",
        "weekly": True,
    },
    {
        "instrument_key": "NSE_FO|43310",
        "exchange_token": "43310",
        "trading_symbol": "NIFTY 24000 PE 29 OCT 26",
        "name": "NIFTY",
        "segment": "NSE_FO",
        "exchange": "NSE",
        "instrument_type": "PE",
        "lot_size": 75,
        "tick_size": 0.05,
        "minimum_lot": 75,
        "expiry": int(
            datetime(2026, 10, 29, 23, 59, 59, tzinfo=timezone.utc).timestamp() * 1000
        ),
        "strike_price": 24000.0,
        "underlying_key": KEY,
        "underlying_symbol": "NIFTY",
        "underlying_type": "INDEX",
        "weekly": True,
    },
    {
        "instrument_key": "NSE_FO|43311",
        "exchange_token": "43311",
        "trading_symbol": "NIFTY 24100 CE 05 NOV 26",
        "name": "NIFTY",
        "segment": "NSE_FO",
        "exchange": "NSE",
        "instrument_type": "CE",
        "lot_size": 75,
        "tick_size": 0.05,
        "minimum_lot": 75,
        "expiry": int(
            datetime(2026, 11, 5, 23, 59, 59, tzinfo=timezone.utc).timestamp() * 1000
        ),
        "strike_price": 24100.0,
        "underlying_key": KEY,
        "underlying_symbol": "NIFTY",
        "underlying_type": "INDEX",
        "weekly": True,
    },
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

The frames are the **decoded** structure, not Protobuf bytes. Upstox's live feed
is Protobuf-encoded, and generating the stubs needs the published `.proto` plus
`protoc`; `JsonFeedDecoder` reads these fixtures through exactly the
normalization the live path uses, so only the wire decode is left to swap in.

Timestamps are anchored to 09:15 IST on Monday 2026-10-19 so bars line up with
the engine's session-open grid. Regenerate with the script referenced in
`docs/TEST_PLAN.md`.
""",
    encoding="utf-8",
)
print("wrote", readme)
