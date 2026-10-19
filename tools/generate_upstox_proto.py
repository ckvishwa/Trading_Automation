"""Fetch and compile Upstox's official Market Data Feed V3 Protobuf schema.

Run: `python tools/generate_upstox_proto.py [--offline]`

Reproducibility is the point. The schema is downloaded from the URL Upstox
publishes, hashed, and the hash is written to `SCHEMA.lock.json` next to the
generated module. A later run that produces a different hash fails loudly rather
than silently regenerating against a changed schema: the decoder's field numbers
are a wire contract, and a silent change there is a silent misread of prices.

`--offline` compiles the `.proto` already in the tree without downloading, which
is what CI should use.

Nothing here invents a field. If the download fails, the generation fails.
"""

from __future__ import annotations

import argparse
import hashlib
import importlib.metadata
import json
import subprocess
import sys
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

SCHEMA_URL = "https://assets.upstox.com/feed/market-data-feed/v3/MarketDataFeed.proto"
DOCS_URL = "https://upstox.com/developer/api-documentation/v3/get-market-data-feed/"

PROJECT_ROOT = Path(__file__).resolve().parent.parent
PACKAGE_DIR = PROJECT_ROOT / "app" / "market_data" / "providers" / "upstox_proto"
PROTO_NAME = "MarketDataFeedV3.proto"
PROTO_PATH = PACKAGE_DIR / PROTO_NAME
GENERATED_PATH = PACKAGE_DIR / "MarketDataFeedV3_pb2.py"
LOCK_PATH = PACKAGE_DIR / "SCHEMA.lock.json"

# Pinned so a regeneration on another machine produces the same module. The
# generated code carries a runtime-version guard, so protobuf must not drift
# independently of the generator.
PINNED = {"protobuf": "5.29.3", "grpcio-tools": "1.68.1"}


def sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def check_pins() -> dict[str, str]:
    actual = {}
    for name, expected in PINNED.items():
        found = importlib.metadata.version(name)
        actual[name] = found
        if found != expected:
            raise SystemExit(
                f"{name} is {found}, expected the pinned {expected}. "
                f"Install the pins before regenerating: pip install "
                + " ".join(f'"{key}=={value}"' for key, value in PINNED.items())
            )
    return actual


def download() -> bytes:
    request = urllib.request.Request(
        SCHEMA_URL, headers={"User-Agent": "nifty-behavior-engine/0.1 (+local)"}
    )
    with urllib.request.urlopen(request, timeout=60) as response:  # noqa: S310
        if response.status != 200:
            raise SystemExit(f"{SCHEMA_URL} returned HTTP {response.status}")
        return response.read()


def compile_proto() -> None:
    from grpc_tools import protoc

    arguments = [
        "protoc",
        f"--proto_path={PACKAGE_DIR}",
        f"--python_out={PACKAGE_DIR}",
        str(PROTO_PATH),
    ]
    # grpc_tools ships google/protobuf/*.proto, which this schema imports
    # (wrappers.proto). Without that include path the compile cannot resolve it.
    include = Path(protoc.__file__).parent / "_proto"
    arguments.insert(1, f"--proto_path={include}")
    code = protoc.main(arguments)
    if code != 0:
        raise SystemExit(f"protoc failed with exit code {code}")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--offline",
        action="store_true",
        help="compile the .proto already in the tree instead of downloading",
    )
    parser.add_argument(
        "--allow-schema-change",
        action="store_true",
        help="accept a downloaded schema whose hash differs from SCHEMA.lock.json",
    )
    args = parser.parse_args()

    versions = check_pins()
    PACKAGE_DIR.mkdir(parents=True, exist_ok=True)

    previous = json.loads(LOCK_PATH.read_text("utf-8")) if LOCK_PATH.exists() else None

    if args.offline:
        if not PROTO_PATH.exists():
            raise SystemExit(f"{PROTO_PATH} is missing; run without --offline first")
        blob = PROTO_PATH.read_bytes()
        retrieved_at = previous["retrieved_at_utc"] if previous else None
    else:
        blob = download()
        retrieved_at = datetime.now(tz=timezone.utc).isoformat(timespec="seconds")

    digest = sha256(blob)
    if previous and previous["sha256"] != digest and not args.allow_schema_change:
        raise SystemExit(
            "The published schema has changed.\n"
            f"  locked:     {previous['sha256']}\n"
            f"  downloaded: {digest}\n"
            "Field numbers are a wire contract. Review the diff, then rerun with "
            "--allow-schema-change."
        )

    PROTO_PATH.write_bytes(blob)
    compile_proto()

    # proto3 has no version string in the file, so the source URL path is the
    # only version marker Upstox publishes.
    LOCK_PATH.write_text(
        json.dumps(
            {
                "schema_url": SCHEMA_URL,
                "documentation_url": DOCS_URL,
                "schema_version": "v3 (from the URL path; the file declares none)",
                "proto_syntax": "proto3",
                "proto_package": "com.upstox.marketdatafeederv3udapi.rpc.proto",
                "sha256": digest,
                "bytes": len(blob),
                "retrieved_at_utc": retrieved_at,
                "generated_at_utc": datetime.now(tz=timezone.utc).isoformat(
                    timespec="seconds"
                ),
                "generator_versions": versions,
                "generated_file": GENERATED_PATH.name,
            },
            indent=2,
            sort_keys=True,
        )
        + "\n",
        encoding="utf-8",
    )

    print(f"schema sha256 {digest} ({len(blob)} bytes)")
    print(f"generated {GENERATED_PATH.relative_to(PROJECT_ROOT)}")
    print(f"lock      {LOCK_PATH.relative_to(PROJECT_ROOT)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
