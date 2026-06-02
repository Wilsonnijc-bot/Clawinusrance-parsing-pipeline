#!/usr/bin/env python3
"""Row-scoped parser for https://www.prudential.com.hk/sc/products/save/ (row 10)."""

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

LISTING_URL = "https://www.prudential.com.hk/sc/products/save/"
EXPECTED_COUNT = 9
OUTPUT_JSON = PROJECT_ROOT / "json_outputs/insurance_rows_prudential_com_hk_row_10.json"

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
    value = re.sub(r"<[^>]+>", " ", value)
    value = unescape(value)
    value = re.sub(r"\s+", " ", value).strip()
    return value or None


def absolute_url(base_url: str, maybe_url: str | None) -> str | None:
    if not maybe_url:
        return None
    u = maybe_url.strip()
    if not u:
        return None
    if u.startswith("//"):
        u = "https:" + u
    elif not u.startswith(("http://", "https://")):
        u = urljoin(base_url, u)
    parsed = urlparse(u)
    if parsed.scheme not in {"http", "https"}:
        return None
    return u


def find_endpoint_from_listing(html: str) -> str:
    m = re.search(r'"(/content/[^"\']*productcomparisonsel\.model\.json[^"\']*)"', html)
    if m:
        endpoint = absolute_url(LISTING_URL, m.group(1))
        if endpoint:
            return endpoint
    return (
        "https://www.prudential.com.hk/content/prudential-aem-lbu/phkl/zh_cn/"
        "products/save/jcr:content/root/containerextension/column0/containerextension/"
        "column0/containerextension_c/column0/productcomparisonsel.model.json"
    )


def extract_pdf_links(detail_html: str, detail_url: str) -> list[str]:
    found: list[str] = []

    for candidate in re.findall(r'(?:"|\')((?:https?:)?//[^"\']+?\.pdf[^"\']*|/[^"\']+?\.pdf[^"\']*)(?:"|\')', detail_html):
        url = absolute_url(detail_url, candidate)
        if url and ".pdf" in url.lower():
            found.append(url)

    for candidate in re.findall(r'/content/[^"\'\s<>]+?\.pdf[^"\'\s<>]*', detail_html):
        url = absolute_url(detail_url, candidate)
        if url and ".pdf" in url.lower():
            found.append(url)

    deduped: list[str] = []
    seen: set[str] = set()
    for u in found:
        if u not in seen:
            seen.add(u)
            deduped.append(u)
    return deduped


def choose_brochure_url(candidates: list[str]) -> str | None:
    if not candidates:
        return None

    def score(url: str) -> int:
        u = url.lower()
        s = 0
        if "/pdf/sc/brochure/" in u:
            s += 100
        if "product-brochure" in u:
            s += 50
        if "/macau/" in u:
            s -= 40
        if "/tc/" in u:
            s -= 20
        for noise in ["promotion", "investment-mix", "guide", "booklet", "flyer", "ranking", "leaflet", "fact-sheet"]:
            if noise in u:
                s -= 30
        return s

    ranked = sorted(enumerate(candidates), key=lambda x: (-score(x[1]), x[0]))
    best = ranked[0][1]
    return best if best.startswith(("http://", "https://")) else None


def fetch_brochure_for_product(session: requests.Session, product_url: str) -> str | None:
    if ".pdf" in product_url.lower():
        return product_url
    resp = session.get(product_url, timeout=40)
    resp.raise_for_status()
    return choose_brochure_url(extract_pdf_links(resp.text, product_url))


def build_rows() -> list[dict[str, Any]]:
    _api_config_loaded = (API_URL, API_KEY)

    session = requests.Session()
    session.headers.update(
        {
            "User-Agent": "Mozilla/5.0 (compatible; insurance-parser/1.0)",
            "Accept-Language": "zh-HK,zh;q=0.9,en;q=0.8",
        }
    )

    listing_resp = session.get(LISTING_URL, timeout=40)
    listing_resp.raise_for_status()

    endpoint_url = find_endpoint_from_listing(listing_resp.text)
    endpoint_data = session.get(endpoint_url, timeout=40).json()

    categories = endpoint_data.get("categories") or []
    target_category = next((c for c in categories if c.get("tabLabelId") == "savings"), categories[0] if categories else None)
    if not target_category:
        raise RuntimeError("No product category found")

    rows: list[dict[str, Any]] = []
    seen: set[tuple[str, str]] = set()

    for product in target_category.get("products") or []:
        plan_name = clean_text(product.get("title"))
        product_url = absolute_url(LISTING_URL, product.get("link"))
        if not plan_name or not product_url:
            continue

        key = (product_url.rstrip("/"), plan_name.lower())
        if key in seen:
            continue
        seen.add(key)

        row = {k: None for k in SCHEMA_KEYS}
        row["Plan_ID"] = product.get("id") or None
        row["Plan_Name"] = plan_name
        row["Provider_Company"] = None
        row["Plan_Description"] = clean_text(product.get("description"))
        row["URL"] = product_url
        row["Product_Brochure_route"] = fetch_brochure_for_product(session, product_url)
        rows.append(row)

    if len(rows) != EXPECTED_COUNT:
        raise RuntimeError(f"Parsed {len(rows)} products, expected {EXPECTED_COUNT}")

    for row in rows:
        if not row["Plan_Name"] or not row["URL"]:
            raise RuntimeError("Plan_Name and URL must be non-empty")
        brochure = row["Product_Brochure_route"]
        if brochure is not None and not brochure.startswith(("http://", "https://")):
            raise RuntimeError("Product_Brochure_route must be absolute when non-null")

    return rows


def main() -> None:
    rows = build_rows()
    OUTPUT_JSON.parent.mkdir(parents=True, exist_ok=True)
    OUTPUT_JSON.write_text(json.dumps(rows, ensure_ascii=False, indent=2), encoding="utf-8")


if __name__ == "__main__":
    main()
