#!/usr/bin/env python3
"""Row-scoped parser for Prudential HK travel and leisure products (row 14)."""

import json
import re
from html import unescape
from pathlib import Path
from typing import Any
from urllib.parse import urljoin, urlparse

import requests
from bs4 import BeautifulSoup

# Config source available for API-driven workflows when needed.
import importlib.util

ROOT_DIR = Path(__file__).resolve().parents[1]
CONFIG_PATH = ROOT_DIR / "api_configgg.py"
if CONFIG_PATH.exists():
    spec = importlib.util.spec_from_file_location("api_configgg", CONFIG_PATH)
    if spec and spec.loader:
        api_configgg = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(api_configgg)

LISTING_URL = "https://www.prudential.com.hk/tc/products/travel-and-leisure/#tab-0"
OUTPUT_JSON = Path("json_outputs/insurance_rows_prudential_com_hk_row_14.json")

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


def extract_tab_index(listing_url: str) -> int:
    frag = (urlparse(listing_url).fragment or "").lower()
    m = re.search(r"tab-(\d+)", frag)
    return int(m.group(1)) if m else 0


def strip_html(text: str | None) -> str | None:
    if not text:
        return None
    soup = BeautifulSoup(text, "lxml")
    cleaned = " ".join(soup.get_text(" ", strip=True).split())
    return cleaned or None


def resolve_http_url(raw_url: str | None, detail_url: str, listing_url: str) -> str | None:
    if not raw_url:
        return None
    absolute = urljoin(detail_url, raw_url)
    if not absolute.startswith(("http://", "https://")):
        absolute = urljoin(listing_url, raw_url)
    return absolute if absolute.startswith(("http://", "https://")) else None


def find_product_endpoint(listing_html: str) -> str:
    # Endpoint-first: discover the structured model endpoint from the listing page.
    m = re.search(r'(/content/[^"\']*productcomparisonsel\.model\.json)', listing_html)
    if not m:
        raise RuntimeError("Unable to locate product comparison model endpoint.")
    return urljoin(LISTING_URL, m.group(1))


def choose_brochure_link(detail_html: str, detail_url: str) -> str | None:
    soup = BeautifulSoup(detail_html, "lxml")
    candidates: list[tuple[int, str]] = []

    for a in soup.select("a[href]"):
        href = (a.get("href") or "").strip()
        if ".pdf" not in href.lower():
            continue

        text = " ".join(a.get_text(" ", strip=True).split())
        href_l = href.lower()
        text_l = text.lower()

        score = 0
        if "product-brochure" in href_l:
            score += 100
        if "產品小冊子" in text:
            score += 40
        if "/brochure/" in href_l:
            score += 20
        if "promotion" in href_l:
            score -= 90
        if "covid" in href_l or "新冠" in text:
            score -= 30

        abs_url = resolve_http_url(href, detail_url, LISTING_URL)
        if abs_url:
            candidates.append((score, abs_url))

    if not candidates:
        return None

    candidates.sort(key=lambda x: x[0], reverse=True)
    return candidates[0][1]


def extract_last_updated(detail_html: str) -> str | None:
    m = re.search(r"repo:modifyDate\\x22:\\x22(.*?)\\x22", detail_html)
    if not m:
        return None
    return m.group(1).replace("\\u002D", "-")


def fetch_detail_fields(detail_url: str) -> dict[str, Any]:
    r = requests.get(detail_url, timeout=30)
    r.raise_for_status()
    html = r.text
    soup = BeautifulSoup(html, "lxml")

    page_title = soup.title.get_text(" ", strip=True) if soup.title else ""
    meta_desc_tag = soup.find("meta", attrs={"name": "description"})
    meta_desc = (meta_desc_tag.get("content") or "").strip() if meta_desc_tag else None

    provider = "保誠香港" if "保誠香港" in page_title else None
    brochure = choose_brochure_link(html, detail_url)
    last_updated = extract_last_updated(html)

    return {
        "Provider_Company": provider,
        "Plan_Description": unescape(meta_desc) if meta_desc else None,
        "Last_Updated": last_updated,
        "Product_Brochure_route": brochure,
    }


def build_rows() -> list[dict[str, Any]]:
    listing_res = requests.get(LISTING_URL, timeout=30)
    listing_res.raise_for_status()
    listing_html = listing_res.text

    endpoint = find_product_endpoint(listing_html)
    endpoint_res = requests.get(endpoint, timeout=30)
    endpoint_res.raise_for_status()
    model = endpoint_res.json()

    categories = model.get("categories") or []
    tab_index = extract_tab_index(LISTING_URL)
    if tab_index >= len(categories):
        raise RuntimeError(f"Tab index {tab_index} out of range for categories.")

    products = categories[tab_index].get("products") or []
    rows: list[dict[str, Any]] = []

    for product in products:
        detail_url = resolve_http_url(product.get("link"), LISTING_URL, LISTING_URL)
        if not detail_url:
            continue

        detail_fields = fetch_detail_fields(detail_url)

        row = {
            "Plan_ID": product.get("id") or None,
            "Plan_Name": (product.get("title") or "").strip() or None,
            "Provider_Company": detail_fields["Provider_Company"],
            "Monthly_Premium_Min": None,
            "Monthly_Premium_Max": None,
            "Deductible_Range": None,
            "Out_of_Pocket_Max_Est": None,
            "Cost_Tier": None,
            "Copays_Description": None,
            "Coverage_Description": strip_html(product.get("description")),
            "Network_Type": None,
            "Geographic_Coverage": None,
            "Campus_Compatible": None,
            "Min_Age": None,
            "Max_Age": None,
            "Student_Eligible": None,
            "International_Student_Compatible": None,
            "Overall_Rating": None,
            "Plan_Description": detail_fields["Plan_Description"],
            "URL": detail_url,
            "Last_Updated": detail_fields["Last_Updated"],
            "Product_Brochure_route": detail_fields["Product_Brochure_route"],
        }

        # Enforce schema and key order deterministically.
        row = {k: row.get(k, None) for k in SCHEMA_KEYS}
        if row["Plan_Name"] and row["URL"]:
            rows.append(row)

    return rows


def main() -> None:
    rows = build_rows()
    OUTPUT_JSON.write_text(json.dumps(rows, ensure_ascii=False, indent=2), encoding="utf-8")


if __name__ == "__main__":
    main()
