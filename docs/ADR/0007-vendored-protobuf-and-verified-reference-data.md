# ADR 0007 — Vendor the official Protobuf schema; verify reference data against its source

* Status: Accepted
* Date: 2026-10-19
* Milestone: M3

## Context

Three things were being taken on trust, and all three turned out to be wrong or
unprovable in a way that mattered.

1. **The feed wire format.** Upstox's live feed is Protobuf. The decoder was a
   placeholder that raised, because writing field definitions by hand would have
   meant inventing a wire contract: a guessed field number decodes a price into
   the wrong attribute, silently and plausibly.
2. **The instrument identifiers.** The engine's alias for `NSE:NIFTY` was
   written from the documentation's spelling of the instrument key, `Nifty 50`.
3. **The exchange calendar.** nseindia.com's HTML pages do not answer automated
   requests, so the only holiday data available was third-party, including a
   Muhurat date two sources disagreed about.

## Decision

### Vendor the official schema, generated reproducibly

`tools/generate_upstox_proto.py` downloads Upstox's published
`MarketDataFeed.proto`, records its URL, SHA256, byte count, retrieval time and
the pinned generator versions in `SCHEMA.lock.json`, and compiles it with
`protobuf==5.29.3` / `grpcio-tools==1.68.1`. A later run whose download hashes
differently **fails** unless `--allow-schema-change` is passed. Field numbers are
a wire contract; a silent change there is a silent misread of prices.

The decoded value is the documented JSON shape, because protobuf's own JSON
mapping already produces it — camelCase names, 64-bit integers as strings, unset
fields omitted. So a live frame and a recorded JSON fixture travel one code path.

Two proto3 details are handled explicitly rather than left to that mapping,
because a zero-valued enum is indistinguishable from an unset one:
`type` is always set (`initial_feed` is 0, so a snapshot would otherwise arrive
untyped) and `requestMode` is filled per feed from the parsed message.

Reading the real schema also corrected two assumptions: `IndexFullFeed` carries
**no depth at all**, so an index in full mode yields a tick and no quote; and the
schema's enum spells the five-level book `full_d5` while the subscription API
calls that mode `full`. Both spellings are reported rather than reconciled.

### Verify reference data against its source, and record the evidence

`tools/verify_upstox_instruments.py` downloads the real instrument file, hashes
it, parses it through the adapter and writes
`data/reference/upstox_instruments_verification.json`. Two findings:

* **`NSE_INDEX|Nifty 50` is the real key, but its `trading_symbol` is `NIFTY`**
  and `Nifty 50` is its `name`. An index record's `trading_symbol` is the
  derivatives ticker. The documented alias therefore did not resolve, and the
  adapter refused to guess — which is how this was caught rather than shipped.
* **`tick_size` is published in paise.** NIFTY options carry `5.0` and trade in
  ₹0.05 ticks; NSE equities carry 1.0/5.0/10.0/50.0/100.0 against the
  ₹0.01–₹1.00 regime. The documentation states no unit. The adapter converts to
  rupees and keeps the raw value in `extra`.

Expiry really is epoch milliseconds: all 18 NIFTY expiries decoded to plausible
Mondays and Tuesdays in 2026–2028. Taken in **IST**, because an expiry stamped
23:59:59 IST reads as the previous day in UTC.

An option missing a lot size, tick size, strike or expiry is now **rejected**
rather than loaded. A defaulted lot size is a wrong order waiting to be sized.

### Separate holiday provenance from timing provenance, per segment

Calendar schema 3. NSE circular **NSE/FAOP/71777** (12 Dec 2025) was retrieved
from `nsearchives.nseindia.com`, which does answer automated requests, and gives
the official 2026 **F&O** holiday list. It states no session timings and says
nothing about the cash segment. So:

* each segment carries its own `holidays_official_through` — F&O 2026-12-31,
  cash `null`;
* session windows keep their own `source`, still `unofficial`;
* an ordinary 2026 trading day therefore reports `UNVERIFIED_CALENDAR` **even
  though the holiday list is official**, because `OPEN` needs both;
* the cash holiday entries stay `unofficial` and are not inferred from the F&O
  list.

Special sessions are now checked **before** the weekend rule. Muhurat 2026 is on
Sunday 2026-11-08; under the old order it would have reported `WEEKEND` and the
pending session would have been invisible. Its timings are "notified
subsequently", so it reports `UNSUPPORTED_SESSION` rather than assuming regular
hours. The third-party 2026-10-21 guess is gone.

### Live access fails closed

`tools/upstox_live_smoke.py` is read-only and reports `LIVE BLOCKED` with exit
code **2** when `UPSTOX_ACCESS_TOKEN` is absent — deliberately not a success.
Credentials are read from the environment only, registered with the log
redactor, and never printed. A repo-wide test asserts no order-placement code
exists anywhere in `app/` or `tools/`.

## Consequences

* `protobuf==5.29.3` becomes a runtime dependency, pinned because the generated
  module carries a runtime-version guard. `grpcio-tools` and `websockets` stay
  in optional extras.
* Regenerating the schema is a deliberate, reviewed act.
* The vendored instrument fixture now mirrors the real record shape, so a test
  that passes against it means something about the real file.
* Candles are stored in SQLite keyed on (symbol, timeframe, open_time), so a
  replayed bar cannot be double-counted or rewritten.
* Still unverified, and marked so in code: the feed's timestamp timezone, the
  cash holiday list, all session timings, Muhurat timings, and anything needing
  an authenticated account (`VERIFIED_AGAINST_LIVE_ACCOUNT = False`).
