#!/usr/bin/env python3
"""Round-2 brochure loop that appends row outputs into brochure_parsing.json."""

from __future__ import annotations

import argparse
import concurrent.futures
import csv
import json
import re
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

BROCHURE_KEYS: list[str] = [
    "Plan_category",
    "Coverage_Description",
    "pricing",
    "age",
    "customer requirement",
    "price structure",
    "additional informations",
]


def url_section_key(url_number: int) -> str:
    return f"# ========================= Url number {url_number} in productpageurl ========================="


def row_section_key(row_number: int) -> str:
    return f"# ========================= Row {row_number} ========================="


def canonical_family(url_value: str) -> tuple[str, str]:
    parsed = urlparse((url_value or "").strip())
    host = parsed.netloc.lower()
    segments = [segment for segment in parsed.path.split("/") if segment]
    if len(segments) >= 3:
        prefix = "/" + "/".join(segments[:3])
    else:
        prefix = parsed.path.rstrip("/")
    return host, prefix


def clean_text(value: Any) -> str:
    if value is None:
        return ""
    text = str(value).strip()
    if text.lower() in {"", "null", "none", "n/a", "na", "unknown"}:
        return ""
    return re.sub(r"\s+", " ", text).strip()


def keywordize(text: str, max_parts: int = 3, max_len: int = 160) -> str | None:
    raw = clean_text(text)
    if not raw:
        return None

    chunks = [part.strip(" .") for part in re.split(r"[;\n]", raw) if part.strip()]
    if not chunks:
        chunks = [raw]

    selected: list[str] = []
    seen: set[str] = set()
    for chunk in chunks:
        cleaned = chunk[:max_len].strip(" .")
        key = cleaned.lower()
        if not cleaned or key in seen:
            continue
        seen.add(key)
        selected.append(cleaned)
        if len(selected) >= max_parts:
            break

    if not selected:
        return None
    return "; ".join(selected)


def normalize_plan_category(text: str) -> str | None:
    raw = clean_text(text)
    if not raw:
        return None
    lowered = raw.lower()
    if "whole life" in lowered:
        return "whole life"
    if "term" in lowered:
        return "term life"
    if "health" in lowered or "medical" in lowered:
        return "health"
    if "critical illness" in lowered:
        return "critical illness"
    if "savings" in lowered:
        return "savings"
    return keywordize(raw, max_parts=1, max_len=50)


def normalize_age_range(text: str) -> str | None:
    raw = clean_text(text)
    if not raw:
        return None

    day_match = re.search(r"\b(\d{1,3})\s*days?\b", raw, flags=re.IGNORECASE)
    day_min = day_match.group(1) if day_match else None

    range_matches = re.findall(r"\b(\d{1,3})\s*[–-]\s*(\d{1,3})\b", raw)
    valid_ranges = [
        (int(start), int(end))
        for start, end in range_matches
        if 0 < int(start) <= int(end) <= 100
    ]
    if valid_ranges:
        start, end = valid_ranges[0]
        if day_min is not None:
            return f"{day_min} days-{end}"
        return f"{start}-{end}"

    age_numbers = [
        int(value)
        for value in re.findall(r"\b(\d{1,3})\b", raw)
        if 10 <= int(value) <= 100
    ]
    if age_numbers:
        min_age = min(age_numbers)
        max_age = max(age_numbers)
        if day_min is not None:
            return f"{day_min} days-{max_age}"
        if min_age == max_age:
            return str(min_age)
        return f"{min_age}-{max_age}"

    upto = re.search(r"up to age\s*(\d{1,3})", raw, flags=re.IGNORECASE)
    if upto:
        return f"up to {upto.group(1)}"
    return keywordize(raw, max_parts=1, max_len=60)


