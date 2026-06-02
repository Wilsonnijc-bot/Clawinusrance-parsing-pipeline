#!/usr/bin/env python3
"""
Run Codex repeatedly with a file-backed model instructions prompt.

This script shells out to `codex exec` for each iteration and validates
required insurance extraction artifacts.
"""

from __future__ import annotations

import argparse
import csv
import hashlib
import json
import re
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

INSURANCE_COLUMNS: list[str] = [
    "Plan_ID",
    "Plan_Name",
    "Provider_Company",
    "Monthly_Premium_Min",
    "Monthly_Premium_Max",
    "Deductible_Range",
    "Out_of_Pocket_Max_Est",
    "Cost_Tier",
    "Copays_Description",
    "Coverage_Description",
    "Network_Type",
    "Geographic_Coverage",
    "Campus_Compatible",
    "Min_Age",
    "Max_Age",
    "Student_Eligible",
    "International_Student_Compatible",
    "Overall_Rating",
    "Plan_Description",
    "URL",
    "Last_Updated",
    "Product_Brochure_route",
]

URL_COLUMN_ALIASES = [
    "site_url",
    "url",
    "urls",
    "website_url",
    "website",
    "product_url",
    "product_page_url",
    "product_page",
    "landing_url",
]

COUNT_COLUMN_ALIASES = [
    "true_product_number",
    "true_products_number",
    "expected_product_count",
    "expected_products_count",
    "product_count",
    "products_count",
]

ROW_NUMBER_COLUMN_ALIASES = [
    "row_number",
    "source_row_number",
    "original_row_number",
]

CSV_LINE_COLUMN_ALIASES = [
    "csv_line",
    "source_csv_line",
    "original_csv_line",
]

