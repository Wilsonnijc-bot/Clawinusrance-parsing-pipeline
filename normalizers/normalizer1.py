#!/usr/bin/env python3
"""Normalize first-round insurance_rows JSON artifacts into Insurance_datas.csv."""

from __future__ import annotations

import argparse
import csv
import json
import re
import sys
from datetime import date
from pathlib import Path
from typing import Any

TARGET_COLUMNS: list[str] = [
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

SOURCE_TO_TARGET: dict[str, str] = {
    "Plan_ID": "Plan_ID",
    "Plan_Name": "Plan_Name",
    "Provider_Company": "Provider_Company",
    "URL": "URL",
    "Product_Brochure_route": "Product_Brochure_route",
    "Coverage_Description": "Coverage_Description",
    "Last_Updated": "Last_Updated",
}

ENRICHMENT_COLUMNS = {
    "Plan_category",
    "pricing",
    "age",
    " customer requirement",
    " price structure",
    " additional informations",
}


def normalize_key(text: str) -> str:
    normalized = re.sub(r"[^a-z0-9]+", "_", (text or "").strip().lower())
    return normalized.strip("_")


def canonical_identity(url_value: str, plan_name: str) -> tuple[str, str]:
    return ((url_value or "").strip().lower(), (plan_name or "").strip().lower())


def resolve_header_map(header: list[str]) -> dict[str, str]:
    by_norm = {normalize_key(column): column for column in header}
    resolved: dict[str, str] = {}
    for target_col in TARGET_COLUMNS:
        key = normalize_key(target_col)
        if key in by_norm:
            resolved[target_col] = by_norm[key]
    return resolved


def read_csv_rows(path: Path) -> tuple[list[str], list[dict[str, str]]]:
    if not path.exists():
        return [], []
    with path.open("r", encoding="utf-8-sig", newline="") as fp:
        reader = csv.DictReader(fp)
        header = reader.fieldnames or []
        rows = list(reader)
    return header, rows


def write_csv_rows(path: Path, header: list[str], rows: list[dict[str, str]]) -> None:
    with path.open("w", encoding="utf-8", newline="") as fp:
        writer = csv.DictWriter(fp, fieldnames=header, extrasaction="ignore")
        writer.writeheader()
        for row in rows:
            writer.writerow({column: row.get(column, "") for column in header})


def normalize_text(value: Any) -> str:
    if value is None:
        return ""
    text = str(value).strip()
    if text.lower() in {"", "null", "none", "n/a", "na", "unknown"}:
        return ""
    return text


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Normalize first-round insurance_rows JSON artifacts into Insurance_datas.csv "
            "(manifest-first mode)."
        )
    )
    parser.add_argument("--manifest-file", default=None)
    parser.add_argument("--input-json", nargs="*", default=[])
    parser.add_argument("--input-glob", default="json_outputs/insurance_rows_*_row_*.json")
    parser.add_argument("--source-csv", default=None)
    parser.add_argument("--target-csv", default="Insurance_datas.csv")
    parser.add_argument("--dry-run", action="store_true")
    return parser.parse_args()


def today_iso() -> str:
    return date.today().isoformat()


def resolve_input_files(input_json: list[str], input_glob: str | None) -> list[Path]:
    files: list[Path] = []
    for item in input_json:
        p = Path(item)
        if p.exists() and p.is_file():
            files.append(p)
    if input_glob:
        files.extend(p for p in Path(".").glob(input_glob) if p.is_file())
    dedup: list[Path] = []
    seen: set[str] = set()
    for p in sorted(files, key=lambda x: str(x)):
        key = str(p.resolve())
        if key in seen:
            continue
        seen.add(key)
        dedup.append(p)
    return dedup


def load_json_list(path: Path) -> list[dict[str, Any]]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(payload, list):
        raise RuntimeError(f"Expected JSON list in {path}")
    return [item for item in payload if isinstance(item, dict)]


def collect_artifact_paths_from_manifest(manifest_path: Path) -> list[tuple[Path, str]]:
    try:
        payload = json.loads(manifest_path.read_text(encoding="utf-8"))
    except Exception as exc:
        raise RuntimeError(f"Invalid manifest JSON: {manifest_path} ({exc})") from exc

    if not isinstance(payload, dict):
        raise RuntimeError(f"Manifest must be object: {manifest_path}")
    results = payload.get("results", [])
    if not isinstance(results, list):
        raise RuntimeError(f"Manifest missing results list: {manifest_path}")

    artifacts: list[tuple[Path, str]] = []
    for result in results:
        if not isinstance(result, dict):
            continue
        target = result.get("target", {})
        source_url = ""
        if isinstance(target, dict):
            source_url = str(target.get("site_url", "") or "").strip()

        artifact_paths = result.get("artifact_paths", {})
        if not isinstance(artifact_paths, dict):
            continue
        json_path = str(artifact_paths.get("insurance_rows_json", "") or "").strip()
        if not json_path:
            continue
        path_obj = Path(json_path)
        if path_obj.exists() and path_obj.is_file():
            artifacts.append((path_obj, source_url))
    return artifacts


