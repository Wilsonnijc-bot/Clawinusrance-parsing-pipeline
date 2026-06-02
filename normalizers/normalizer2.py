#!/usr/bin/env python3
"""Normalize brochure parsing JSON rows into dental_insurance.csv."""

from __future__ import annotations

import argparse
import csv
import json
import re
import sys
from pathlib import Path
from typing import Any

ROW_FILENAME_PATTERN = re.compile(r"brochure_fields_row_(\d+)\.json$", re.IGNORECASE)
ROW_SECTION_PATTERN = re.compile(r"row\s+(\d+)", re.IGNORECASE)

TARGET_FIELD_HINTS: dict[str, str] = {
    "plan_category": "Plan_category",
    "coverage_description": "Coverage_Description",
    "pricing": "pricing",
    "age": "age",
    "customer_requirement": "customer requirement",
    "price_structure": "price structure",
    "additional_informations": "additional informations",
}

KEY_ALIASES: dict[str, str] = {
    "plan_category": "plan_category",
    "plan_categorys": "plan_category",
    "plancategory": "plan_category",
    "coverage_description": "coverage_description",
    "coveragedescription": "coverage_description",
    "coverage": "coverage_description",
    "pricing": "pricing",
    "price": "pricing",
    "age": "age",
    "customer_requirement": "customer_requirement",
    "customer_requirements": "customer_requirement",
    "customerrequirement": "customer_requirement",
    "price_structure": "price_structure",
    "pricestructure": "price_structure",
    "additional_informations": "additional_informations",
    "additional_information": "additional_informations",
    "additionalinformation": "additional_informations",
}

NULL_LIKE = {"", "null", "none", "n/a", "na", "unknown", "not available", "not found"}


def normalize_key(text: str) -> str:
    normalized = re.sub(r"[^a-z0-9]+", "_", text.strip().lower())
    return re.sub(r"_+", "_", normalized).strip("_")


def normalize_value(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, bool):
        return "TRUE" if value else "FALSE"
    if isinstance(value, (int, float)):
        return str(value)
    if isinstance(value, list):
        parts = [normalize_value(item) for item in value]
        parts = [part for part in parts if part]
        return "; ".join(parts)
    if isinstance(value, dict):
        return json.dumps(value, ensure_ascii=False, sort_keys=True)

    text = str(value).strip()
    if text.lower() in NULL_LIKE:
        return ""
    return text


def resolve_target_headers(header: list[str]) -> dict[str, str]:
    header_by_normalized = {normalize_key(column): column for column in header}
    resolved: dict[str, str] = {}
    missing: list[str] = []

    for canonical_key, header_hint in TARGET_FIELD_HINTS.items():
        normalized_hint = normalize_key(header_hint)
        header_name = header_by_normalized.get(normalized_hint)
        if header_name is None:
            missing.append(header_hint)
            continue
        resolved[canonical_key] = header_name

    if missing:
        missing_text = ", ".join(missing)
        raise RuntimeError(f"CSV header missing required brochure columns: {missing_text}")
    return resolved


def parse_row_number(path: Path) -> int:
    match = ROW_FILENAME_PATTERN.search(path.name)
    if not match:
        raise RuntimeError(
            f"Filename must match brochure_fields_row_{{row_number}}.json: {path.name}"
        )
    return int(match.group(1))


def parse_row_number_from_section(section_name: str) -> int | None:
    match = ROW_SECTION_PATTERN.search(section_name or "")
    if not match:
        return None
    return int(match.group(1))


def load_brochure_payload(path: Path) -> dict[str, Any]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except Exception as exc:
        raise RuntimeError(f"Invalid JSON in {path}: {exc}") from exc

    if isinstance(payload, list) and len(payload) == 1 and isinstance(payload[0], dict):
        payload = payload[0]

    if not isinstance(payload, dict):
        raise RuntimeError(f"Expected JSON object in {path}")
    return payload


def canonicalize_payload_keys(payload: dict[str, Any]) -> dict[str, str]:
    normalized: dict[str, str] = {}
    for raw_key, raw_value in payload.items():
        key_norm = normalize_key(str(raw_key))
        canonical = KEY_ALIASES.get(key_norm)
        if canonical is None:
            continue
        normalized[canonical] = normalize_value(raw_value)
    return normalized


def load_rows_from_brochure_parsing_json(path: Path) -> list[tuple[int, dict[str, Any], str]]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except Exception as exc:
        raise RuntimeError(f"Invalid JSON in {path}: {exc}") from exc

    if not isinstance(payload, dict):
        raise RuntimeError(f"Expected top-level object in {path}")

    entries: list[tuple[int, dict[str, Any], str]] = []

    # New flat format:
    # { "# ========================= Row N =========================": { ... } }
    for row_section, row_payload in payload.items():
        row_number = parse_row_number_from_section(str(row_section))
        if row_number is None:
            continue
        if not isinstance(row_payload, dict):
            continue
        entries.append((row_number, row_payload, f"{path}:{row_section}"))

    # Backward compatibility for old nested format:
    # { "Url section": { "Row section": { ... } } }
    if not entries:
        for top_section, section_body in payload.items():
            if not isinstance(section_body, dict):
                continue
            for row_section, row_payload in section_body.items():
                row_number = parse_row_number_from_section(str(row_section))
                if row_number is None:
                    continue
                if not isinstance(row_payload, dict):
                    continue
                entries.append((row_number, row_payload, f"{path}:{top_section}/{row_section}"))

    if not entries:
        raise RuntimeError(f"No row sections found in {path}")

    entries.sort(key=lambda item: item[0])
    return entries