def normalize_pricing(text: str) -> str | None:
    raw = clean_text(text)
    if not raw:
        return None

    lowered = raw.lower()
    has_concrete_amount = bool(
        re.search(
            r"(?:US\$|HK\$|MOP\$|USD|HKD|MOP)\s*\d[\d,]*(?:\.\d+)?",
            raw,
            flags=re.IGNORECASE,
        )
    )

    if has_concrete_amount:
        # Keep concrete brochure examples/ranges instead of over-compressing.
        if len(raw) <= 280:
            return raw
        chunks = [part.strip(" .") for part in re.split(r"[;\n]", raw) if part.strip()]
        if not chunks:
            return raw[:280].rstrip()
        return "; ".join(chunks[:2])[:280].rstrip()

    if "not listed" in lowered or "no fixed" in lowered or "not fixed" in lowered:
        return "not listed"
    if "rate" in lowered and ("per" in lowered or "/1,000" in lowered):
        return "rate-based"

    return keywordize(raw, max_parts=2, max_len=120)


def dedupe_additional(
    additional: str | None,
    coverage_description: str,
    plan_category: str | None,
    pricing: str | None,
    price_structure: str | None,
) -> str | None:
    if not additional:
        return None

    coverage_lower = clean_text(coverage_description).lower()
    plan_lower = clean_text(plan_category).lower()
    pricing_lower = clean_text(pricing).lower()
    structure_lower = clean_text(price_structure).lower()

    parts = [part.strip() for part in additional.split(";") if part.strip()]
    kept: list[str] = []
    for part in parts:
        lowered = part.lower()
        if plan_lower and plan_lower in lowered:
            continue
        if coverage_lower and lowered in coverage_lower:
            continue
        if "whole life" in lowered and "whole life" in coverage_lower:
            continue
        if "benefit term" in lowered and "benefit term" in coverage_lower:
            continue
        if pricing_lower and lowered == pricing_lower:
            continue
        if structure_lower and lowered == structure_lower:
            continue
        kept.append(part)

    if not kept:
        return None
    return "; ".join(kept[:3])


def polish_brochure_payload(
    payload: dict[str, Any],
    coverage_description: str,
) -> dict[str, Any]:
    plan_category = normalize_plan_category(str(payload.get("Plan_category", "") or ""))
    coverage_output = keywordize(
        str(payload.get("Coverage_Description", "") or ""),
        max_parts=3,
        max_len=180,
    )
    pricing = normalize_pricing(str(payload.get("pricing", "") or ""))
    age = normalize_age_range(str(payload.get("age", "") or ""))
    customer_requirement = keywordize(
        str(payload.get("customer requirement", "") or ""),
        max_parts=2,
        max_len=140,
    )
    price_structure = keywordize(
        str(payload.get("price structure", "") or ""),
        max_parts=2,
        max_len=140,
    )
    additional = keywordize(
        str(payload.get("additional informations", "") or ""),
        max_parts=3,
        max_len=140,
    )
    additional = dedupe_additional(
        additional=additional,
        coverage_description=(coverage_output or coverage_description),
        plan_category=plan_category,
        pricing=pricing,
        price_structure=price_structure,
    )

    return {
        "Plan_category": plan_category,
        "Coverage_Description": coverage_output,
        "pricing": pricing,
        "age": age,
        "customer requirement": customer_requirement,
        "price structure": price_structure,
        "additional informations": additional,
    }


def load_productpage_rows(path: Path) -> list[dict[str, Any]]:
    with path.open("r", encoding="utf-8-sig", newline="") as fp:
        reader = csv.DictReader(fp)
        rows: list[dict[str, Any]] = []
        for idx, row in enumerate(reader, start=1):
            url_text = str(row.get("url", "") or "").strip()
            if not url_text:
                continue
            host, prefix = canonical_family(url_text)
            rows.append(
                {
                    "url_number": idx,
                    "url": url_text,
                    "host": host,
                    "prefix": prefix,
                }
            )
    return rows


