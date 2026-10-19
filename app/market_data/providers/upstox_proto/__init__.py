"""Generated decoding classes for Upstox Market Data Feed V3.

`MarketDataFeedV3.proto` is Upstox's official schema, downloaded verbatim from
the URL recorded in `SCHEMA.lock.json`, along with its SHA256, retrieval time
and the pinned generator versions. `MarketDataFeedV3_pb2.py` is produced from it
by `tools/generate_upstox_proto.py`.

Do not edit either file by hand, and do not add a field definition. Field numbers
are a wire contract: a number invented here would decode a price into the wrong
attribute, silently. Regenerate instead; the generator refuses to proceed if the
published schema's hash has changed without that change being acknowledged.
"""

from __future__ import annotations

from app.market_data.providers.upstox_proto import MarketDataFeedV3_pb2 as pb2

FeedResponse = pb2.FeedResponse
Feed = pb2.Feed
FullFeed = pb2.FullFeed
MarketFullFeed = pb2.MarketFullFeed
IndexFullFeed = pb2.IndexFullFeed
FirstLevelWithGreeks = pb2.FirstLevelWithGreeks
LTPC = pb2.LTPC
Quote = pb2.Quote
MarketLevel = pb2.MarketLevel
MarketOHLC = pb2.MarketOHLC
OHLC = pb2.OHLC
OptionGreeks = pb2.OptionGreeks
MarketInfo = pb2.MarketInfo
StatusInfo = pb2.StatusInfo
Type = pb2.Type
RequestMode = pb2.RequestMode
MarketStatus = pb2.MarketStatus

__all__ = [
    "pb2",
    "FeedResponse",
    "Feed",
    "FullFeed",
    "MarketFullFeed",
    "IndexFullFeed",
    "FirstLevelWithGreeks",
    "LTPC",
    "Quote",
    "MarketLevel",
    "MarketOHLC",
    "OHLC",
    "OptionGreeks",
    "MarketInfo",
    "StatusInfo",
    "Type",
    "RequestMode",
    "MarketStatus",
]