def load_csv_rows(csv_path: Path) -> tuple[list[str], list[dict[str, str]]]:
    if not csv_path.exists():
        raise RuntimeError(f"CSV file not found: {csv_path}")

    with csv_path.open("r", encoding="utf-8-sig", newline="") as fp:
        reader = csv.DictReader(fp)
        fieldnames = reader.fieldnames or []
        if not fieldnames:
            raise RuntimeError(f"CSV has no header: {csv_path}")
        rows = list(reader)

    return fieldnames, rows


def write_csv_rows(csv_path: Path, header: list[str], rows: list[dict[str, str]]) -> None:
    with csv_path.open("w", encoding="utf-8", newline="") as fp:
        writer = csv.DictWriter(fp, fieldnames=header, extrasaction="ignore")
        writer.writeheader()
        for row in rows:
            writer.writerow({column: row.get(column, "") for column in header})


def resolve_input_files(input_json: list[str], input_glob: str | None) -> list[Path]:
    files: list[Path] = []
    for path_str in input_json:
        path = Path(path_str)
        if path.exists() and path.is_file():
            files.append(path)

    if input_glob:
        files.extend(path for path in Path(".").glob(input_glob) if path.is_file())

    deduped: list[Path] = []
    seen: set[str] = set()
    for path in sorted(files, key=lambda p: str(p)):
        key = str(path.resolve())
        if key in seen:
            continue
        seen.add(key)
        deduped.append(path)

    return deduped


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Normalize brochure_fields artifacts and update dental_insurance.csv in place."
    )
    parser.add_argument(
        "--csv-file",
        default="dental_insurance.csv",
        help="Target CSV file to update in place.",
    )
    parser.add_argument(
        "--input-json",
        nargs="*",
        default=[],
        help="Specific brochure_fields JSON files to process.",
    )
    parser.add_argument(
        "--input-glob",
        default="json_outputs/brochure_fields_row_*.json",
        help="Glob pattern for brochure field artifacts (set empty to disable).",
    )
    parser.add_argument(
        "--brochure-parsing-json",
        default="json_outputs/brochure_parsing.json",
        help=(
            "Primary consolidated JSON to read row sections from. "
            "If found, this is used before --input-json/--input-glob."
        ),
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Validate and report updates without writing CSV changes.",
    )
    parser.add_argument(
        "--continue-on-error",
        action="store_true",
        help="Continue processing remaining files when one file fails.",
    )
    return parser.parse_args()


def main() -> int:
    args = parse_args()

    csv_path = Path(args.csv_file)
    header, rows = load_csv_rows(csv_path)
    target_headers = resolve_target_headers(header)

    row_payloads: list[tuple[int, dict[str, Any], str]] = []
    if args.brochure_parsing_json and str(args.brochure_parsing_json).strip():
        consolidated_path = Path(args.brochure_parsing_json)
        if consolidated_path.exists():
            row_payloads = load_rows_from_brochure_parsing_json(consolidated_path)

    artifact_files: list[Path] = []
    if not row_payloads:
        input_glob = args.input_glob.strip() if args.input_glob and args.input_glob.strip() else None
        artifact_files = resolve_input_files(args.input_json, input_glob)
        if not artifact_files:
            raise SystemExit(
                "No brochure row data found. Provide --brochure-parsing-json or --input-json/--input-glob."
            )
        for path in artifact_files:
            row_number = parse_row_number(path)
            payload = load_brochure_payload(path)
            row_payloads.append((row_number, payload, str(path)))

    failures = 0
    updated_rows = 0
    updated_cells = 0
    processed_rows = 0

    for row_number, payload, source_label in row_payloads:
        try:
            row_index = row_number - 1
            if row_index < 0 or row_index >= len(rows):
                raise RuntimeError(
                    f"Row number {row_number} out of range for {csv_path} ({len(rows)} data rows)."
                )

            normalized_fields = canonicalize_payload_keys(payload)
            if not normalized_fields:
                raise RuntimeError(
                    "No supported brochure keys found in artifact. "
                    "Expected one or more of: "
                    "plan_category, pricing, age, customer_requirement, "
                    "price_structure, additional_informations."
                )

            row = rows[row_index]
            row_cell_updates = 0
            for canonical_key, value in normalized_fields.items():
                header_name = target_headers.get(canonical_key)
                if header_name is None:
                    continue
                if row.get(header_name, "") != value:
                    row[header_name] = value
                    row_cell_updates += 1

            processed_rows += 1
            if row_cell_updates > 0:
                updated_rows += 1
                updated_cells += row_cell_updates
            print(
                f"source={source_label} row_number={row_number} updated_cells={row_cell_updates}",
                flush=True,
            )
        except Exception as exc:
            failures += 1
            print(f"source={source_label} error={exc}", file=sys.stderr, flush=True)
            if not args.continue_on_error:
                break

    if not args.dry_run and (updated_rows > 0 or failures == 0):
        write_csv_rows(csv_path, header, rows)

    summary = {
        "status": "ok" if failures == 0 else "failed",
        "csv_file": str(csv_path.resolve()),
        "dry_run": args.dry_run,
        "rows_discovered": len(row_payloads),
        "rows_processed": processed_rows,
        "failures": failures,
        "updated_rows": updated_rows,
        "updated_cells": updated_cells,
    }
    print(json.dumps(summary, indent=2))

    return 0 if failures == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
