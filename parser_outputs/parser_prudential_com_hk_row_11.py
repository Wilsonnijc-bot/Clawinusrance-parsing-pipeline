#!/usr/bin/env python3
"""Row-scoped parser for Prudential HK save retirement tab (row 11)."""

from __future__ import annotations

import json
import re
import sys
from html import unescape
from pathlib import Path
from typing import Any
from urllib.parse import urljoin, urlparse

import requests

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from api_configgg import API_KEY, API_URL  # required config source

LISTING_URL = "https://www.prudential.com.hk/sc/products/save/#retirement-1-tab"
EXPECTED_COUNT = 7
OUTPUT_JSON = PROJECT_ROOT / "json_outputs/insurance_rows_prudential_com_hk_row_11.json"

SCHEMA_KEYS = [
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


def clean_text(value: str | None) -> str | None:
    if not value:
        return None
    text = re.sub(r"<[^>]+>", " ", value)
    text = unescape(text)
    text = re.sub(r"\s+", " ", text).strip()
    return text or None


def absolute_http_url(base_url: str, maybe_url: str | None) -> str | None:
    if not maybe_url:
        return None
    url = maybe_url.strip()
    if not url:
        return None
    if url.startswith("//"):
        url = "https:" + url
    elif not url.startswith(("http://", "https://")):
        url = urljoin(base_url, url)
    parsed = urlparse(url)
    if parsed.scheme not in {"http", "https"}:
        return None
    return url


def discover_endpoint(listing_html: str) -> str:
    match = re.search(r'data-search-endpoint="([^"]+)"', listing_html)
    if match:
        endpoint = absolute_http_url(LISTING_URL, match.group(1))
        if endpoint:
            return endpoint

    match = re.search(r'"(/content/[^"\']*productcomparisonsel\.model\.json[^"\']*)"', listing_html)
    if match:
        endpoint = absolute_http_url(LISTING_URL, match.group(1))
        if endpoint:
            return endpoint

    raise RuntimeError("Could not discover product comparison endpoint")


def target_tab_id_from_url(url: str) -> str | None:
    fragment = urlparse(url).fragment or ""
    if not fragment:
        return None
    # e.g. retirement-1-tab -> retirement
    if "-" in fragment:
        return fragment.split("-", 1)[0] or None
    return fragment or None


def extract_pdf_candidates(detail_html: str, detail_url: str, listing_url: str) -> list[str]:
    candidates: list[str] = []

    pattern_quoted = r'(?:"|\')((?:https?:)?//[^"\'\s<>]+?\.pdf[^"\'\s<>]*|/[^"\'\s<>]+?\.pdf[^"\'\s<>]*)(?:"|\')'
    for raw in re.findall(pattern_quoted, detail_html, flags=re.IGNORECASE):
        abs_url = absolute_http_url(detail_url, raw) or absolute_http_url(listing_url, raw)
        if abs_url and ".pdf" in abs_url.lower():
            candidates.append(abs_url)

    pattern_unquoted = r'/content/[^"\'\s<>]+?\.pdf[^"\'\s<>]*'
    for raw in re.findall(pattern_unquoted, detail_html, flags=re.IGNORECASE):
        abs_url = absolute_http_url(detail_url, raw) or absolute_http_url(listing_url, raw)
        if abs_url and ".pdf" in abs_url.lower():
            candidates.append(abs_url)

    deduped: list[str] = []
    seen: set[str] = set()
    for item in candidates:
        if item not in seen:
            seen.add(item)
            deduped.append(item)
    return deduped


def brochure_score(url: str) -> int:
    u = url.lower()
    score = 0
    if "/pdf/sc/brochure/" in u:
        score += 120
    if "product-brochure" in u:
        score += 80
    if "/sc/brochure/" in u:
        score += 40
    if "brochure" in u:
        score += 20

    for noise in [
        "promotion",
        "investment-mix",
        "quick-guide",
        "guide",
        "flyer",
        "booklet",
        "ranking",
        "factsheet",
        "fact-sheet",
        "leaflet",
    ]:
        if noise in u:
            score -= 45

    if "/macau/" in u:
        score -= 30
    if "/tc/" in u:
        score -= 15

    return score


def choose_brochure(candidates: list[str]) -> str | None:
    if not candidates:
        return None
    ranked = sorted(enumerate(candidates), key=lambda item: (-brochure_score(item[1]), item[0]))
    best = ranked[0][1]
    if not best.startswith(("http://", "https://")):
        return None
    return best


def extract_last_updated(detail_html: str) -> str | None:
    patterns = [
        r'<meta[^>]+property=["\']article:modified_time["\'][^>]+content=["\']([^"\']+)["\']',
        r'<meta[^>]+property=["\']og:updated_time["\'][^>]+content=["\']([^"\']+)["\']',
        r'<meta[^>]+name=["\']last-modified["\'][^>]+content=["\']([^"\']+)["\']',
        r'<meta[^>]+name=["\']lastModified["\'][^>]+content=["\']([^"\']+)["\']',
    ]
    for pattern in patterns:
        match = re.search(pattern, detail_html, flags=re.IGNORECASE)
        if match:
            return match.group(1).strip() or None
    return None


def fetch_detail_fields(session: requests.Session, product_url: str) -> tuple[str | None, str | None]:
    if ".pdf" in product_url.lower():
        brochure = absolute_http_url(product_url, product_url)
        return brochure, None

    resp = session.get(product_url, timeout=45)
    resp.raise_for_status()
    detail_html = resp.text

    brochure = choose_brochure(extract_pdf_candidates(detail_html, product_url, LISTING_URL))
    last_updated = extract_last_updated(detail_html)
    return brochure, last_updated


def build_rows() -> list[dict[str, Any]]:
    # Required config source touch.
    _ = (API_URL, API_KEY)

    session = requests.Session()
    session.headers.update(
        {
            "User-Agent": "Mozilla/5.0 (compatible; insurance-row-parser/1.0)",
            "Accept-Language": "zh-HK,zh;q=0.9,en;q=0.8",
        }
    )

    listing_resp = session.get(LISTING_URL, timeout=45)
    listing_resp.raise_for_status()

    endpoint_url = discover_endpoint(listing_resp.text)
    endpoint_data = session.get(endpoint_url, timeout=45).json()

    categories = endpoint_data.get("categories") or []
    target_tab = target_tab_id_from_url(LISTING_URL)

    selected_category = None
    if target_tab:
        selected_category = next((c for c in categories if (c.get("tabLabelId") or "") == target_tab), None)
    if not selected_category and categories:
        selected_category = categories[0]
    if not selected_category:
        raise RuntimeError("No category found in endpoint response")

    rows: list[dict[str, Any]] = []
    seen: set[tuple[str, str]] = set()

    for product in selected_category.get("products") or []:
        plan_name = clean_text(product.get("title"))
        product_url = absolute_http_url(LISTING_URL, product.get("link"))
        if not plan_name or not product_url:
            continue

        key = (product_url.rstrip("/"), plan_name.lower())
        if key in seen:
            continue
        seen.add(key)

        brochure_url, last_updated = fetch_detail_fields(session, product_url)

        row = {key_name: None for key_name in SCHEMA_KEYS}
        row["Plan_ID"] = product.get("id") or None
        row["Plan_Name"] = plan_name
        row["Provider_Company"] = None
        row["Coverage_Description"] = clean_text(product.get("description"))
        row["Geographic_Coverage"] = ", ".join(product.get("productTag") or []) or None
        row["Plan_Description"] = clean_text(product.get("description"))
        row["URL"] = product_url
        row["Last_Updated"] = last_updated
        row["Product_Brochure_route"] = brochure_url
        rows.append(row)

    if len(rows) != EXPECTED_COUNT:
        raise RuntimeError(f"Parsed {len(rows)} products, expected {EXPECTED_COUNT}")

    for row in rows:
        if not row["Plan_Name"] or not row["URL"]:
            raise RuntimeError("Each row must include non-empty Plan_Name and URL")
        brochure = row["Product_Brochure_route"]
        if brochure is not None and not brochure.startswith(("http://", "https://")):
            raise RuntimeError("Product_Brochure_route must be absolute http(s) URL when non-null")

    return rows


def main() -> None:
    rows = build_rows()
    OUTPUT_JSON.parent.mkdir(parents=True, exist_ok=True)
    OUTPUT_JSON.write_text(json.dumps(rows, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


if __name__ == "__main__":
    main()
