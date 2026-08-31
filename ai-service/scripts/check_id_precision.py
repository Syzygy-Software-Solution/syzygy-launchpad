"""Pin down where a surrogate key loses precision.

Ids in this source are bigints; many run past 2^53, the largest integer an
IEEE-754 double holds exactly. Anything that parses them as a double rounds
them silently (26177172834150613 -> ...612). This script fetches ONE row
straight from the Datasphere consumption layer and prints the RAW bytes, so
you can see whether the id is already wrong when it arrives.

Run it where the destination service is bound (VCAP_SERVICES set):

    cf ssh syzygy-ai-service -c "cd app && python3 scripts/check_id_precision.py cs_payment"

Reading the output:
  * id arrives as a JSON string ("2617...")  -> source honours IEEE754Compatible;
    the service parses it exactly. Nothing to fix.
  * id arrives as a JSON number <= 2^53      -> exact either way. Nothing to fix.
  * id arrives as a JSON number  > 2^53      -> the source ignored the Accept
    parameter. The value MAY already be rounded and no client can recover it;
    the fix belongs in the Datasphere view / consumption API (keep the column
    Edm.Int64, or expose it as a string).
"""
from __future__ import annotations

import asyncio
import json
import pathlib
import re
import sys

import httpx

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

from app.catalog import catalog  # noqa: E402
from app.config import get_settings  # noqa: E402
from app.destination_client import destinations  # noqa: E402

MAX_EXACT_INT = 2 ** 53
ACCEPTS = ["application/json;IEEE754Compatible=true", "application/json"]


async def main(entity_name: str) -> int:
    spec = catalog().get(entity_name)
    if spec is None:
        print(f"unknown entity {entity_name!r}; known: {sorted(catalog())}")
        return 2

    id_cols = [
        c.name for c in spec.columns.values()
        if c.type.lower() == "bigint" and c.role in ("primary_key", "foreign_key")
    ]
    settings = get_settings()
    dest = await destinations().get(settings.tcmp_destination)
    url = f"{dest.url}{settings.tcmp_base_path}".rstrip("/") + spec.endpoint + "?$top=1"

    print(f"entity : {entity_name}\nurl    : {url}\nid cols: {', '.join(id_cols)}\n")

    row: dict = {}
    async with httpx.AsyncClient(timeout=60.0) as client:
        for accept in ACCEPTS:
            resp = await client.get(url, headers={**dest.headers, "Accept": accept})
            print(f"--- Accept: {accept}")
            print(f"    HTTP {resp.status_code}  {resp.headers.get('content-type', '')}")
            if resp.status_code >= 400:
                print(f"    {resp.text[:300]}\n")
                continue

            raw = resp.text
            row = ((resp.json().get("value") or []) + [{}])[0] or {}
            for col in id_cols:
                if col not in row:
                    continue
                # The parsed value tells us the TYPE; the raw text tells us
                # what the server actually put on the wire.
                m = re.search(rf'"{col}"\s*:\s*("?[-\d.eE+]+"?)', raw)
                literal = m.group(1) if m else "?"
                value = row[col]
                if isinstance(value, str):
                    verdict = "OK (string — exact)"
                elif isinstance(value, float):
                    verdict = "LOSSY (JSON float)"
                elif abs(value) > MAX_EXACT_INT:
                    verdict = "AT RISK (JSON number past 2^53 — may be rounded)"
                else:
                    verdict = "OK (below 2^53)"
                print(f"    {col:<28} wire={literal:<24} {verdict}")
            print()

    print(
        "If the id is AT RISK/LOSSY under BOTH headers, the rounding happened "
        "at or before the consumption API and must be fixed there.\n"
        "Compare the value above against the table preview in Datasphere:\n"
        f"  {json.dumps({c: row.get(c) for c in id_cols if c in row})}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(
        asyncio.run(main(sys.argv[1] if len(sys.argv) > 1 else "cs_payment"))
    )
