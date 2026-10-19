"""Verify the engine's Upstox instrument assumptions against the real file.

Run: `python tools/verify_upstox_instruments.py [--exchange NSE] [--keep <dir>]`

Downloads Upstox's published instrument file, hashes it, parses it through the
adapter, and checks the things the engine would otherwise be guessing:

* that the configured alias for `NSE:NIFTY` resolves to a real `INDEX` record,
  and what that record's `instrument_key` and `trading_symbol` actually are;
* that `expiry` really is epoch milliseconds, by checking the decoded dates land
  in a plausible window and on plausible weekdays;
* that option contracts carry a lot size, tick size, strike and expiry, and that
  none of those are ambiguous or missing;
* that `instrument_key` is unique within the file.

It writes a provenance record to `data/reference/` holding the URL, SHA256,
retrieval time, counts and the verified metadata. **The raw file is not kept**
unless `--keep` is given: it is tens of megabytes of vendor data that would
otherwise land in git.

Nothing here invents a value. If the alias does not resolve, that is the finding.
"""

from __future__ import annotations

import argparse
import gzip
import hashlib
import json
import shutil
import sys
import tempfile
import urllib.request
from collections import Counter
from datetime import date, datetime, timezone
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from app.market_data.contracts import OptionType  # noqa: E402
from app.market_data.providers import upstox  # noqa: E402

OUTPUT_PATH = PROJECT_ROOT / "data" / "reference" / "upstox_instruments_verification.json"

# A contract whose expiry decodes outside this window means the unit assumption
# (epoch milliseconds) is wrong, and the engine must not trade on it.
PLAUSIBLE_EXPIRY_FROM = date(2000, 1, 1)
PLAUSIBLE_EXPIRY_TO = date(2045, 1, 1)