def load_insurance_rows(path: Path) -> list[dict[str, Any]]:
    with path.open("r", encoding="utf-8-sig", newline="") as fp:
        reader = csv.DictReader(fp)
        rows: list[dict[str, Any]] = []
        for idx, row in enumerate(reader, start=1):
            rows.append({"row_number": idx, "row": row})
    return rows


def resolve_url_number(product_url: str, productpage_rows: list[dict[str, Any]]) -> int | None:
    host, prefix = canonical_family(product_url)
    if not host:
        return None
    for item in productpage_rows:
        if host == item["host"] and prefix == item["prefix"]:
            return int(item["url_number"])
    return None


def infer_url_number_from_neighbors(
    row_number: int,
    insurance_rows: list[dict[str, Any]],
    productpage_rows: list[dict[str, Any]],
) -> int | None:
    index = row_number - 1
    if index < 0 or index >= len(insurance_rows):
        return None

    prev_num: int | None = None
    next_num: int | None = None

    if index - 1 >= 0:
        prev_url = str(insurance_rows[index - 1]["row"].get("URL", "") or "").strip()
        if prev_url:
            prev_num = resolve_url_number(prev_url, productpage_rows)

    if index + 1 < len(insurance_rows):
        next_url = str(insurance_rows[index + 1]["row"].get("URL", "") or "").strip()
        if next_url:
            next_num = resolve_url_number(next_url, productpage_rows)

    if prev_num is not None and next_num is not None:
        if prev_num == next_num:
            return prev_num
        # Boundary rows may inherit nearby promo URLs; prefer next row section.
        return next_num
    if next_num is not None:
        return next_num
    if prev_num is not None:
        return prev_num
    return None


def _is_row_key(value: str) -> bool:
    return bool(re.search(r"\brow\s+\d+\b", value or "", flags=re.IGNORECASE))


def load_or_init_parsing_output(output_path: Path) -> dict[str, Any]:
    data: dict[str, Any] = {}
    if output_path.exists():
        try:
            payload = json.loads(output_path.read_text(encoding="utf-8"))
            if isinstance(payload, dict):
                data = payload
        except Exception:
            data = {}

    # Normalize to flat row-only structure.
    flat: dict[str, Any] = {}
    for top_key, top_value in data.items():
        if _is_row_key(str(top_key)) and isinstance(top_value, dict):
            flat[str(top_key)] = {key: top_value.get(key) for key in BROCHURE_KEYS}
            continue
        if not isinstance(top_value, dict):
            continue
        for nested_key, nested_value in top_value.items():
            if not _is_row_key(str(nested_key)) or not isinstance(nested_value, dict):
                continue
            flat[str(nested_key)] = {key: nested_value.get(key) for key in BROCHURE_KEYS}
    return flat


def save_parsing_output(output_path: Path, data: dict[str, Any]) -> None:
    output_path.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")


def load_validate_brochure_artifact(path: Path) -> dict[str, Any]:
    if not path.exists():
        raise RuntimeError(f"Missing artifact: {path}")
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except Exception as exc:
        raise RuntimeError(f"Invalid JSON in {path}: {exc}") from exc
    if not isinstance(payload, dict):
        raise RuntimeError(f"Artifact must be an object: {path}")
    missing = [key for key in BROCHURE_KEYS if key not in payload]
    if missing:
        raise RuntimeError(
            "Artifact keys mismatch in "
            f"{path}. missing={missing} actual={sorted(payload.keys())}"
        )
    return {key: payload.get(key) for key in BROCHURE_KEYS}


