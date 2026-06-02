#!/usr/bin/env python3
"""Shared PostgreSQL sync helpers for Insurance_datas-compatible rows."""

from __future__ import annotations

import csv
import json
import os
from pathlib import Path
from typing import Any

import psycopg
from psycopg import sql

SUPABASE_DB_URL_ENV = "SUPABASE_DB_URL"
DEFAULT_TABLE_NAME = "insurance_products"
CSV_COLUMNS: list[str] = [
    "Plan_ID",
    "Plan_Name",
    "Provider_Company",
    "URL",
    "Product_Brochure_route",
    "Plan_category",
    "Coverage_Description",
    "pricing",
    "age",
    " customer requirement",
    " price structure",
    " additional informations",
    "Last_Updated",
]
DB_COLUMN_BY_CSV_COLUMN: dict[str, str] = {
    "Plan_ID": "plan_id",
    "Plan_Name": "plan_name",
    "Provider_Company": "provider_company",
    "URL": "url",
    "Product_Brochure_route": "product_brochure_route",
    "Plan_category": "plan_category",
    "Coverage_Description": "coverage_description",
    "pricing": "pricing",
    "age": "age",
    " customer requirement": "customer_requirement",
    " price structure": "price_structure",
    " additional informations": "additional_informations",
    "Last_Updated": "last_updated",
}
DB_COLUMNS: list[str] = [DB_COLUMN_BY_CSV_COLUMN[column] for column in CSV_COLUMNS]
UPSERT_KEY_DB_COLUMNS = ("url", "plan_name")


def require_db_url() -> str:
    db_url = os.environ.get(SUPABASE_DB_URL_ENV, "").strip()
    if not db_url:
        raise SystemExit(
            f"Missing required environment variable {SUPABASE_DB_URL_ENV}. "
            "Set it to your Supabase PostgreSQL connection string."
        )
    return db_url


def connect(db_url: str | None = None) -> psycopg.Connection[Any]:
    return psycopg.connect(db_url or require_db_url())


def _identifier_list(columns: list[str] | tuple[str, ...]) -> sql.SQL:
    return sql.SQL(", ").join(sql.Identifier(column) for column in columns)


def _validate_table_name(table_name: str) -> str:
    normalized = str(table_name or "").strip()
    if not normalized:
        raise ValueError("table_name must be non-empty")
    return normalized


def _load_table_columns(conn: psycopg.Connection[Any], table_name: str) -> set[str]:
    query = """
        SELECT column_name
        FROM information_schema.columns
        WHERE table_schema = 'public' AND table_name = %s
    """
    with conn.cursor() as cur:
        cur.execute(query, (_validate_table_name(table_name),))
        return {str(row[0]) for row in cur.fetchall()}