def download(url: str, destination: Path) -> tuple[str, int]:
    request = urllib.request.Request(
        url, headers={"User-Agent": "nifty-behavior-engine/0.1 (+local)"}
    )
    digest = hashlib.sha256()
    size = 0
    with urllib.request.urlopen(request, timeout=300) as response:  # noqa: S310
        if response.status != 200:
            raise SystemExit(f"{url} returned HTTP {response.status}")
        with destination.open("wb") as handle:
            while chunk := response.read(1 << 20):
                digest.update(chunk)
                size += len(chunk)
                handle.write(chunk)
    return digest.hexdigest(), size


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--exchange", default="NSE", choices=sorted(upstox.INSTRUMENT_FILES))
    parser.add_argument("--underlying", default="NSE:NIFTY")
    parser.add_argument(
        "--keep",
        type=Path,
        default=None,
        help="keep the downloaded file in this directory instead of discarding it",
    )
    args = parser.parse_args()

    url = upstox.INSTRUMENT_FILES[args.exchange]
    work = Path(tempfile.mkdtemp(prefix="upstox-instruments-"))
    blob_path = work / f"{args.exchange}.json.gz"
    retrieved_at = datetime.now(tz=timezone.utc)

    print(f"downloading {url}")
    digest, size = download(url, blob_path)
    print(f"  sha256 {digest}")
    print(f"  bytes  {size:,}")

    raw = json.loads(gzip.decompress(blob_path.read_bytes()))
    print(f"  records {len(raw):,}")

    findings: dict[str, object] = {
        "schema": 1,
        "source_url": url,
        "sha256": digest,
        "compressed_bytes": size,
        "retrieved_at_utc": retrieved_at.isoformat(timespec="seconds"),
        "record_count": len(raw),
        "authenticated": False,
        "note": "Instrument files are a CDN download; no access token was used, "
        "and the Upstox documentation does not state whether one is required.",
    }

    # --- key uniqueness -------------------------------------------------
    keys = Counter(record.get("instrument_key") for record in raw)
    duplicates = {key: count for key, count in keys.items() if count > 1}
    findings["duplicate_instrument_keys"] = len(duplicates)
    findings["duplicate_examples"] = sorted(duplicates)[:5]

    # --- parse through the adapter --------------------------------------
    master = upstox.UpstoxInstrumentMaster(source_url=url)
    loaded = master.load_records(raw, retrieved_at)
    findings["parsed_records"] = loaded
    print(f"  parsed  {loaded:,}")

    # --- index identity --------------------------------------------------
    alias = upstox.DEFAULT_SYMBOL_ALIASES.get(args.underlying.upper())
    findings["configured_alias"] = {"symbol": args.underlying, "alias": list(alias or ())}
    index_records = [
        record
        for record in raw
        if str(record.get("segment", "")).endswith("_INDEX")
        and "NIFTY" in str(record.get("trading_symbol", "")).upper().replace(" ", "")
    ]
    findings["nifty_like_index_records"] = sorted(
        {
            (
                str(record.get("instrument_key")),
                str(record.get("trading_symbol")),
                str(record.get("name")),
            )
            for record in index_records
        }
    )[:25]

    try:
        index = master.index_instrument(args.underlying)
        findings["index_resolution"] = {
            "status": "RESOLVED",
            "instrument_key": index.provider_key,
            "trading_symbol": index.extra.get("trading_symbol"),
            "name": index.extra.get("name"),
            "segment": index.segment,
            "exchange": index.exchange,
            "exchange_token": index.extra.get("exchange_token"),
            "instrument_type": index.extra.get("instrument_type"),
        }
        print(f"  index   RESOLVED {index.provider_key!r}")
    except Exception as exc:  # noqa: BLE001
        findings["index_resolution"] = {"status": "UNRESOLVED", "error": str(exc)}
        print(f"  index   UNRESOLVED: {exc}")

    # --- option metadata --------------------------------------------------
    option_report: dict[str, object] = {}
    try:
        expiries = master.option_expiries(args.underlying)
        contracts = master.option_contracts(args.underlying, expiries[0])
        implausible = [
            expiry.isoformat()
            for expiry in expiries
            if not PLAUSIBLE_EXPIRY_FROM <= expiry <= PLAUSIBLE_EXPIRY_TO
        ]
        lot_sizes = Counter(
            contract.lot_size
            for expiry in expiries
            for contract in master.option_contracts(args.underlying, expiry)
        )
        tick_sizes = Counter(
            contract.tick_size
            for expiry in expiries
            for contract in master.option_contracts(args.underlying, expiry)
        )
        missing = [
            contract.provider_key
            for expiry in expiries
            for contract in master.option_contracts(args.underlying, expiry)
            if contract.lot_size is None
            or contract.tick_size is None
            or contract.strike is None
            or contract.option_type is None
        ]
        strikes = sorted(
            {contract.strike for contract in contracts if contract.strike is not None}
        )
        calls = master.option_contracts(args.underlying, expiries[0], OptionType.CE)
        puts = master.option_contracts(args.underlying, expiries[0], OptionType.PE)
        option_report = {
            "status": "OK",
            "expiry_count": len(expiries),
            "expiries": [expiry.isoformat() for expiry in expiries[:12]],
            "expiry_weekdays": sorted(
                {expiry.strftime("%A") for expiry in expiries}
            ),
            "implausible_expiries": implausible,
            "expiry_unit_consistent_with_milliseconds": not implausible,
            "nearest_expiry": expiries[0].isoformat(),
            "nearest_expiry_contracts": len(contracts),
            "nearest_expiry_calls": len(calls),
            "nearest_expiry_puts": len(puts),
            "strike_min": strikes[0] if strikes else None,
            "strike_max": strikes[-1] if strikes else None,
            "strike_step_modes": Counter(
                round(b - a, 4) for a, b in zip(strikes, strikes[1:])
            ).most_common(3),
            "lot_sizes": dict(lot_sizes),
            "tick_sizes": dict(tick_sizes),
            "contracts_missing_required_metadata": len(missing),
            "missing_examples": missing[:5],
        }
        print(
            f"  options {len(expiries)} expiries, nearest {expiries[0]} with "
            f"{len(contracts)} contracts, lot sizes {dict(lot_sizes)}"
        )
    except Exception as exc:  # noqa: BLE001
        option_report = {"status": "FAILED", "error": str(exc)}
        print(f"  options FAILED: {exc}")
    findings["option_metadata"] = option_report

    OUTPUT_PATH.parent.mkdir(parents=True, exist_ok=True)
    OUTPUT_PATH.write_text(
        json.dumps(findings, indent=2, sort_keys=True, default=str) + "\n",
        encoding="utf-8",
    )
    print(f"wrote {OUTPUT_PATH.relative_to(PROJECT_ROOT)}")

    if args.keep:
        args.keep.mkdir(parents=True, exist_ok=True)
        shutil.copy2(blob_path, args.keep / blob_path.name)
        print(f"kept raw file at {args.keep / blob_path.name}")
    shutil.rmtree(work, ignore_errors=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