def build_row_prompt(row_number: int, attempt_number: int, row: dict[str, Any]) -> str:
    plan_name = str(row.get("Plan_Name", "") or "").strip()
    provider_company = str(row.get("Provider_Company", "") or "").strip()
    product_url = str(row.get("URL", "") or "").strip()
    brochure_route = str(row.get("Product_Brochure_route", "") or "").strip()
    coverage_description = str(row.get("Coverage_Description", "") or "").strip()
    coverage_description = clean_text(coverage_description)
    if len(coverage_description) > 600:
        coverage_description = coverage_description[:600].rstrip() + "..."
    fields_line = ", ".join(BROCHURE_KEYS)

    return (
        "Run one brochure extraction task.\n"
        f"- row_number: {row_number}\n"
        f"- attempt_number: {attempt_number}\n"
        f"- plan_name: {plan_name}\n"
        f"- provider_company: {provider_company}\n"
        f"- product_url: {product_url}\n"
        f"- product_brochure_route: {brochure_route}\n\n"
        f"- coverage_description: {coverage_description}\n\n"
        "Requirements:\n"
        "1) Produce exactly one file: json_outputs/brochure_fields_row_{row_number}.json\n"
        f"2) Output must be one JSON object with exactly keys: {fields_line}\n"
        "3) Unknown values must be null.\n"
        "4) Keep output concise for age and non-pricing fields; for pricing, keep concrete premium examples/ranges when evidence exists.\n"
        "5) Dedupe repeated concepts across plan category, coverage_description, and additional informations.\n"
        "6) Do not write other project-root files.\n"
        f"Reply exactly: DONE row {row_number} attempt {attempt_number}\n"
    )


def run_codex_attempt(
    codex_bin: str,
    system_prompt_file: Path,
    prompt_text: str,
    response_file: Path,
    model: str | None,
    model_reasoning_effort: str | None,
    sandbox_mode: str,
    approval_policy: str,
    disable_web_search: bool,
    timeout_seconds: int,
) -> tuple[int, str, str]:
    def _to_text(value: Any) -> str:
        if value is None:
            return ""
        if isinstance(value, bytes):
            return value.decode("utf-8", errors="replace")
        return str(value)

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
    if model:
        command.extend(["-m", model])
    if model_reasoning_effort:
        command.extend(["-c", f"model_reasoning_effort={json.dumps(model_reasoning_effort)}"])
    command.extend(["--ephemeral", "-"])

    try:
        proc = subprocess.run(
            command,
            input=prompt_text,
            text=True,
            capture_output=True,
            timeout=timeout_seconds,
        )
        return proc.returncode, _to_text(proc.stdout), _to_text(proc.stderr)
    except subprocess.TimeoutExpired as exc:
        return 124, _to_text(exc.stdout), _to_text(exc.stderr)


def split_tasks_into_url_groups(
    tasks: list[dict[str, Any]],
    group_count: int,
) -> list[list[dict[str, Any]]]:
    if group_count <= 1:
        return [tasks] if tasks else []
    groups: list[list[dict[str, Any]]] = [[] for _ in range(group_count)]
    for task in tasks:
        idx = (int(task["row_number"]) - 1) % group_count
        groups[idx].append(task)
    return [group for group in groups if group]