DIAGNOSTIC_PREFIXES = (
    "index_error",
    "requests_index_error",
    "playwright_index_error",
    "listing_fetch_error",
    "initialized_pending_parser_run",
    "index_empty_no_products_found",
    "extraction failed",
)


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def sha256_hex(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def read_text_file(path: Path, label: str) -> str:
    try:
        return path.read_text(encoding="utf-8")
    except FileNotFoundError as exc:
        raise SystemExit(f"{label} not found: {path}") from exc


def as_text(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, bytes):
        return value.decode("utf-8", errors="replace")
    return str(value)


def looks_like_url(value: str) -> bool:
    return bool(re.match(r"^https?://", value.strip(), flags=re.IGNORECASE))


def site_slug(site_url: str) -> str:
    parsed = urlparse(site_url)
    host = (parsed.netloc or parsed.path or "site").lower()
    host = host.split("@")[-1].split(":")[0]
    if host.startswith("www."):
        host = host[4:]
    slug = re.sub(r"[^a-z0-9]+", "_", host).strip("_")
    return slug or "site"


def canonical_url_for_compare(url_value: str) -> str:
    raw = str(url_value or "").strip()
    if not raw:
        return ""
    parsed = urlparse(raw)
    scheme = (parsed.scheme or "https").lower()
    host = (parsed.netloc or "").lower()
    path = parsed.path.rstrip("/")
    return f"{scheme}://{host}{path}"


def _normalize_colname(name: str) -> str:
    normalized = re.sub(r"[^a-z0-9]+", "_", name.strip().lower())
    return normalized.strip("_")


def _parse_optional_int(value: Any) -> int | None:
    if value is None:
        return None
    text = str(value).strip()
    if not text:
        return None
    cleaned = text.replace(",", "")
    if re.fullmatch(r"[+-]?\d+", cleaned):
        parsed = int(cleaned)
        return parsed if parsed >= 0 else None
    if re.fullmatch(r"[+-]?\d+\.0+", cleaned):
        parsed = int(float(cleaned))
        return parsed if parsed >= 0 else None
    return None


def expected_artifacts(row_number: int, site_url: str) -> dict[str, Path]:
    slug = site_slug(site_url)
    return {
        "parser_py": Path("parser_outputs") / f"parser_{slug}_row_{row_number}.py",
        "insurance_rows_json": Path("json_outputs") / f"insurance_rows_{slug}_row_{row_number}.json",
    }


def remove_stale_artifacts(artifacts: dict[str, Path]) -> None:
    for path in artifacts.values():
        if path.exists() and path.is_file():
            path.unlink()


def cleanup_row_extras(row_number: int, site_url: str) -> None:
    slug = site_slug(site_url)
    extra_files = [
        Path(f"validation_{slug}_row_{row_number}.json"),
        Path(f"jobs_{slug}_row_{row_number}.json"),
    ]
    for path in extra_files:
        if path.exists() and path.is_file():
            path.unlink()


def load_json_list(path: Path) -> list[Any]:
    if not path.exists():
        raise RuntimeError(f"Missing file: {path}")
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except Exception as exc:
        raise RuntimeError(f"Invalid JSON in {path}: {exc}") from exc
    if not isinstance(payload, list):
        raise RuntimeError(f"Expected top-level JSON list in {path}")
    return payload


def validate_artifacts(
    artifacts: dict[str, Path],
    expected_count: int | None = None,
    source_url: str | None = None,
) -> dict[str, Any]:
    parser_path = artifacts["parser_py"]
    rows_path = artifacts["insurance_rows_json"]

    parser_exists = parser_path.is_file() and parser_path.stat().st_size > 0
    parser_syntax_ok = False
    parser_error = ""
    if parser_exists:
        parser_check = subprocess.run(
            [sys.executable, "-m", "py_compile", str(parser_path)],
            capture_output=True,
            text=True,
        )
        parser_syntax_ok = parser_check.returncode == 0
        parser_error = (parser_check.stderr or "").strip()

    insurance_rows_exists = rows_path.is_file() and rows_path.stat().st_size > 0
    insurance_rows_json_valid = False
    insurance_rows_is_list = False
    insurance_rows_count = 0
    insurance_rows_schema_ok = False
    insurance_rows_expected_count_ok = expected_count is None
    insurance_rows_brochure_routes_absolute = True
    insurance_rows_relative_brochure_count = 0
    insurance_rows_listing_fallback_only = False
    insurance_rows_diagnostic_only = False
    insurance_rows_error = ""

    if insurance_rows_exists:
        try:
            insurance_data = json.loads(rows_path.read_text(encoding="utf-8"))
            insurance_rows_json_valid = True
            insurance_rows_is_list = isinstance(insurance_data, list)
            if insurance_rows_is_list:
                insurance_rows_count = len(insurance_data)
                insurance_rows_schema_ok = all(
                    isinstance(item, dict)
                    and bool(str(item.get("Plan_Name", "") or "").strip())
                    and bool(str(item.get("URL", "") or "").strip())
                    for item in insurance_data
                )
                insurance_rows_expected_count_ok = (
                    expected_count is None or insurance_rows_count == expected_count
                )
                dict_rows = [item for item in insurance_data if isinstance(item, dict)]
                if dict_rows:
                    invalid_brochure_count = 0
                    for item in dict_rows:
                        brochure_raw = item.get("Product_Brochure_route")
                        if brochure_raw is None:
                            continue
                        brochure_text = str(brochure_raw).strip()
                        if not brochure_text or brochure_text.lower() == "null":
                            continue
                        if not looks_like_url(brochure_text):
                            invalid_brochure_count += 1
                    insurance_rows_relative_brochure_count = invalid_brochure_count
                    insurance_rows_brochure_routes_absolute = invalid_brochure_count == 0
                    source_canonical = canonical_url_for_compare(source_url or "")
                    if (
                        insurance_rows_count == 1
                        and source_canonical
                        and canonical_url_for_compare(str(dict_rows[0].get("URL", "") or ""))
                        == source_canonical
                        and not bool(str(dict_rows[0].get("Plan_Name", "") or "").strip())
                    ):
                        insurance_rows_listing_fallback_only = True
                    insurance_rows_diagnostic_only = all(
                        str(item.get("Plan_Description", "") or "").strip().lower().startswith(
                            DIAGNOSTIC_PREFIXES
                        )
                        for item in dict_rows
                    )
        except Exception as exc:
            insurance_rows_error = str(exc)

    issues: list[str] = []
    if not parser_exists:
        issues.append(f"Missing file: {parser_path}")
    elif not parser_syntax_ok:
        issues.append(f"Python syntax invalid: {parser_path} ({parser_error[:200]})")

    if not insurance_rows_exists:
        issues.append(f"Missing file: {rows_path}")
    elif not insurance_rows_json_valid:
        issues.append(f"Invalid JSON: {rows_path} ({insurance_rows_error[:200]})")
    elif not insurance_rows_is_list:
        issues.append(f"insurance rows JSON must be a list: {rows_path}")
    elif insurance_rows_count == 0:
        issues.append(f"insurance rows JSON is empty: {rows_path}")
    elif not insurance_rows_schema_ok:
        issues.append(
            f"insurance rows schema check failed: {rows_path} "
            "(each item must include non-empty Plan_Name and URL)"
        )
    elif not insurance_rows_expected_count_ok:
        issues.append(
            "insurance rows count mismatch: "
            f"expected={expected_count} actual={insurance_rows_count}"
        )
    elif not insurance_rows_brochure_routes_absolute:
        issues.append(
            "Product_Brochure_route must be absolute URL for all non-null rows "
            f"(invalid_count={insurance_rows_relative_brochure_count})"
        )
    elif insurance_rows_listing_fallback_only:
        issues.append(
            f"insurance rows appear diagnostic fallback only: single row mapped to listing URL in {rows_path}"
        )
    elif insurance_rows_diagnostic_only:
        issues.append(
            f"insurance rows appear diagnostic-only: Plan_Description markers detected in {rows_path}"
        )

    return {
        "success": len(issues) == 0,
        "parser_exists": parser_exists,
        "parser_syntax_ok": parser_syntax_ok,
        "parser_error": parser_error,
        "insurance_rows_exists": insurance_rows_exists,
        "insurance_rows_json_valid": insurance_rows_json_valid,
        "insurance_rows_is_list": insurance_rows_is_list,
        "insurance_rows_count": insurance_rows_count,
        "insurance_rows_schema_ok": insurance_rows_schema_ok,
        "insurance_rows_expected_count": expected_count,
        "insurance_rows_expected_count_ok": insurance_rows_expected_count_ok,
        "insurance_rows_brochure_routes_absolute": insurance_rows_brochure_routes_absolute,
        "insurance_rows_relative_brochure_count": insurance_rows_relative_brochure_count,
        "insurance_rows_listing_fallback_only": insurance_rows_listing_fallback_only,
        "insurance_rows_diagnostic_only": insurance_rows_diagnostic_only,
        "insurance_rows_error": insurance_rows_error,
        "issues": issues,
        "artifact_paths": {k: str(v) for k, v in artifacts.items()},
    }


def build_attempt_prompt(
    base_prompt: str,
    row_number: int,
    attempt: int,
    max_attempts: int,
    artifacts: dict[str, Path],
    previous_issues: list[str] | None = None,
) -> str:
    fields = ", ".join(INSURANCE_COLUMNS)
    artifact_contract = (
        "Execution contract for this attempt:\n"
        f"- Required files: {artifacts['parser_py']} and {artifacts['insurance_rows_json']}\n"
        "- Write these files first before long exploration.\n"
        "- insurance rows JSON must be a non-empty list.\n"
        "- Each JSON item must represent one real product and include non-empty Plan_Name + URL.\n"
        f"- Required JSON keys for each product: {fields}\n"
        "- For missing values, write null (do not infer).\n"
        "- Output must contain real product rows, not diagnostic/placeholder rows.\n"
        "- If true_product_number is provided in the prompt, output row count must match it exactly.\n"
        "- Product_Brochure_route must be absolute URL (same style as prior absolute rows), not /content/...\n"
        "- Strategy order: endpoint/model JSON first; HTML/DOM heuristics only as fallback.\n"
        "- Do not switch to other websites.\n"
        "- Do not create additional project-root files beyond the two required files.\n"
        "- Prefer local file edits and finish quickly."
    )

    retry_note = ""
    if previous_issues:
        joined = "\n".join(f"- {issue}" for issue in previous_issues)
        retry_note = (
            f"\n\nPrevious attempt issues (attempt {attempt - 1}):\n"
            f"{joined}\n"
            "Fix only these issues now."
        )

    return (
        f"{base_prompt}\n\n"
        f"{artifact_contract}{retry_note}\n\n"
        f"Attempt {attempt}/{max_attempts}. "
        f"Reply exactly: DONE row {row_number} attempt {attempt}"
    )


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Loop Codex calls using system_prompt.txt as model instructions. "
            "By default, each child run is assigned one insurance product page URL from CSV."
        )
    )
    parser.add_argument(
        "--system-prompt-file",
        default="prompts/system_prompt.txt",
        help="Path to the file used as Codex model instructions.",
    )
    parser.add_argument(
        "--csv-file",
        default="productpageurl.csv",
        help="Input file containing insurance product page URLs.",
    )
    parser.add_argument(
        "--site-column",
        help="Column name for product page URLs when --csv-file has a header.",
    )
    parser.add_argument(
        "--insurance-data-file",
        default="",
        help="Deprecated and ignored. Round 1 now outputs JSON artifacts only.",
    )
    parser.add_argument(
        "--start-index",
        type=int,
        default=1,
        help="1-based index into detected URL targets.",
    )
    parser.add_argument(
        "--user-prompt",
        default=(
            "Run {iteration}/{total}. Work on exactly one insurance product page URL.\n"
            "- csv_file: {csv_file}\n"
            "- csv_line: {csv_line}\n"
            "- product_page_url: {site_url}\n"
            "- true_product_number: {true_product_number}\n"
            "- expected_output_json: json_outputs/insurance_rows_{site_slug}_row_{row_number}.json\n\n"
            "Rules:\n"
            "1) Build parser code only for this product page URL.\n"
            "2) Do not switch to other websites.\n"
            "3) Use api_configgg.py for API URL and key source.\n"
            "4) Produce row-scoped outputs: parser_outputs/parser_{site_slug}_row_{row_number}.py and json_outputs/insurance_rows_{site_slug}_row_{row_number}.json.\n"
            "5) insurance_rows JSON must be a list where each item is one product listed on the product page.\n"
            "6) Use exactly these fields per row: {insurance_columns}.\n"
            "7) Missing fields must be null in JSON (no guessing).\n"
            "8) Endpoint-first workflow: discover and use model/API endpoint first (for example data-search-endpoint or *.model.json).\n"
            "9) Use HTML/DOM heuristics only if endpoint path is unavailable."
        ),
        help=(
            "User prompt template. Supported placeholders: "
            "{iteration}, {total}, {csv_file}, {site_url}, {site_slug}, "
            "{row_number}, {csv_line}, {site_column}, {insurance_columns}, {true_product_number}"
        ),
    )
    parser.add_argument(
        "--user-prompt-file",
        help="Optional file containing user prompt template. Overrides --user-prompt.",
    )
    parser.add_argument(
        "--iterations",
        type=int,
        default=100,
        help="How many Codex calls to run.",
    )
    parser.add_argument(
        "--delay-seconds",
        type=float,
        default=0.0,
        help="Sleep between iterations.",
    )
    parser.add_argument(
        "--iteration-timeout-seconds",
        type=int,
        default=600,
        help="Timeout for each child Codex run in seconds (default: 600).",
    )
    parser.add_argument(
        "--max-attempts-per-target",
        type=int,
        default=3,
        help="Retry count per target URL until required artifacts are produced (default: 3).",
    )
    parser.add_argument(
        "--model",
        default="gpt-5.3-codex",
        help="Optional Codex model override, e.g. gpt-5.3-codex.",
    )
    parser.add_argument(
        "--model-reasoning-effort",
        default="high",
        help="Optional Codex reasoning effort override, e.g. low, medium, high, xhigh.",
    )
    parser.add_argument(
        "--codex-bin",
        default="codex",
        help="Path to codex binary.",
    )
    parser.add_argument(
        "--output-dir",
        default=None,
        help="Output directory (default: /tmp/codex_runs/<timestamp>).",
    )
    parser.add_argument(
        "--continue-on-error",
        action="store_true",
        help="Continue remaining iterations if one iteration fails.",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Only write rendered prompts and metadata; do not call codex.",
    )
    parser.add_argument(
        "--sandbox-mode",
        default="danger-full-access",
        help="Child Codex sandbox mode (default: danger-full-access).",
    )
    parser.add_argument(
        "--approval-policy",
        default="never",
        help="Child Codex approval policy (default: never).",
    )
    parser.add_argument(
        "--ephemeral",
        action="store_true",
        default=True,
        help="Run each child Codex session as ephemeral (default: true).",
    )
    parser.add_argument(
        "--no-ephemeral",
        dest="ephemeral",
        action="store_false",
        help="Disable ephemeral mode and keep child session files.",
    )
    parser.add_argument(
        "--disable-web-search",
        dest="disable_web_search",
        action="store_true",
        default=True,
        help="Disable child web_search tool to prioritize artifact generation (default: true).",
    )
    parser.add_argument(
        "--allow-web-search",
        dest="disable_web_search",
        action="store_false",
        help="Allow child web_search tool.",
    )
    return parser.parse_args()


