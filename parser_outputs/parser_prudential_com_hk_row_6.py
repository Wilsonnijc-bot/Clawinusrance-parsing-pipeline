#!/usr/bin/env python3
"""Row-scoped parser for Prudential HK health critical illness tab (row 6)."""

from __future__ import annotations

import json
import re
from html import unescape
from pathlib import Path
from typing import Any
from urllib.parse import urljoin

import requests
from bs4 import BeautifulSoup

LISTING_URL = "https://www.prudential.com.hk/sc/products/health/#critical-illness-0-tab"
ENDPOINT_URL = (
    "https://www.prudential.com.hk/content/prudential-aem-lbu/phkl/zh_cn/products/health/"
    "jcr:content/root/containerextension/column0/containerextension_543280936/column0/"
    "containerextension_c/column0/productcomparisonsel.model.json"
)
TARGET_TAB_ID = "critical-illness"
TRUE_PRODUCT_NUMBER = 8
OUTPUT_JSON = Path("json_outputs/insurance_rows_prudential_com_hk_row_6.json")
PROVIDER = "Prudential Hong Kong"

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


def strip_html(text: str | None) -> str | None:
    if not text:
        return None
    plain = BeautifulSoup(text, "html.parser").get_text(" ", strip=True)
    plain = unescape(re.sub(r"\s+", " ", plain)).strip()
    return plain or None


def absolute_http_url(base_url: str, raw_url: str | None) -> str | None:
    if not raw_url:
        return None
    resolved = urljoin(base_url, raw_url.strip())
    return resolved if resolved.startswith(("http://", "https://")) else None


def pick_brochure_url(detail_url: str, session: requests.Session) -> str | None:
    # If product link itself is already a PDF brochure.
    if ".pdf" in detail_url.lower():
        return detail_url

    try:
        resp = session.get(detail_url, timeout=30)
        resp.raise_for_status()
    except Exception:
        return None

    soup = BeautifulSoup(resp.text, "html.parser")
    candidates: list[tuple[str, str]] = []
    for a in soup.find_all("a", href=True):
        href = (a.get("href") or "").strip()
        text = " ".join(a.get_text(" ", strip=True).split())
        low_href = href.lower()
        if ".pdf" in low_href or "brochure" in low_href or "小册子" in text:
            abs_href = absolute_http_url(detail_url, href)
            if abs_href:
                candidates.append((text, abs_href))

    if not candidates:
        return None

    def choose(predicate):
        for text, href in candidates:
            if predicate(text, href):
                return href
        return None

    return (
        choose(lambda t, h: "产品小册子" in t and "下载" in t and "澳门" not in t and ".pdf" in h.lower())
        or choose(lambda t, h: "產品小冊子" in t and "下載" in t and "澳門" not in t and ".pdf" in h.lower())
        or choose(lambda t, h: "产品小册子" in t and "澳门" not in t and ".pdf" in h.lower())
        or choose(lambda t, h: "產品小冊子" in t and "澳門" not in t and ".pdf" in h.lower())
        or choose(lambda t, h: "/brochure/" in h.lower() and "/macau/" not in h.lower() and ".pdf" in h.lower())
        or choose(lambda t, h: "brochure" in h.lower() and "macau" not in h.lower() and ".pdf" in h.lower())
        or choose(lambda t, h: ".pdf" in h.lower())
    )


def extract_rows() -> list[dict[str, Any]]:
    session = requests.Session()
    data = session.get(ENDPOINT_URL, timeout=30).json()
    category = next(c for c in data.get("categories", []) if c.get("tabLabelId") == TARGET_TAB_ID)
    products = category.get("products", [])

    rows: list[dict[str, Any]] = []
    seen: set[tuple[str, str]] = set()

    for product in products:
        plan_name = (product.get("title") or "").strip()
        if not plan_name:
            continue

        product_url = absolute_http_url(LISTING_URL, product.get("link"))
        if not product_url:
            continue

        dedupe_key = (product_url.rstrip("/"), re.sub(r"\s+", " ", plan_name).lower())
        if dedupe_key in seen:
            continue
        seen.add(dedupe_key)

        tags = product.get("productTag") if isinstance(product.get("productTag"), list) else []
        geo_tokens = [t for t in tags if t in {"香港", "澳门", "澳門", "中国", "中國"}]
        geographic = ", ".join(geo_tokens) if geo_tokens else None

        description = strip_html(product.get("description"))
        brochure = pick_brochure_url(product_url, session)

        row = {
            "Plan_ID": product.get("id") or None,
            "Plan_Name": plan_name,
            "Provider_Company": PROVIDER,
            "Monthly_Premium_Min": None,
            "Monthly_Premium_Max": None,
            "Deductible_Range": None,
            "Out_of_Pocket_Max_Est": None,
            "Cost_Tier": None,
            "Copays_Description": None,
            "Coverage_Description": description,
            "Network_Type": None,
            "Geographic_Coverage": geographic,
            "Campus_Compatible": None,
            "Min_Age": None,
            "Max_Age": None,
            "Student_Eligible": None,
            "International_Student_Compatible": None,
            "Overall_Rating": None,
            "Plan_Description": description,
            "URL": product_url,
            "Last_Updated": None,
            "Product_Brochure_route": brochure,
        }

        # Ensure exact schema key order.
        row = {k: row.get(k) for k in SCHEMA_KEYS}
        rows.append(row)

    return rows


def main() -> None:
    rows = extract_rows()

    if len(rows) != TRUE_PRODUCT_NUMBER:
        raise RuntimeError(f"Expected {TRUE_PRODUCT_NUMBER} products, got {len(rows)}")

    for idx, row in enumerate(rows, 1):
        if not row.get("Plan_Name"):
            raise RuntimeError(f"Row {idx} missing Plan_Name")
        if not row.get("URL"):
            raise RuntimeError(f"Row {idx} missing URL")
        brochure = row.get("Product_Brochure_route")
        if brochure is not None and not str(brochure).startswith(("http://", "https://")):
            raise RuntimeError(f"Row {idx} has non-absolute brochure URL: {brochure}")

    OUTPUT_JSON.parent.mkdir(parents=True, exist_ok=True)
    OUTPUT_JSON.write_text(json.dumps(rows, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


if __name__ == "__main__":
    main()
