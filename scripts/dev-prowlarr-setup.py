#!/usr/bin/env python3
"""Configure the compose Prowlarr for Hermes development: add the PandaCD indexer if it is
missing, test it, and run one music search so the setup is proven end to end.

Run from the repo root once the stack is up:

    uv run python scripts/dev-prowlarr-setup.py [--url http://localhost:9696] [--api-key ...]
    uv run python scripts/dev-prowlarr-setup.py \
        --capture tests/fixtures/prowlarr/search_pandacd_nin.json

PandaCD (https://pandacd.io/) is a public tracker for Creative Commons and artist-permitted
music; Prowlarr ships a definition for it. Keep search volume modest.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
from pathlib import Path

import httpx


def _key_from_dotenv() -> str:
    env = Path(__file__).resolve().parent.parent / ".env"
    if env.exists():
        for line in env.read_text(encoding="utf-8").splitlines():
            if line.startswith("PROWLARR_API_KEY="):
                return line.split("=", 1)[1].strip()
    return ""


DEV_API_KEY = os.environ.get("PROWLARR_API_KEY") or _key_from_dotenv()
INDEXER_DEFINITION = "pandacd"
INDEXER_NAME = "PandaCD"


def wait_ready(client: httpx.Client, timeout: float = 120.0) -> None:
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            r = client.get("/api/v1/system/status")
            if r.status_code == 200:
                print(f"prowlarr {r.json().get('version')} ready")
                return
        except httpx.HTTPError:
            pass
        time.sleep(3)
    raise SystemExit("prowlarr did not become ready")


def ensure_indexer(client: httpx.Client) -> dict:
    existing = client.get("/api/v1/indexer").json()
    for ix in existing:
        if ix.get("definitionName") == INDEXER_DEFINITION:
            print(f"indexer present: {ix['name']} (id {ix['id']})")
            return ix
    schema = client.get("/api/v1/indexer/schema").json()
    template = next((s for s in schema if s.get("definitionName") == INDEXER_DEFINITION), None)
    if template is None:
        raise SystemExit(f"no '{INDEXER_DEFINITION}' definition in this Prowlarr's schema")
    template.update({"name": INDEXER_NAME, "enable": True, "appProfileId": 1, "priority": 25})
    r = client.post("/api/v1/indexer", json=template)
    if r.status_code >= 300:
        raise SystemExit(f"adding indexer failed: {r.status_code} {r.text[:300]}")
    ix = r.json()
    print(f"indexer added: {ix['name']} (id {ix['id']})")
    return ix


def test_indexer(client: httpx.Client, indexer: dict) -> None:
    r = client.post("/api/v1/indexer/test", json=indexer)
    print("indexer test:", "ok" if r.status_code < 300 else f"{r.status_code} {r.text[:200]}")


def smoke_search(client: httpx.Client, indexer_id: int, capture: Path | None) -> None:
    r = client.get(
        "/api/v1/search",
        params={
            "query": "Nine Inch Nails The Slip",
            "type": "music",
            "indexerIds": indexer_id,
            "limit": 20,
        },
        timeout=90,
    )
    r.raise_for_status()
    results = r.json()
    print(f"search returned {len(results)} results")
    for row in results[:6]:
        cats = [c["id"] for c in row.get("categories", [])]
        print(
            f"  {row['title'][:60]:60} seeders={row.get('seeders')} size={row.get('size')} {cats}"
        )
    if capture:
        capture.parent.mkdir(parents=True, exist_ok=True)
        capture.write_text(json.dumps(results, indent=1), encoding="utf-8")
        print(f"captured {capture}")


def main() -> int:
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    ap.add_argument("--url", default="http://localhost:9696")
    ap.add_argument("--api-key", default=DEV_API_KEY)
    ap.add_argument(
        "--capture", type=Path, default=None, help="write the smoke search results here"
    )
    args = ap.parse_args()
    with httpx.Client(base_url=args.url, headers={"X-Api-Key": args.api_key}, timeout=30) as client:
        wait_ready(client)
        indexer = ensure_indexer(client)
        test_indexer(client, indexer)
        smoke_search(client, indexer["id"], args.capture)
    return 0


if __name__ == "__main__":
    sys.exit(main())
