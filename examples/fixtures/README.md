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
