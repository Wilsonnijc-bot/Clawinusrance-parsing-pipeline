#!/usr/bin/env python3
"""Run either:
- product-page flow: local product-page URLs -> round 1 -> round 2 -> Supabase
- category-specific flow: local row-shaped CSV -> round 2 -> Supabase
"""

from __future__ import annotations

import argparse
import concurrent.futures
import csv
import json
import os
import shutil
import subprocess
import sys
from datetime import datetime
from pathlib import Path
from urllib.parse import urlparse

from insurance_db import (
    DEFAULT_TABLE_NAME,
    connect,
    ensure_table,
    export_table_to_csv,
    read_csv_row_window,
    require_db_url,
    upsert_rows,
)


def family_key(url_value: str) -> tuple[str, str]:
    parsed = urlparse((url_value or "").strip())
    host = parsed.netloc.lower()
    segments = [segment for segment in parsed.path.split("/") if segment]
    if len(segments) >= 3:
        prefix = "/" + "/".join(segments[:3])
    else:
        prefix = parsed.path.rstrip("/")
    return host, prefix


def load_productpage_rows(csv_path: Path) -> list[dict[str, str | int]]:
    with csv_path.open("r", encoding="utf-8-sig", newline="") as fp:
        reader = csv.DictReader(fp)
        rows: list[dict[str, str | int]] = []
        for row_number, row in enumerate(reader, start=1):
            url_text = str(row.get("url", "") or "").strip()
            if not url_text:
                continue
            count_text = str(row.get("true_product_number", "") or "").strip()
            rows.append(
                {
                    "row_number": reader.line_num,
                    "csv_line": reader.line_num,
                    "data_row_number": row_number,
                    "true_product_number": count_text,
                    "url": url_text,
                }
            )
    return rows


def load_processed_families(insurance_csv_path: Path) -> set[tuple[str, str]]:
    if not insurance_csv_path.exists():
        return set()
    with insurance_csv_path.open("r", encoding="utf-8-sig", newline="") as fp:
        reader = csv.DictReader(fp)
        families: set[tuple[str, str]] = set()
        for row in reader:
            url_text = str(row.get("URL", "") or "").strip()
            if not url_text:
                continue
            families.add(family_key(url_text))
    return families


def count_csv_data_rows(csv_path: Path) -> int:
    if not csv_path.exists():
        return 0
    with csv_path.open("r", encoding="utf-8-sig", newline="") as fp:
        reader = csv.DictReader(fp)
        return sum(1 for _ in reader)