def ensure_table(
    conn: psycopg.Connection[Any],
    table_name: str = DEFAULT_TABLE_NAME,
) -> None:
    table_name = _validate_table_name(table_name)
    column_defs = [
        sql.SQL("{} text").format(sql.Identifier(column)) for column in DB_COLUMNS
    ]
    create_table = sql.SQL(
        """
        CREATE TABLE IF NOT EXISTS {table} (
            id bigint generated always as identity primary key,
            {business_columns},
            created_at timestamptz not null default now(),
            updated_at timestamptz not null default now()
        )
        """
    ).format(
        table=sql.Identifier(table_name),
        business_columns=sql.SQL(", ").join(column_defs),
    )
    create_unique_index = sql.SQL(
        """
        CREATE UNIQUE INDEX IF NOT EXISTS {index_name}
        ON {table} ({key_columns})
        """
    ).format(
        index_name=sql.Identifier(f"{table_name}_url_plan_name_key"),
        table=sql.Identifier(table_name),
        key_columns=_identifier_list(list(UPSERT_KEY_DB_COLUMNS)),
    )

    with conn.cursor() as cur:
        cur.execute(create_table)
        existing_columns = _load_table_columns(conn, table_name)

        # Migrate legacy literal CSV-style columns to snake_case when present.
        for csv_column, db_column in DB_COLUMN_BY_CSV_COLUMN.items():
            if csv_column in existing_columns and db_column not in existing_columns:
                cur.execute(
                    sql.SQL("ALTER TABLE {table} RENAME COLUMN {old} TO {new}").format(
                        table=sql.Identifier(table_name),
                        old=sql.Identifier(csv_column),
                        new=sql.Identifier(db_column),
                    )
                )
                existing_columns.remove(csv_column)
                existing_columns.add(db_column)

        # If the table predates a schema change, add any missing snake_case business columns.
        for db_column in DB_COLUMNS:
            if db_column in existing_columns:
                continue
            cur.execute(
                sql.SQL("ALTER TABLE {table} ADD COLUMN {column} text").format(
                    table=sql.Identifier(table_name),
                    column=sql.Identifier(db_column),
                )
            )
            existing_columns.add(db_column)

        if "created_at" not in existing_columns:
            cur.execute(
                sql.SQL(
                    "ALTER TABLE {table} ADD COLUMN created_at timestamptz not null default now()"
                ).format(table=sql.Identifier(table_name))
            )
            existing_columns.add("created_at")

        if "updated_at" not in existing_columns:
            cur.execute(
                sql.SQL(
                    "ALTER TABLE {table} ADD COLUMN updated_at timestamptz not null default now()"
                ).format(table=sql.Identifier(table_name))
            )
            existing_columns.add("updated_at")

        cur.execute(create_unique_index)
    conn.commit()


def _normalize_row(row: dict[str, Any]) -> dict[str, str]:
    normalized: dict[str, str] = {}
    for column in CSV_COLUMNS:
        value = row.get(column, "")
        if value is None:
            normalized[column] = ""
        else:
            normalized[column] = str(value)
    return normalized


def _normalize_rows(rows: list[dict[str, Any]]) -> list[dict[str, str]]:
    deduped: dict[tuple[str, str], dict[str, str]] = {}
    ordered_keys: list[tuple[str, str]] = []

    for row in rows:
        normalized = _normalize_row(row)
        key = tuple(normalized[column] for column in ("URL", "Plan_Name"))
        if not all(part.strip() for part in key):
            raise RuntimeError(
                "Cannot sync row without both URL and Plan_Name. "
                f"row={json.dumps(normalized, ensure_ascii=False)}"
            )
        if key not in deduped:
            ordered_keys.append(key)
        deduped[key] = normalized

    return [deduped[key] for key in ordered_keys]


def read_csv_rows(csv_path: Path) -> tuple[list[str], list[dict[str, str]]]:
    with csv_path.open("r", encoding="utf-8-sig", newline="") as fp:
        reader = csv.DictReader(fp)
        header = reader.fieldnames or []
        rows = [_normalize_row(row) for row in reader]
    return header, rows


def read_csv_row_window(
    csv_path: Path,
    start_row: int,
    end_row: int,
) -> list[dict[str, str]]:
    if start_row <= 0:
        raise ValueError("start_row must be >= 1")
    if end_row < start_row:
        return []

    selected: list[dict[str, str]] = []
    with csv_path.open("r", encoding="utf-8-sig", newline="") as fp:
        reader = csv.DictReader(fp)
        for row_number, row in enumerate(reader, start=1):
            if row_number < start_row:
                continue
            if row_number > end_row:
                break
            selected.append(_normalize_row(row))
    return selected


def write_csv_rows(csv_path: Path, rows: list[dict[str, Any]]) -> None:
    csv_path.parent.mkdir(parents=True, exist_ok=True)
    with csv_path.open("w", encoding="utf-8", newline="") as fp:
        writer = csv.DictWriter(fp, fieldnames=CSV_COLUMNS, extrasaction="ignore")
        writer.writeheader()
        for row in rows:
            writer.writerow(_normalize_row(row))