def collect_source_rows_from_artifacts(
    artifact_sources: list[tuple[Path, str]],
) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for artifact_path, source_url in artifact_sources:
        for row in load_json_list(artifact_path):
            row_copy = dict(row)
            row_copy["_source_url"] = source_url
            row_copy["_artifact_path"] = str(artifact_path)
            rows.append(row_copy)
    return rows


def collect_source_rows_from_csv(source_csv: Path) -> list[dict[str, Any]]:
    header, source_rows = read_csv_rows(source_csv)
    source_map = {normalize_key(col): col for col in header}
    rows: list[dict[str, Any]] = []
    for source_row in source_rows:
        row_dict = {}
        for source_col in SOURCE_TO_TARGET.keys():
            source_real = source_map.get(normalize_key(source_col), "")
            row_dict[source_col] = source_row.get(source_real, "")
        rows.append(row_dict)
    return rows


def main() -> int:
    args = parse_args()
    target_path = Path(args.target_csv)

    source_rows: list[dict[str, Any]] = []
    source_mode = "none"
    artifacts_seen = 0

    if args.manifest_file:
        manifest_path = Path(args.manifest_file)
        artifact_sources = collect_artifact_paths_from_manifest(manifest_path)
        artifacts_seen = len(artifact_sources)
        source_rows = collect_source_rows_from_artifacts(artifact_sources)
        source_mode = "manifest"
    elif args.input_json or (args.input_glob and args.input_glob.strip()):
        input_glob = args.input_glob.strip() if args.input_glob and args.input_glob.strip() else None
        paths = resolve_input_files(args.input_json, input_glob)
        artifact_sources = [(p, "") for p in paths]
        artifacts_seen = len(artifact_sources)
        source_rows = collect_source_rows_from_artifacts(artifact_sources)
        source_mode = "input_json"
    elif args.source_csv:
        source_path = Path(args.source_csv)
        source_rows = collect_source_rows_from_csv(source_path)
        source_mode = "source_csv"
    else:
        raise SystemExit("No input source provided. Use --manifest-file or --input-json/--input-glob.")

    if not source_rows:
        raise SystemExit("No source rows found from first-round artifacts.")

    target_header, target_rows = read_csv_rows(target_path)
    if not target_header:
        target_header = list(TARGET_COLUMNS)
        target_rows = []

    target_header_map = resolve_header_map(target_header)
    missing_required = [
        column for column in TARGET_COLUMNS if normalize_key(column) not in {
            normalize_key(item) for item in target_header
        }
    ]
    if missing_required:
        raise SystemExit(
            f"Target CSV header missing columns: {missing_required}. File: {target_path}"
        )

    existing_index: dict[tuple[str, str], int] = {}
    for idx, row in enumerate(target_rows):
        identity = canonical_identity(
            row.get(target_header_map["URL"], ""),
            row.get(target_header_map["Plan_Name"], ""),
        )
        if identity == ("", ""):
            continue
        existing_index[identity] = idx

    appended = 0
    updated = 0
    skipped = 0

    for source_row in source_rows:
        plan_name = normalize_text(source_row.get("Plan_Name", ""))
        url_value = normalize_text(source_row.get("URL", ""))
        if not url_value:
            url_value = normalize_text(source_row.get("_source_url", ""))
        if not plan_name or not url_value:
            skipped += 1
            continue

        identity = canonical_identity(url_value, plan_name)

        target_payload: dict[str, str] = {}
        for source_col, target_col in SOURCE_TO_TARGET.items():
            target_real = target_header_map[target_col]
            target_payload[target_real] = normalize_text(source_row.get(source_col, ""))

        if not target_payload.get(target_header_map["Last_Updated"], ""):
            target_payload[target_header_map["Last_Updated"]] = today_iso()
        if not target_payload.get(target_header_map["URL"], ""):
            target_payload[target_header_map["URL"]] = url_value

        for enrichment_col in ENRICHMENT_COLUMNS:
            target_payload[target_header_map[enrichment_col]] = ""

        if identity in existing_index:
            row_index = existing_index[identity]
            existing = target_rows[row_index]
            for key, value in target_payload.items():
                if key in {target_header_map[col] for col in ENRICHMENT_COLUMNS}:
                    continue
                existing[key] = value
            updated += 1
        else:
            new_row = {column: "" for column in target_header}
            for key, value in target_payload.items():
                new_row[key] = value
            target_rows.append(new_row)
            existing_index[identity] = len(target_rows) - 1
            appended += 1

    if not args.dry_run:
        write_csv_rows(target_path, target_header, target_rows)

    summary = {
        "status": "ok",
        "source_mode": source_mode,
        "artifacts_seen": artifacts_seen,
        "target_csv": str(target_path.resolve()),
        "source_rows_loaded": len(source_rows),
        "target_rows_after": len(target_rows),
        "appended": appended,
        "updated": updated,
        "skipped": skipped,
        "dry_run": args.dry_run,
    }
    print(json.dumps(summary, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
