import json
import re
from html import unescape
from urllib.parse import urljoin, urlparse

import requests
from bs4 import BeautifulSoup

import sys
from pathlib import Path

ROOT_DIR = Path(__file__).resolve().parents[1]
if str(ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(ROOT_DIR))

from api_configgg import API_KEY, API_URL  # required config source


LISTING_URL = "https://www.prudential.com.hk/sc/products/investment/"
EXPECTED_COUNT = 1
OUTPUT_JSON = "json_outputs/insurance_rows_prudential_com_hk_row_12.json"

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


def _clean_text(text):
    if text is None:
        return None
    value = " ".join(BeautifulSoup(text, "lxml").get_text(" ", strip=True).split())
    return value or None


def _discover_listing_endpoint(listing_html):
    pattern = r"(/content/[^\"']*productcomparisonsel\.model\.json)"
    match = re.search(pattern, listing_html)
    if match:
        return urljoin(LISTING_URL, match.group(1))
    return None


def _extract_provider_and_last_updated(detail_html):
    provider = None
    title_match = re.search(r"<title>(.*?)</title>", detail_html, flags=re.IGNORECASE | re.DOTALL)
    if title_match:
        title_text = unescape(title_match.group(1)).strip()
        if "|" in title_text:
            provider = title_text.split("|")[-1].strip() or None

    last_updated = None
    mod_match = re.search(r"repo:modifyDate\\x22:\\x22(.*?)\\x22", detail_html)
    if mod_match:
        try:
            last_updated = bytes(mod_match.group(1), "utf-8").decode("unicode_escape")
        except Exception:
            last_updated = mod_match.group(1)

    return provider, last_updated


def _extract_brochure_url(detail_html, detail_url):
    soup = BeautifulSoup(detail_html, "lxml")
    candidates = []

    for a in soup.find_all("a", href=True):
        href = a["href"].strip()
        full = urljoin(detail_url, href)
        text = " ".join(a.get_text(" ", strip=True).split())
        if ".pdf" in full.lower() or "brochure" in full.lower():
            candidates.append((text, full))

    if not candidates:
        return None

    priority_words = ["产品册子", "產品冊子", "product brochure", "brochure"]
    for text, full in candidates:
        lt = text.lower()
        if any((w in text) or (w in lt) for w in priority_words):
            return full if urlparse(full).scheme in {"http", "https"} else None

    for _, full in candidates:
        if "/brochure/" in full.lower() and urlparse(full).scheme in {"http", "https"}:
            return full

    full = candidates[0][1]
    return full if urlparse(full).scheme in {"http", "https"} else None


def _build_rows(endpoint_json):
    rows = []
    seen = set()

    for category in endpoint_json.get("categories", []):
        for product in category.get("products", []):
            plan_name = _clean_text(product.get("title"))
            detail_url = urljoin(LISTING_URL, product.get("link", ""))
            if not plan_name or not detail_url:
                continue

            dedupe_key = (detail_url.rstrip("/"), plan_name.lower())
            if dedupe_key in seen:
                continue
            seen.add(dedupe_key)

            detail_html = requests.get(detail_url, timeout=30).text
            provider, last_updated = _extract_provider_and_last_updated(detail_html)
            brochure_url = _extract_brochure_url(detail_html, detail_url)

            row = {
                "Plan_ID": _clean_text(product.get("id")),
                "Plan_Name": plan_name,
                "Provider_Company": provider,
                "Monthly_Premium_Min": None,
                "Monthly_Premium_Max": None,
                "Deductible_Range": None,
                "Out_of_Pocket_Max_Est": None,
                "Cost_Tier": None,
                "Copays_Description": None,
                "Coverage_Description": None,
                "Network_Type": None,
                "Geographic_Coverage": None,
                "Campus_Compatible": None,
                "Min_Age": None,
                "Max_Age": None,
                "Student_Eligible": None,
                "International_Student_Compatible": None,
                "Overall_Rating": None,
                "Plan_Description": _clean_text(product.get("description")),
                "URL": detail_url,
                "Last_Updated": last_updated,
                "Product_Brochure_route": brochure_url,
            }

            # enforce exact schema keys
            row = {k: row.get(k, None) for k in SCHEMA_KEYS}
            rows.append(row)

    rows.sort(key=lambda x: (x["Plan_Name"] or "", x["URL"] or ""))
    return rows


def main():
    _ = API_URL, API_KEY  # explicitly sourced from api_configgg.py

    listing_html = requests.get(LISTING_URL, timeout=30).text
    endpoint = _discover_listing_endpoint(listing_html)
    if not endpoint:
        raise RuntimeError("No productcomparisonsel.model.json endpoint discovered from listing page")

    endpoint_json = requests.get(endpoint, timeout=30).json()
    rows = _build_rows(endpoint_json)

    if len(rows) != EXPECTED_COUNT:
        raise RuntimeError(f"Row count mismatch: expected {EXPECTED_COUNT}, got {len(rows)}")

    with open(OUTPUT_JSON, "w", encoding="utf-8") as f:
        json.dump(rows, f, ensure_ascii=False, indent=2)


if __name__ == "__main__":
    main()