def export_table_to_csv(
    conn: psycopg.Connection[Any],
    csv_path: Path,
    table_name: str = DEFAULT_TABLE_NAME,
) -> dict[str, Any]:
    table_name = _validate_table_name(table_name)
    query = sql.SQL(
        "SELECT {columns} FROM {table} ORDER BY id ASC"
    ).format(
        columns=_identifier_list(DB_COLUMNS),
        table=sql.Identifier(table_name),
    )
    with conn.cursor() as cur:
        cur.execute(query)
        rows = cur.fetchall()

    payload_rows: list[dict[str, str]] = []
    for row in rows:
        payload_rows.append(
            {
                CSV_COLUMNS[idx]: ("" if row[idx] is None else str(row[idx]))
                for idx in range(len(CSV_COLUMNS))
            }
        )

    write_csv_rows(csv_path, payload_rows)
    return {
        "table": table_name,
        "csv_path": str(csv_path.resolve()),
        "rows_exported": len(payload_rows),
    }


def _load_existing_keys(
    conn: psycopg.Connection[Any],
    table_name: str,
) -> set[tuple[str, str]]:
    query = sql.SQL(
        "SELECT {columns} FROM {table}"
    ).format(
        columns=_identifier_list(list(UPSERT_KEY_DB_COLUMNS)),
        table=sql.Identifier(_validate_table_name(table_name)),
    )
    with conn.cursor() as cur:
        cur.execute(query)
        rows = cur.fetchall()
    return {
        tuple("" if value is None else str(value) for value in row)
        for row in rows
    }


def upsert_rows(
    conn: psycopg.Connection[Any],
    rows: list[dict[str, Any]],
    table_name: str = DEFAULT_TABLE_NAME,
) -> dict[str, Any]:
    table_name = _validate_table_name(table_name)
    normalized_rows = _normalize_rows(rows)
    if not normalized_rows:
        return {
            "table": table_name,
            "rows_seen": 0,
            "rows_written": 0,
            "inserted": 0,
            "updated": 0,
        }

    existing_keys = _load_existing_keys(conn, table_name)
    inserted = 0
    updated = 0

    placeholders = sql.SQL(", ").join(sql.Placeholder() for _ in DB_COLUMNS)
    conflict_target = _identifier_list(list(UPSERT_KEY_DB_COLUMNS))
    update_assignments = sql.SQL(", ").join(
        [
            sql.SQL("{column} = EXCLUDED.{column}").format(column=sql.Identifier(column))
            for column in DB_COLUMNS
        ]
        + [sql.SQL("updated_at = now()")]
    )
    query = sql.SQL(
        """
        INSERT INTO {table} ({columns})
        VALUES ({values})
        ON CONFLICT ({conflict_target})
        DO UPDATE SET {assignments}
        """
    ).format(
        table=sql.Identifier(table_name),
        columns=_identifier_list(DB_COLUMNS),
        values=placeholders,
        conflict_target=conflict_target,
        assignments=update_assignments,
    )

    with conn.cursor() as cur:
        for row in normalized_rows:
            db_payload = [row[csv_column] for csv_column in CSV_COLUMNS]
            key = tuple(
                row[csv_column]
                for csv_column in ("URL", "Plan_Name")
            )
            if key in existing_keys:
                updated += 1
            else:
                inserted += 1
                existing_keys.add(key)
            cur.execute(query, db_payload)
    conn.commit()

    return {
        "table": table_name,
        "rows_seen": len(rows),
        "rows_written": len(normalized_rows),
        "inserted": inserted,
        "updated": updated,
    }


def upsert_csv(
    conn: psycopg.Connection[Any],
    csv_path: Path,
    table_name: str = DEFAULT_TABLE_NAME,
) -> dict[str, Any]:
    header, rows = read_csv_rows(csv_path)
    missing = [column for column in CSV_COLUMNS if column not in header]
    if missing:
        raise RuntimeError(f"CSV missing required columns: {missing}. file={csv_path}")
    summary = upsert_rows(conn, rows, table_name=table_name)
    summary["csv_path"] = str(csv_path.resolve())
    return summary