def process_row_task(
    task: dict[str, Any],
    args: argparse.Namespace,
    run_dir: Path,
    system_prompt_file: Path,
) -> dict[str, Any]:
    row_number = int(task["row_number"])
    row = task["row"]
    row_key = str(task["row_key"])
    artifact_path = Path(task["artifact_path"])
    group_id = int(task["group_id"])

    row_ok = False
    last_error = ""

    for attempt in range(1, args.max_attempts_per_row + 1):
        prompt_text = build_row_prompt(row_number=row_number, attempt_number=attempt, row=row)
        tag = f"group_{group_id:02d}_row_{row_number:04d}_attempt_{attempt:02d}"
        prompt_file = run_dir / f"{tag}.prompt.txt"
        response_file = run_dir / f"{tag}.txt"
        events_file = run_dir / f"{tag}.jsonl"
        stderr_file = run_dir / f"{tag}.stderr.log"
        prompt_file.write_text(prompt_text, encoding="utf-8")

        if not args.use_existing_artifacts:
            exit_code, stdout_text, stderr_text = run_codex_attempt(
                codex_bin=args.codex_bin,
                system_prompt_file=system_prompt_file,
                prompt_text=prompt_text,
                response_file=response_file,
                model=args.model,
                model_reasoning_effort=args.model_reasoning_effort,
                sandbox_mode=args.sandbox_mode,
                approval_policy=args.approval_policy,
                disable_web_search=args.disable_web_search,
                timeout_seconds=args.timeout_seconds,
            )
            events_file.write_text(stdout_text, encoding="utf-8")
            stderr_file.write_text(stderr_text, encoding="utf-8")
        else:
            exit_code = 0
            events_file.write_text("", encoding="utf-8")
            stderr_file.write_text("", encoding="utf-8")

        try:
            payload = load_validate_brochure_artifact(artifact_path)
            polished_payload = polish_brochure_payload(
                payload=payload,
                coverage_description=str(row.get("Coverage_Description", "") or ""),
            )
            artifact_path.write_text(
                json.dumps(polished_payload, indent=2, ensure_ascii=False),
                encoding="utf-8",
            )
            row_ok = True
            print(
                f"row={row_number} status=ok group={group_id} "
                f"attempt={attempt} exit={exit_code}",
                flush=True,
            )
            return {
                "row_number": row_number,
                "row_key": row_key,
                "status": "ok",
                "payload": polished_payload,
                "attempt": attempt,
                "exit_code": exit_code,
            }
        except Exception as exc:
            last_error = str(exc)
            print(
                f"row={row_number} status=retry group={group_id} attempt={attempt} "
                f"exit={exit_code} error={last_error}",
                flush=True,
            )
            time.sleep(1)

    if not row_ok:
        print(
            f"row={row_number} status=failed group={group_id} error={last_error}",
            flush=True,
        )
    return {
        "row_number": row_number,
        "row_key": row_key,
        "status": "failed",
        "error": last_error,
    }