def write_unprocessed_url_csv(path: Path, rows: list[dict[str, str | int]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8", newline="") as fp:
        writer = csv.writer(fp)
        writer.writerow(["row_number", "csv_line", "true_product_number", "url"])
        for row in rows:
            writer.writerow(
                [
                    row.get("row_number", ""),
                    row.get("csv_line", ""),
                    row.get("true_product_number", ""),
                    row.get("url", ""),
                ]
            )


def split_rows_into_parallel_groups(
    rows: list[dict[str, str | int]],
    group_count: int,
) -> list[list[dict[str, str | int]]]:
    if group_count <= 0:
        raise ValueError("group_count must be >= 1")
    if not rows:
        return []

    actual_group_count = min(group_count, len(rows))
    base_size = len(rows) // actual_group_count
    remainder = len(rows) % actual_group_count
    group_sizes = [base_size] * actual_group_count
    for index in range(actual_group_count - remainder, actual_group_count):
        if 0 <= index < actual_group_count:
            group_sizes[index] += 1

    groups: list[list[dict[str, str | int]]] = []
    cursor = 0
    for size in group_sizes:
        group_rows = rows[cursor : cursor + size]
        if group_rows:
            groups.append(group_rows)
        cursor += size
    return groups


def run_step(command: list[str], cwd: Path) -> None:
    print(f"[run] {' '.join(command)}", flush=True)
    proc = subprocess.run(command, cwd=str(cwd), text=True)
    if proc.returncode != 0:
        raise RuntimeError(f"Step failed with exit code {proc.returncode}: {' '.join(command)}")


def run_logged_step(command: list[str], cwd: Path, log_path: Path) -> dict[str, object]:
    log_path.parent.mkdir(parents=True, exist_ok=True)
    print(f"[run] {' '.join(command)}", flush=True)
    with log_path.open("w", encoding="utf-8") as fp:
        proc = subprocess.run(
            command,
            cwd=str(cwd),
            text=True,
            stdout=fp,
            stderr=subprocess.STDOUT,
        )
    return {
        "command": command,
        "returncode": proc.returncode,
        "log_path": str(log_path.resolve()),
    }


def merge_first_round_manifests(
    manifest_paths: list[Path],
    output_path: Path,
    group_count: int,
) -> dict[str, object]:
    payloads: list[dict[str, object]] = []
    for manifest_path in manifest_paths:
        payload = json.loads(manifest_path.read_text(encoding="utf-8"))
        if not isinstance(payload, dict):
            raise RuntimeError(f"Invalid first-round manifest: {manifest_path}")
        payloads.append(payload)

    merged_results: list[dict[str, object]] = []
    generation_failures = 0
    for payload in payloads:
        results = payload.get("results", [])
        if isinstance(results, list):
            merged_results.extend(result for result in results if isinstance(result, dict))
        generation_failures += int(payload.get("generation_failures", 0) or 0)

    merged_results.sort(
        key=lambda result: (
            int(((result.get("target") or {}) if isinstance(result.get("target"), dict) else {}).get("row_number", 0) or 0),
            int(((result.get("target") or {}) if isinstance(result.get("target"), dict) else {}).get("csv_line", 0) or 0),
        )
    )

    merged_manifest: dict[str, object] = {
        "started_at_utc": payloads[0].get("started_at_utc") if payloads else None,
        "finished_at_utc": payloads[-1].get("finished_at_utc") if payloads else None,
        "run_id": output_path.parent.name,
        "parallel_groups": group_count,
        "group_manifests": [str(path.resolve()) for path in manifest_paths],
        "status": "ok" if generation_failures == 0 else "failed",
        "iterations_requested": len(merged_results),
        "iterations_planned": len(merged_results),
        "iterations_completed": len(merged_results),
        "generation_failures": generation_failures,
        "failures": generation_failures,
        "results": merged_results,
    }
    output_path.write_text(json.dumps(merged_manifest, indent=2), encoding="utf-8")
    return merged_manifest


def default_brochure_parsing_json(csv_path: Path) -> Path:
    return Path("json_outputs") / f"{csv_path.stem}_brochure_parsing.json"


def resolve_supabase_table_name(args: argparse.Namespace) -> str:
    if args.supabase_table and str(args.supabase_table).strip():
        return str(args.supabase_table).strip()
    if args.flow == "product-page":
        return DEFAULT_TABLE_NAME
    return Path(args.category_csv).stem


def resolve_codex_bin(args: argparse.Namespace) -> str:
    requested = str(args.codex_bin or "").strip()
    if requested and requested != "codex":
        return requested

    detected = shutil.which(requested or "codex")
    if detected:
        return detected

    shell_path = os.environ.get("SHELL", "/bin/zsh")
    probe = subprocess.run(
        [shell_path, "-lc", f"command -v {requested or 'codex'}"],
        text=True,
        capture_output=True,
    )
    candidate = (probe.stdout or "").strip()
    if probe.returncode == 0 and candidate:
        return candidate

    raise SystemExit(
        "Could not resolve the Codex executable. "
        "Install `codex` on PATH or pass --codex-bin /absolute/path/to/codex."
    )


def resolve_first_round_model(args: argparse.Namespace) -> str | None:
    if args.first_round_model and str(args.first_round_model).strip():
        return str(args.first_round_model).strip()
    return "gpt-5.3-codex"


def resolve_first_round_reasoning_effort(args: argparse.Namespace) -> str | None:
    if args.first_round_reasoning_effort and str(args.first_round_reasoning_effort).strip():
        return str(args.first_round_reasoning_effort).strip()
    return "high"


def resolve_second_round_model(args: argparse.Namespace) -> str | None:
    if args.second_round_model and str(args.second_round_model).strip():
        return str(args.second_round_model).strip()
    return "gpt-5.3-codex"


def resolve_second_round_reasoning_effort(args: argparse.Namespace) -> str | None:
    if args.second_round_reasoning_effort and str(args.second_round_reasoning_effort).strip():
        return str(args.second_round_reasoning_effort).strip()
    return "high"


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Run the product-page flow or the general row-shaped CSV flow."
    )
    parser.add_argument(
        "--flow",
        choices=("product-page", "category-specific"),
        default="product-page",
        help=(
            "product-page: start from listing URLs in productpageurl.csv and run round 1 + round 2. "
            "category-specific: start from an existing row-shaped CSV such as dental_insurance.csv, "
            "car_insurance.csv, or flight_insurance.csv and run round 2 only."
        ),
    )
    parser.add_argument("--productpage-csv", default="productpageurl.csv")
    parser.add_argument(
        "--codex-bin",
        default="codex",
        help="Codex executable for child round-1 and round-2 runs. Defaults to auto-resolved `codex`.",
    )
    parser.add_argument(
        "--insurance-data-csv",
        default="Insurance_data.csv",
        help="Deprecated and ignored. Round 1 now uses JSON artifacts + normalizer1.",
    )
    parser.add_argument(
        "--insurance-datas-csv",
        default="Insurance_datas.csv",
        help="Target CSV for the product-page flow.",
    )
    parser.add_argument(
        "--category-csv",
        default="dental_insurance.csv",
        help="Row-shaped local seed CSV for the category-specific flow.",
    )
    parser.add_argument(
        "--brochure-parsing-json",
        default=None,
        help=(
            "Optional consolidated round-2 output JSON. "
            "Defaults to json_outputs/<target_csv_stem>_brochure_parsing.json."
        ),
    )
    parser.add_argument(
        "--supabase-table",
        default=None,
        help=(
            "Supabase/PostgreSQL table to sync. "
            "Defaults to insurance_products for product-page flow and <category_csv stem> for category-specific flow."
        ),
    )
    parser.add_argument("--first-round-timeout", type=int, default=1800)
    parser.add_argument("--first-round-attempts", type=int, default=3)
    parser.add_argument(
        "--first-round-parallel-groups",
        type=int,
        default=4,
        help="Number of parallel round-1 product-page groups (default: 4). Set 1 for sequential.",
    )
    parser.add_argument(
        "--first-round-model",
        default=None,
        help="Optional model override passed to child round-1 Codex runs.",
    )
    parser.add_argument(
        "--first-round-reasoning-effort",
        default=None,
        help="Optional reasoning effort override passed to child round-1 Codex runs.",
    )
    parser.add_argument("--second-round-attempts", type=int, default=3)
    parser.add_argument("--second-round-timeout", type=int, default=900)
    parser.add_argument(
        "--second-round-model",
        default=None,
        help="Optional model override passed to child round-2 Codex runs.",
    )
    parser.add_argument(
        "--second-round-reasoning-effort",
        default=None,
        help="Optional reasoning effort override passed to child round-2 Codex runs.",
    )
    parser.add_argument("--start-index", type=int, default=1)
    parser.add_argument("--limit", type=int, default=1000000)
    parser.add_argument("--run-dir", default=None)
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--reparse-second-round", action="store_true")
    return parser.parse_args()


def run_product_page_flow(
    args: argparse.Namespace,
    cwd: Path,
    run_dir: Path,
    brochure_parsing_json: Path,
) -> int:
    productpage_csv = Path(args.productpage_csv)
    logical_target_csv = Path(args.insurance_datas_csv)
    working_csv = run_dir / logical_target_csv.name
    db_url = require_db_url()
    table_name = resolve_supabase_table_name(args)
    codex_bin = resolve_codex_bin(args)
    first_round_model = resolve_first_round_model(args)
    first_round_reasoning_effort = resolve_first_round_reasoning_effort(args)
    second_round_model = resolve_second_round_model(args)
    second_round_reasoning_effort = resolve_second_round_reasoning_effort(args)

    with connect(db_url) as conn:
        ensure_table(conn, table_name=table_name)
        export_summary = export_table_to_csv(conn, working_csv, table_name=table_name)

    product_rows = load_productpage_rows(productpage_csv)
    processed_families = load_processed_families(working_csv)
    unprocessed_rows = [
        row for row in product_rows if family_key(str(row["url"])) not in processed_families
    ]

    unprocessed_csv = run_dir / "unprocessed_productpageurl.csv"
    write_unprocessed_url_csv(unprocessed_csv, unprocessed_rows)

    plan = {
        "flow": args.flow,
        "product_urls_total": len(product_rows),
        "unprocessed_urls": len(unprocessed_rows),
        "first_round_parallel_groups": min(args.first_round_parallel_groups, max(1, len(unprocessed_rows))) if unprocessed_rows else 0,
        "codex_bin": codex_bin,
        "first_round_model": first_round_model or "default-from-codex-config",
        "first_round_reasoning_effort": first_round_reasoning_effort or "default-from-codex-config",
        "second_round_model": second_round_model or "default-from-codex-config",
        "second_round_reasoning_effort": second_round_reasoning_effort or "default-from-codex-config",
        "unprocessed_csv": str(unprocessed_csv),
        "logical_target_csv": str(logical_target_csv.resolve()),
        "working_csv": str(working_csv.resolve()),
        "supabase_table": table_name,
        "supabase_rows_exported": export_summary["rows_exported"],
        "brochure_parsing_json": str(brochure_parsing_json.resolve()),
        "run_dir": str(run_dir.resolve()),
    }
    print(json.dumps(plan, indent=2))

    if args.dry_run:
        return 0

    rows_before_round1 = count_csv_data_rows(working_csv)
    first_round_manifest = run_dir / "first_round" / "manifest.json"

    if unprocessed_rows:
        first_round_groups = split_rows_into_parallel_groups(
            unprocessed_rows,
            args.first_round_parallel_groups,
        )
        group_logs_dir = run_dir / "first_round" / "logs"
        manifest_paths: list[Path] = []

        if len(first_round_groups) == 1:
            first_round_cmd = [
                sys.executable,
                "codex_self_loop.py",
                "--codex-bin",
                str(codex_bin),
                "--csv-file",
                str(unprocessed_csv),
                "--iterations",
                str(len(unprocessed_rows)),
                "--max-attempts-per-target",
                str(args.first_round_attempts),
                "--iteration-timeout-seconds",
                str(args.first_round_timeout),
                "--output-dir",
                str(run_dir / "first_round"),
            ]
            if first_round_model:
                first_round_cmd.extend(["--model", str(first_round_model)])
            if first_round_reasoning_effort:
                first_round_cmd.extend(["--model-reasoning-effort", str(first_round_reasoning_effort)])
            run_step(first_round_cmd, cwd=cwd)
        else:
            commands: list[tuple[int, list[str], Path]] = []
            for group_index, group_rows in enumerate(first_round_groups, start=1):
                group_csv = run_dir / "first_round" / f"group_{group_index:02d}.csv"
                group_output_dir = run_dir / "first_round" / f"group_{group_index:02d}"
                group_log = group_logs_dir / f"group_{group_index:02d}.log"
                write_unprocessed_url_csv(group_csv, group_rows)
                manifest_paths.append(group_output_dir / "manifest.json")

                command = [
                    sys.executable,
                    "codex_self_loop.py",
                    "--codex-bin",
                    str(codex_bin),
                    "--csv-file",
                    str(group_csv),
                    "--iterations",
                    str(len(group_rows)),
                    "--max-attempts-per-target",
                    str(args.first_round_attempts),
                    "--iteration-timeout-seconds",
                    str(args.first_round_timeout),
                    "--output-dir",
                    str(group_output_dir),
                ]
                if first_round_model:
                    command.extend(["--model", str(first_round_model)])
                if first_round_reasoning_effort:
                    command.extend(["--model-reasoning-effort", str(first_round_reasoning_effort)])
                commands.append((group_index, command, group_log))

            with concurrent.futures.ThreadPoolExecutor(max_workers=len(commands)) as executor:
                futures = {
                    executor.submit(run_logged_step, command, cwd, log_path): (group_index, log_path)
                    for group_index, command, log_path in commands
                }
                failures: list[str] = []
                for future in concurrent.futures.as_completed(futures):
                    group_index, log_path = futures[future]
                    result = future.result()
                    if int(result["returncode"]) != 0:
                        failures.append(
                            f"group {group_index} failed with exit code {result['returncode']} (log: {log_path})"
                        )
                if failures:
                    raise RuntimeError("First-round parallel group failure(s): " + "; ".join(failures))

            missing_manifests = [path for path in manifest_paths if not path.exists()]
            if missing_manifests:
                raise RuntimeError(f"Missing first-round manifest(s): {missing_manifests}")
            merge_first_round_manifests(
                manifest_paths=manifest_paths,
                output_path=first_round_manifest,
                group_count=len(first_round_groups),
            )
    else:
        print("[skip] first round: no unprocessed productpage URLs", flush=True)

    if first_round_manifest.exists():
        run_step(
            [
                sys.executable,
                "normalizer1.py",
                "--manifest-file",
                str(first_round_manifest),
                "--target-csv",
                str(working_csv),
            ],
            cwd=cwd,
        )
    elif unprocessed_rows:
        raise RuntimeError(f"Missing first-round manifest: {first_round_manifest}")
    else:
        print("[skip] normalizer1: no first-round run in this execution", flush=True)

    rows_after_round1 = count_csv_data_rows(working_csv)
    newly_added_rows = max(0, rows_after_round1 - rows_before_round1)
    round2_start_index = rows_before_round1 + 1

    round2_skipped = False
    if not args.reparse_second_round and newly_added_rows == 0:
        round2_skipped = True
        print(
            "[skip] second round: no newly added target rows after round 1",
            flush=True,
        )
    else:
        second_round_cmd = [
            sys.executable,
            "brochure_round2_loop.py",
            "--codex-bin",
            str(codex_bin),
            "--insurance-csv",
            str(working_csv),
            "--productpage-csv",
            str(productpage_csv),
            "--output-json",
            str(brochure_parsing_json),
            "--max-attempts-per-row",
            str(args.second_round_attempts),
            "--timeout-seconds",
            str(args.second_round_timeout),
            "--run-dir",
            str(run_dir / "second_round"),
        ]
        if second_round_model:
            second_round_cmd.extend(["--model", str(second_round_model)])
        if second_round_reasoning_effort:
            second_round_cmd.extend(["--model-reasoning-effort", str(second_round_reasoning_effort)])
        if args.reparse_second_round:
            second_round_cmd.append("--reparse-all")
        else:
            second_round_cmd.extend(
                [
                    "--start-index",
                    str(round2_start_index),
                    "--limit",
                    str(newly_added_rows),
                ]
            )

        run_step(second_round_cmd, cwd=cwd)

    if round2_skipped:
        print("[skip] normalizer2: round 2 skipped", flush=True)
    else:
        normalizer2_cmd = [
            sys.executable,
            "normalizer2.py",
            "--csv-file",
            str(working_csv),
        ]

        if args.reparse_second_round:
            normalizer2_cmd.extend(
                [
                    "--brochure-parsing-json",
                    str(brochure_parsing_json),
                ]
            )
        else:
            row_artifacts: list[str] = []
            for row_number in range(round2_start_index, rows_after_round1 + 1):
                artifact = cwd / "json_outputs" / f"brochure_fields_row_{row_number}.json"
                if artifact.exists() and artifact.is_file():
                    row_artifacts.append(str(artifact))

            if not row_artifacts:
                print(
                    "[skip] normalizer2: no row-scoped brochure artifacts for newly added rows",
                    flush=True,
                )
                round2_skipped = True
            else:
                normalizer2_cmd.extend(
                    [
                        "--brochure-parsing-json",
                        "",
                        "--input-glob",
                        "",
                        "--input-json",
                        *row_artifacts,
                    ]
                )

        if not round2_skipped:
            run_step(normalizer2_cmd, cwd=cwd)

    rows_to_sync: list[dict[str, str]] = []
    if args.reparse_second_round:
        rows_to_sync = read_csv_row_window(
            working_csv,
            start_row=1,
            end_row=count_csv_data_rows(working_csv),
        )
    elif newly_added_rows > 0 and rows_after_round1 >= round2_start_index:
        rows_to_sync = read_csv_row_window(
            working_csv,
            start_row=round2_start_index,
            end_row=rows_after_round1,
        )

    sync_summary: dict[str, object] = {
        "rows_seen": len(rows_to_sync),
        "rows_written": 0,
        "inserted": 0,
        "updated": 0,
    }
    if rows_to_sync:
        with connect(db_url) as conn:
            ensure_table(conn, table_name=table_name)
            sync_summary = upsert_rows(conn, rows_to_sync, table_name=table_name)

    print(
        json.dumps(
            {
                "status": "ok",
                "flow": args.flow,
                "run_dir": str(run_dir.resolve()),
                "logical_target_csv": str(logical_target_csv.resolve()),
                "working_csv": str(working_csv.resolve()),
                "supabase_table": table_name,
                "brochure_parsing_json": str(brochure_parsing_json.resolve()),
                "rows_before_round1": rows_before_round1,
                "rows_after_round1": rows_after_round1,
                "newly_added_rows": newly_added_rows,
                "supabase_sync": sync_summary,
                "round2_row_window": (
                    "all_rows"
                    if args.reparse_second_round
                    else (
                        f"{round2_start_index}-{rows_after_round1}"
                        if rows_after_round1 >= round2_start_index
                        else "none"
                    )
                ),
            },
            indent=2,
        )
    )
    return 0


def run_category_specific_flow(
    args: argparse.Namespace,
    cwd: Path,
    run_dir: Path,
    brochure_parsing_json: Path,
) -> int:
    category_csv = Path(args.category_csv)
    working_csv = run_dir / category_csv.name
    table_name = resolve_supabase_table_name(args)
    codex_bin = resolve_codex_bin(args)
    second_round_model = resolve_second_round_model(args)
    second_round_reasoning_effort = resolve_second_round_reasoning_effort(args)
    rows_total = count_csv_data_rows(category_csv)
    selected_end = args.start_index - 1 + args.limit
    selected_window = f"{args.start_index}-{min(rows_total, selected_end)}" if rows_total else "none"

    plan = {
        "flow": args.flow,
        "category_csv": str(category_csv.resolve()),
        "working_csv": str(working_csv.resolve()),
        "rows_total": rows_total,
        "row_window": "all_rows" if args.reparse_second_round else selected_window,
        "supabase_table": table_name,
        "codex_bin": codex_bin,
        "second_round_model": second_round_model or "default-from-codex-config",
        "second_round_reasoning_effort": second_round_reasoning_effort or "default-from-codex-config",
        "brochure_parsing_json": str(brochure_parsing_json.resolve()),
        "run_dir": str(run_dir.resolve()),
    }
    print(json.dumps(plan, indent=2))

    if args.dry_run:
        return 0

    if not category_csv.exists():
        raise SystemExit(f"CSV file not found: {category_csv}")

    db_url = require_db_url()
    shutil.copyfile(category_csv, working_csv)

    second_round_cmd = [
        sys.executable,
        "brochure_round2_loop.py",
        "--codex-bin",
        str(codex_bin),
        "--insurance-csv",
        str(working_csv),
        "--productpage-csv",
        str(Path(args.productpage_csv)),
        "--output-json",
        str(brochure_parsing_json),
        "--max-attempts-per-row",
        str(args.second_round_attempts),
        "--timeout-seconds",
        str(args.second_round_timeout),
        "--run-dir",
        str(run_dir / "second_round"),
    ]
    if second_round_model:
        second_round_cmd.extend(["--model", str(second_round_model)])
    if second_round_reasoning_effort:
        second_round_cmd.extend(["--model-reasoning-effort", str(second_round_reasoning_effort)])
    if args.reparse_second_round:
        second_round_cmd.append("--reparse-all")
    else:
        second_round_cmd.extend(
            [
                "--start-index",
                str(args.start_index),
                "--limit",
                str(args.limit),
            ]
        )

    run_step(second_round_cmd, cwd=cwd)

    run_step(
        [
            sys.executable,
            "normalizer2.py",
            "--csv-file",
            str(working_csv),
            "--brochure-parsing-json",
            str(brochure_parsing_json),
        ],
        cwd=cwd,
    )

    if args.reparse_second_round:
        rows_to_sync = read_csv_row_window(
            working_csv,
            start_row=1,
            end_row=count_csv_data_rows(working_csv),
        )
    else:
        rows_to_sync = read_csv_row_window(
            working_csv,
            start_row=args.start_index,
            end_row=min(rows_total, selected_end),
        )

    sync_summary: dict[str, object] = {
        "rows_seen": len(rows_to_sync),
        "rows_written": 0,
        "inserted": 0,
        "updated": 0,
    }
    if rows_to_sync:
        with connect(db_url) as conn:
            ensure_table(conn, table_name=table_name)
            sync_summary = upsert_rows(conn, rows_to_sync, table_name=table_name)

    print(
        json.dumps(
            {
                "status": "ok",
                "flow": args.flow,
                "run_dir": str(run_dir.resolve()),
                "target_csv": str(category_csv.resolve()),
                "working_csv": str(working_csv.resolve()),
                "supabase_table": table_name,
                "brochure_parsing_json": str(brochure_parsing_json.resolve()),
                "row_window": "all_rows" if args.reparse_second_round else selected_window,
                "supabase_sync": sync_summary,
            },
            indent=2,
        )
    )
    return 0


def main() -> int:
    args = parse_args()
    cwd = Path.cwd()

    if args.start_index <= 0:
        raise SystemExit("--start-index must be >= 1")
    if args.limit <= 0:
        raise SystemExit("--limit must be >= 1")
    if args.first_round_parallel_groups <= 0:
        raise SystemExit("--first-round-parallel-groups must be >= 1")

    run_stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    run_prefix = "product_page_flow" if args.flow == "product-page" else "category_specific_flow"
    run_dir = Path(args.run_dir) if args.run_dir else Path("/tmp/codex_runs") / f"{run_prefix}_{run_stamp}"
    run_dir.mkdir(parents=True, exist_ok=True)

    target_csv_for_flow = (
        Path(args.insurance_datas_csv) if args.flow == "product-page" else Path(args.category_csv)
    )
    brochure_parsing_json = (
        Path(args.brochure_parsing_json)
        if args.brochure_parsing_json and str(args.brochure_parsing_json).strip()
        else default_brochure_parsing_json(target_csv_for_flow)
    )
    brochure_parsing_json.parent.mkdir(parents=True, exist_ok=True)

    if args.flow == "product-page":
        return run_product_page_flow(
            args=args,
            cwd=cwd,
            run_dir=run_dir,
            brochure_parsing_json=brochure_parsing_json,
        )

    return run_category_specific_flow(
        args=args,
        cwd=cwd,
        run_dir=run_dir,
        brochure_parsing_json=brochure_parsing_json,
    )


if __name__ == "__main__":
    sys.exit(main())