def render_prompt(template: str, variables: dict[str, Any]) -> str:
    try:
        return template.format(**variables)
    except KeyError as exc:
        raise SystemExit(
            f"Invalid template placeholder: {exc}. Allowed placeholders: "
            "{iteration}, {total}, {csv_file}, {site_url}, {site_slug}, "
            "{row_number}, {csv_line}, {site_column}, {insurance_columns}, {true_product_number}"
        ) from exc


def ensure_dirs(root: Path) -> dict[str, Path]:
    paths = {
        "root": root,
        "prompts": root / "prompts",
        "responses": root / "responses",
        "events": root / "events",
        "stderr": root / "stderr",
        "meta": root / "meta",
    }
    for path in paths.values():
        path.mkdir(parents=True, exist_ok=True)
    return paths


def _resolve_site_column(fieldnames: list[str], explicit_name: str | None) -> str:
    if not fieldnames:
        raise SystemExit("CSV has no header row")

    by_normalized = {_normalize_colname(name): name for name in fieldnames}

    if explicit_name:
        if explicit_name in fieldnames:
            return explicit_name
        normalized_explicit = _normalize_colname(explicit_name)
        if normalized_explicit in by_normalized:
            return by_normalized[normalized_explicit]
        raise SystemExit(f"site column '{explicit_name}' not found in CSV header: {fieldnames}")

    for alias in URL_COLUMN_ALIASES:
        if alias in by_normalized:
            return by_normalized[alias]

    for normalized, original in by_normalized.items():
        if "url" in normalized:
            return original

    raise SystemExit(
        "Could not auto-detect site URL column from CSV header. "
        f"CSV header: {fieldnames}. Use --site-column to specify explicitly."
    )


