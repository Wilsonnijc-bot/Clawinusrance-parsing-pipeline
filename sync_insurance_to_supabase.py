#!/usr/bin/env python3
"""Bootstrap a CSV with the shared insurance row shape into Supabase/PostgreSQL."""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from insurance_db import DEFAULT_TABLE_NAME, connect, ensure_table, require_db_url, upsert_csv


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Create/update a Supabase table from a local CSV with the insurance row shape."
    )
    parser.add_argument("--csv", default="Insurance_datas.csv")
    parser.add_argument(
        "--table-name",
        default=DEFAULT_TABLE_NAME,
        help="Supabase/PostgreSQL table name to create/update.",
    )
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    csv_path = Path(args.csv)
    if not csv_path.exists():
        raise SystemExit(f"CSV file not found: {csv_path}")

    db_url = require_db_url()
    with connect(db_url) as conn:
        ensure_table(conn, table_name=args.table_name)
        summary = upsert_csv(conn, csv_path, table_name=args.table_name)

    print(
        json.dumps(
            {
                "status": "ok",
                "csv_path": str(csv_path.resolve()),
                **summary,
            },
            indent=2,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
