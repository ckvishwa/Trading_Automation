# Replayable market-data fixtures

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