def _header_has_url_hint(first_row: list[str]) -> bool:
    normalized_cells = {_normalize_colname(cell) for cell in first_row if str(cell).strip()}
    if not normalized_cells:
        return False
    if any("url" in cell for cell in normalized_cells):
        return True
    return any(alias in normalized_cells for alias in URL_COLUMN_ALIASES)


def _load_targets_from_plain_rows(indexed_rows: list[tuple[int, list[str]]]) -> list[dict[str, Any]]:
    targets: list[dict[str, Any]] = []
    for line_number, row in indexed_rows:
        site_url = ""
        for cell in row:
            candidate = str(cell or "").strip()
            if looks_like_url(candidate):
                site_url = candidate
                break
        if not site_url:
            continue
        targets.append(
            {
                "row_number": line_number,
                "csv_line": line_number,
                "site_url": site_url,
                "site_slug": site_slug(site_url),
                "true_product_number": None,
            }
        )
    return targets


def _load_targets_from_header_csv(
    csv_file: Path,
    site_column: str | None,
) -> tuple[list[dict[str, Any]], str]:
    try:
        with csv_file.open("r", encoding="utf-8-sig", newline="") as fp:
            reader = csv.DictReader(fp)
            fieldnames = reader.fieldnames or []
            resolved_site_column = _resolve_site_column(fieldnames, site_column)
            by_normalized = {_normalize_colname(name): name for name in fieldnames}
            count_column: str | None = None
            row_number_column: str | None = None
            csv_line_column: str | None = None
            for alias in COUNT_COLUMN_ALIASES:
                if alias in by_normalized:
                    count_column = by_normalized[alias]
                    break
            for alias in ROW_NUMBER_COLUMN_ALIASES:
                if alias in by_normalized:
                    row_number_column = by_normalized[alias]
                    break
            for alias in CSV_LINE_COLUMN_ALIASES:
                if alias in by_normalized:
                    csv_line_column = by_normalized[alias]
                    break
            if count_column is None:
                for normalized, original in by_normalized.items():
                    if "product" in normalized and ("count" in normalized or "number" in normalized):
                        count_column = original
                        break

            targets: list[dict[str, Any]] = []
            for fallback_row_number, row in enumerate(reader, start=1):
                site_url = str(row.get(resolved_site_column, "") or "").strip()
                if not site_url or not looks_like_url(site_url):
                    continue
                true_product_number = (
                    _parse_optional_int(row.get(count_column)) if count_column else None
                )
                source_row_number = (
                    _parse_optional_int(row.get(row_number_column))
                    if row_number_column
                    else None
                )
                source_csv_line = (
                    _parse_optional_int(row.get(csv_line_column))
                    if csv_line_column
                    else None
                )
                row_number = source_row_number or fallback_row_number
                csv_line = source_csv_line or reader.line_num
                targets.append(
                    {
                        "row_number": row_number,
                        "csv_line": csv_line,
                        "site_url": site_url,
                        "site_slug": site_slug(site_url),
                        "true_product_number": true_product_number,
                    }
                )
    except FileNotFoundError as exc:
        raise SystemExit(f"CSV file not found: {csv_file}") from exc

    if not targets:
        raise SystemExit(f"No website URLs found in CSV column '{resolved_site_column}'")

    return targets, resolved_site_column