def process_task_group(
    group_id: int,
    group_tasks: list[dict[str, Any]],
    args: argparse.Namespace,
    run_dir: Path,
    system_prompt_file: Path,
) -> list[dict[str, Any]]:
    results: list[dict[str, Any]] = []
    for task in group_tasks:
        task_with_group = dict(task)
        task_with_group["group_id"] = group_id
        results.append(process_row_task(task_with_group, args, run_dir, system_prompt_file))
    return results


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Round-2 brochure loop. For each Insurance_datas row, generate "
            "brochure_fields_row_{row}.json and append/update brochure_parsing.json."
        )
    )
    parser.add_argument("--insurance-csv", default="dental_insurance.csv")
    parser.add_argument(
        "--productpage-csv",
        default="productpageurl.csv",
        help="Retained for compatibility; not used by round-2 row-only JSON layout.",
    )
    parser.add_argument("--output-json", default="json_outputs/brochure_parsing.json")
    parser.add_argument("--system-prompt-file", default="prompts/system_prompt_brochure.txt")
    parser.add_argument("--codex-bin", default="codex")
    parser.add_argument("--start-index", type=int, default=1)
    parser.add_argument("--limit", type=int, default=1000000)
    parser.add_argument("--max-attempts-per-row", type=int, default=3)
    parser.add_argument("--timeout-seconds", type=int, default=900)
    parser.add_argument("--model", default="gpt-5.3-codex")
    parser.add_argument(
        "--model-reasoning-effort",
        default="high",
        help="Optional Codex reasoning effort override, e.g. low, medium, high, xhigh.",
    )
    parser.add_argument("--sandbox-mode", default="danger-full-access")
    parser.add_argument("--approval-policy", default="never")
    parser.add_argument("--disable-web-search", action="store_true", default=True)
    parser.add_argument(
        "--parallel-groups",
        type=int,
        default=4,
        help="Number of parallel row groups for round-2 parsing (default: 4). Set 1 for sequential.",
    )
    parser.add_argument("--reparse-all", action="store_true")
    parser.add_argument("--use-existing-artifacts", action="store_true")
    parser.add_argument("--run-dir", default=None)
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    if args.start_index <= 0:
        raise SystemExit("--start-index must be >= 1")
    if args.limit <= 0:
        raise SystemExit("--limit must be >= 1")
    if args.max_attempts_per_row <= 0:
        raise SystemExit("--max-attempts-per-row must be >= 1")
    if args.parallel_groups <= 0:
        raise SystemExit("--parallel-groups must be >= 1")

    insurance_csv = Path(args.insurance_csv)
    output_json = Path(args.output_json)
    system_prompt_file = Path(args.system_prompt_file)

    insurance_rows = load_insurance_rows(insurance_csv)
    parsing_output = load_or_init_parsing_output(output_json)

    run_stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    run_dir = Path(args.run_dir) if args.run_dir else Path("/tmp/codex_runs") / f"brochure_round2_{run_stamp}"
    run_dir.mkdir(parents=True, exist_ok=True)

    end_index = args.start_index - 1 + args.limit
    selected = insurance_rows[args.start_index - 1 : end_index]

    processed = 0
    succeeded = 0
    failed = 0
    skipped = 0
    appended_to_json = 0

    tasks: list[dict[str, Any]] = []
    for item in selected:
        row_number = int(item["row_number"])
        row = item["row"]
        brochure_route = str(row.get("Product_Brochure_route", "") or "").strip()
        if not brochure_route:
            skipped += 1
            continue

        row_key = row_section_key(row_number)
        if not args.reparse_all and row_key in parsing_output:
            skipped += 1
            continue

        artifact_path = Path("json_outputs") / f"brochure_fields_row_{row_number}.json"
        artifact_path.parent.mkdir(parents=True, exist_ok=True)
        if artifact_path.exists() and not args.use_existing_artifacts:
            artifact_path.unlink()

        tasks.append(
            {
                "row_number": row_number,
                "row": row,
                "row_key": row_key,
                "artifact_path": artifact_path,
            }
        )

    processed = len(tasks)
    groups = split_tasks_into_url_groups(tasks, args.parallel_groups)

    task_results: list[dict[str, Any]] = []
    if groups:
        if len(groups) == 1:
            task_results = process_task_group(
                group_id=1,
                group_tasks=groups[0],
                args=args,
                run_dir=run_dir,
                system_prompt_file=system_prompt_file,
            )
        else:
            max_workers = min(len(groups), args.parallel_groups)
            with concurrent.futures.ThreadPoolExecutor(max_workers=max_workers) as executor:
                futures = {
                    executor.submit(
                        process_task_group,
                        group_id=group_id,
                        group_tasks=group_tasks,
                        args=args,
                        run_dir=run_dir,
                        system_prompt_file=system_prompt_file,
                    ): group_id
                    for group_id, group_tasks in enumerate(groups, start=1)
                }
                for future in concurrent.futures.as_completed(futures):
                    group_result = future.result()
                    task_results.extend(group_result)

    # Apply successful payloads in deterministic row order.
    for result in sorted(task_results, key=lambda item: int(item["row_number"])):
        if result["status"] == "ok":
            row_key = str(result["row_key"])
            payload = result["payload"]
            parsing_output[row_key] = payload
            save_parsing_output(output_json, parsing_output)
            appended_to_json += 1
            succeeded += 1
        else:
            failed += 1

    summary = {
        "status": "ok" if failed == 0 else "partial",
        "run_dir": str(run_dir.resolve()),
        "output_json": str(output_json.resolve()),
        "rows_selected": len(selected),
        "rows_processed": processed,
        "rows_succeeded": succeeded,
        "rows_failed": failed,
        "rows_skipped": skipped,
        "json_appends": appended_to_json,
        "parallel_groups": args.parallel_groups,
        "parallel_group_count": len(groups),
    }
    print(json.dumps(summary, indent=2))
    return 0 if failed == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