def load_targets(
    csv_file: Path,
    site_column: str | None,
) -> tuple[list[dict[str, Any]], str]:
    try:
        with csv_file.open("r", encoding="utf-8-sig", newline="") as fp:
            indexed_rows = [
                (line_number, row)
                for line_number, row in enumerate(csv.reader(fp), start=1)
                if row and any(str(cell).strip() for cell in row)
            ]
    except FileNotFoundError as exc:
        raise SystemExit(f"CSV file not found: {csv_file}") from exc

    if not indexed_rows:
        raise SystemExit(f"CSV file is empty: {csv_file}")

    if site_column:
        return _load_targets_from_header_csv(csv_file, site_column)

    first_row = indexed_rows[0][1]
    if len(first_row) == 1 and looks_like_url(str(first_row[0]).strip()):
        targets = _load_targets_from_plain_rows(indexed_rows)
        if not targets:
            raise SystemExit(f"No valid URLs found in plain URL list: {csv_file}")
        return targets, "plain_line_url"

    if all(len(row) == 1 and looks_like_url(str(row[0]).strip()) for _, row in indexed_rows):
        targets = _load_targets_from_plain_rows(indexed_rows)
        if not targets:
            raise SystemExit(f"No valid URLs found in plain URL list: {csv_file}")
        return targets, "plain_line_url"

    if _header_has_url_hint(first_row):
        return _load_targets_from_header_csv(csv_file, site_column=None)

    # Fallback: try header mode first; if it cannot resolve a URL column,
    # fallback to plain-line mode when first cell appears to be URLs.
    try:
        return _load_targets_from_header_csv(csv_file, site_column=None)
    except SystemExit:
        first_col_all_urls = all(looks_like_url(str(row[0]).strip()) for _, row in indexed_rows if row)
        if first_col_all_urls:
            targets = _load_targets_from_plain_rows(indexed_rows)
            if targets:
                return targets, "plain_line_url"
        raise


def build_command(
    codex_bin: str,
    response_file: Path,
    system_prompt_file: Path,
    model: str | None,
    model_reasoning_effort: str | None,
    sandbox_mode: str,
    approval_policy: str,
    ephemeral: bool,
    disable_web_search: bool,
) -> list[str]:
    command = [
        codex_bin,
        "exec",
        "--skip-git-repo-check",
        "--json",
        "-o",
        str(response_file),
        "-c",
        f"model_instructions_file={json.dumps(str(system_prompt_file.resolve()))}",
        "-c",
        f"sandbox_mode={json.dumps(sandbox_mode)}",
        "-c",
        f"approval_policy={json.dumps(approval_policy)}",
    ]
    if disable_web_search:
        command.extend(["-c", 'disabled_tools=["web_search"]'])
    if ephemeral:
        command.append("--ephemeral")
    if model:
        command.extend(["-m", model])
    if model_reasoning_effort:
        command.extend(["-c", f"model_reasoning_effort={json.dumps(model_reasoning_effort)}"])
    command.append("-")
    return command


def main() -> int:
    args = parse_args()
    if args.iterations <= 0:
        raise SystemExit("--iterations must be >= 1")
    if args.start_index <= 0:
        raise SystemExit("--start-index must be >= 1")
    if args.delay_seconds < 0:
        raise SystemExit("--delay-seconds must be >= 0")
    if args.iteration_timeout_seconds <= 0:
        raise SystemExit("--iteration-timeout-seconds must be >= 1")
    if args.max_attempts_per_target <= 0:
        raise SystemExit("--max-attempts-per-target must be >= 1")

    system_prompt_path = Path(args.system_prompt_file)
    system_prompt_text = read_text_file(system_prompt_path, "System prompt file")
    if not system_prompt_text.strip():
        raise SystemExit(f"System prompt file is empty: {system_prompt_path}")
    baseline_system_hash = sha256_hex(system_prompt_text.encode("utf-8"))

    insurance_columns_text = ", ".join(INSURANCE_COLUMNS)

    if args.user_prompt_file:
        user_prompt_template = read_text_file(Path(args.user_prompt_file), "User prompt file")
    else:
        user_prompt_template = args.user_prompt

    csv_path = Path(args.csv_file)
    targets, resolved_site_column = load_targets(
        csv_file=csv_path,
        site_column=args.site_column,
    )
    selected_targets = targets[args.start_index - 1 : args.start_index - 1 + args.iterations]
    if not selected_targets:
        raise SystemExit(
            f"No targets selected. start_index={args.start_index}, total_targets={len(targets)}"
        )

    run_id = datetime.now().strftime("%Y%m%d_%H%M%S")
    root = Path(args.output_dir) if args.output_dir else Path("/tmp/codex_runs") / run_id
    paths = ensure_dirs(root)

    manifest = {
        "started_at_utc": utc_now_iso(),
        "run_id": run_id,
        "cwd": str(Path.cwd()),
        "codex_bin": args.codex_bin,
        "model": args.model,
        "model_reasoning_effort": args.model_reasoning_effort,
        "system_prompt_file": str(system_prompt_path.resolve()),
        "system_prompt_sha256": baseline_system_hash,
        "iterations_requested": args.iterations,
        "iterations_planned": len(selected_targets),
        "delay_seconds": args.delay_seconds,
        "iteration_timeout_seconds": args.iteration_timeout_seconds,
        "max_attempts_per_target": args.max_attempts_per_target,
        "dry_run": args.dry_run,
        "continue_on_error": args.continue_on_error,
        "csv_file": str(csv_path.resolve()),
        "site_column": resolved_site_column,
        "round1_output_mode": "json_artifacts_only",
        "start_index": args.start_index,
        "sandbox_mode": args.sandbox_mode,
        "approval_policy": args.approval_policy,
        "ephemeral": args.ephemeral,
        "disable_web_search": args.disable_web_search,
        "insurance_schema_columns": INSURANCE_COLUMNS,
        "results": [],
    }

    (paths["root"] / "targets.json").write_text(
        json.dumps(selected_targets, indent=2),
        encoding="utf-8",
    )

    generation_failures = 0

    for iteration, target in enumerate(selected_targets, start=1):
        print(
            f"[{iteration}/{len(selected_targets)}] row={target['row_number']} site={target['site_url']}",
            flush=True,
        )

        current_system_text = read_text_file(system_prompt_path, "System prompt file")
        current_system_hash = sha256_hex(current_system_text.encode("utf-8"))
        if current_system_hash != baseline_system_hash:
            raise SystemExit(
                "System prompt changed during run. "
                f"Expected {baseline_system_hash}, got {current_system_hash}."
            )

        prompt_vars = {
            "iteration": iteration,
            "total": len(selected_targets),
            "csv_file": str(csv_path),
            "site_url": target["site_url"],
            "site_slug": target["site_slug"],
            "row_number": target["row_number"],
            "csv_line": target["csv_line"],
            "site_column": resolved_site_column,
            "insurance_columns": insurance_columns_text,
            "true_product_number": (
                target.get("true_product_number")
                if target.get("true_product_number") is not None
                else "unknown"
            ),
        }

        rendered_user_prompt = render_prompt(user_prompt_template, prompt_vars)
        prompt_hash = sha256_hex(rendered_user_prompt.encode("utf-8"))
        artifacts = expected_artifacts(target["row_number"], target["site_url"])
        for artifact_path in artifacts.values():
            artifact_path.parent.mkdir(parents=True, exist_ok=True)
        remove_stale_artifacts(artifacts)
        cleanup_row_extras(target["row_number"], target["site_url"])

        meta_file = paths["meta"] / f"iteration_{iteration:04d}.json"

        record: dict[str, Any] = {
            "iteration": iteration,
            "started_at_utc": utc_now_iso(),
            "system_prompt_sha256": baseline_system_hash,
            "user_prompt_sha256": prompt_hash,
            "exit_code": None,
            "elapsed_seconds": None,
            "timed_out": False,
            "target": target,
            "artifact_paths": {k: str(v) for k, v in artifacts.items()},
            "attempts": [],
        }

        if args.dry_run:
            dry_prompt = build_attempt_prompt(
                base_prompt=rendered_user_prompt,
                row_number=target["row_number"],
                attempt=1,
                max_attempts=args.max_attempts_per_target,
                artifacts=artifacts,
            )
            prompt_file = paths["prompts"] / f"iteration_{iteration:04d}_attempt_01.txt"
            prompt_file.write_text(dry_prompt, encoding="utf-8")
            record["exit_code"] = 0
            record["elapsed_seconds"] = 0.0
            record["status"] = "dry-run"
            record["attempts"].append(
                {
                    "attempt": 1,
                    "status": "dry-run",
                    "prompt_file": str(prompt_file),
                }
            )
            meta_file.write_text(json.dumps(record, indent=2), encoding="utf-8")
            manifest["results"].append(record)
            continue

        previous_issues: list[str] | None = None
        artifact_success = False
        for attempt in range(1, args.max_attempts_per_target + 1):
            print(
                f"  attempt {attempt}/{args.max_attempts_per_target} for row {target['row_number']}",
                flush=True,
            )
            attempt_tag = f"iteration_{iteration:04d}_attempt_{attempt:02d}"
            prompt_file = paths["prompts"] / f"{attempt_tag}.txt"
            response_file = paths["responses"] / f"{attempt_tag}.txt"
            events_file = paths["events"] / f"{attempt_tag}.jsonl"
            stderr_file = paths["stderr"] / f"{attempt_tag}.log"

            attempt_prompt = build_attempt_prompt(
                base_prompt=rendered_user_prompt,
                row_number=target["row_number"],
                attempt=attempt,
                max_attempts=args.max_attempts_per_target,
                artifacts=artifacts,
                previous_issues=previous_issues,
            )
            prompt_file.write_text(attempt_prompt, encoding="utf-8")

            command = build_command(
                codex_bin=args.codex_bin,
                response_file=response_file,
                system_prompt_file=system_prompt_path,
                model=args.model,
                model_reasoning_effort=args.model_reasoning_effort,
                sandbox_mode=args.sandbox_mode,
                approval_policy=args.approval_policy,
                ephemeral=args.ephemeral,
                disable_web_search=args.disable_web_search,
            )

            started = time.time()
            timeout_expired = False
            try:
                proc = subprocess.run(
                    command,
                    input=attempt_prompt,
                    text=True,
                    capture_output=True,
                    timeout=args.iteration_timeout_seconds,
                )
            except subprocess.TimeoutExpired as exc:
                timeout_expired = True

                class _TimedOutProc:
                    returncode = 124
                    stdout = as_text(exc.stdout)
                    stderr = as_text(exc.stderr)

                proc = _TimedOutProc()  # type: ignore

            elapsed = round(time.time() - started, 3)

            events_file.write_text(as_text(proc.stdout), encoding="utf-8")
            stderr_file.write_text(as_text(proc.stderr), encoding="utf-8")

            expected_count_raw = target.get("true_product_number")
            expected_count = expected_count_raw if isinstance(expected_count_raw, int) else None
            artifact_check = validate_artifacts(
                artifacts,
                expected_count=expected_count,
                source_url=target["site_url"],
            )
            previous_issues = artifact_check["issues"]
            cleanup_row_extras(target["row_number"], target["site_url"])

            attempt_status = (
                "ok_artifacts"
                if artifact_check["success"]
                else ("timed_out" if timeout_expired else "missing_artifacts")
            )
            attempt_record = {
                "attempt": attempt,
                "status": attempt_status,
                "timed_out": timeout_expired,
                "exit_code": proc.returncode,
                "elapsed_seconds": elapsed,
                "prompt_file": str(prompt_file),
                "response_file": str(response_file),
                "events_file": str(events_file),
                "stderr_file": str(stderr_file),
                "command": command,
                "artifact_check": artifact_check,
            }
            record["attempts"].append(attempt_record)

            print(
                f"    status={attempt_status} exit={proc.returncode} "
                f"insurance_rows_count={artifact_check.get('insurance_rows_count')} "
                f"issues={len(artifact_check.get('issues', []))}",
                flush=True,
            )

            if artifact_check["success"]:
                artifact_success = True
                break

        last_attempt = record["attempts"][-1]
        record["exit_code"] = last_attempt["exit_code"]
        record["elapsed_seconds"] = sum(float(a["elapsed_seconds"]) for a in record["attempts"])
        record["timed_out"] = any(bool(a["timed_out"]) for a in record["attempts"])
        iteration_status = "failed"
        if artifact_success:
            try:
                rows_data = load_json_list(artifacts["insurance_rows_json"])
                record["artifacts"] = {
                    "insurance_rows_count": len(rows_data),
                }
                print(f"  produced insurance_rows_count={len(rows_data)}", flush=True)
                iteration_status = "ok"
            except Exception as exc:
                artifact_success = False
                record["artifact_read_error"] = str(exc)
                print(f"  artifact_read_error={exc}", flush=True)
                iteration_status = "failed"

        record["status"] = iteration_status
        record["finished_at_utc"] = utc_now_iso()
        record["attempts_used"] = len(record["attempts"])

        if iteration_status == "failed":
            generation_failures += 1
            print(f"  row {target['row_number']} failed", flush=True)

        meta_file.write_text(json.dumps(record, indent=2), encoding="utf-8")
        manifest["results"].append(record)

        if iteration_status == "failed" and not args.continue_on_error:
            break

        if args.delay_seconds > 0 and iteration < len(selected_targets):
            time.sleep(args.delay_seconds)

    manifest["finished_at_utc"] = utc_now_iso()
    manifest["iterations_completed"] = len(manifest["results"])
    manifest["generation_failures"] = generation_failures
    manifest["failures"] = generation_failures
    manifest["status"] = "ok" if generation_failures == 0 else "failed"

    (paths["root"] / "manifest.json").write_text(json.dumps(manifest, indent=2), encoding="utf-8")

    summary = {
        "status": manifest["status"],
        "iterations_completed": manifest["iterations_completed"],
        "iterations_requested": args.iterations,
        "iterations_planned": len(selected_targets),
        "generation_failures": generation_failures,
        "failures": generation_failures,
        "run_dir": str(paths["root"].resolve()),
    }
    print(json.dumps(summary, indent=2))
    return 0 if generation_failures == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
