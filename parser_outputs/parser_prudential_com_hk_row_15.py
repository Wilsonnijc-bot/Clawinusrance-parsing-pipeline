#!/usr/bin/env python3
"""Row-scoped parser for Prudential HK products page (row 15)."""

from __future__ import annotations

import json
import re
from html import unescape
from pathlib import Path
from typing import Dict, List, Optional
from urllib.parse import urljoin, urlparse

import requests

LISTING_URL = "https://www.prudential.com.hk/tc/products/travel-and-leisure/#tab-1"
MODEL_ENDPOINT = (
    "https://www.prudential.com.hk/content/prudential-aem-lbu/phkl/zh_hk/"
    "products/travel-and-leisure/jcr:content/root/containerextension/column0/"
    "containerextension/column0/containerextension_c/column0/"
    "productcomparisonsel.model.json"
)
OUTPUT_JSON = Path("json_outputs/insurance_rows_prudential_com_hk_row_15.json")

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


def clean_html_to_text(value: Optional[str]) -> Optional[str]:
    if not value:
        return None
    text = re.sub(r"<br\s*/?>", "\n", value, flags=re.I)
    text = re.sub(r"<[^>]+>", " ", text)
    text = unescape(text)
    text = re.sub(r"\s+", " ", text).strip()
    return text or None


def extract_tab_index(url: str) -> int:
    fragment = urlparse(url).fragment or ""
    match = re.search(r"tab-(\d+)", fragment)
    return int(match.group(1)) if match else 0


def absolute_http_url(candidate: Optional[str], base_url: str) -> Optional[str]:
    if not candidate:
        return None
    url = candidate.strip()
    if not url:
        return None
    if url.startswith("//"):
        url = "https:" + url
    else:
        url = urljoin(base_url, url)
    if url.startswith("http://") or url.startswith("https://"):
        return url
    return None


def extract_meta_description(html: str) -> Optional[str]:
    match = re.search(
        r'<meta\s+name=["\']description["\']\s+content=["\']([^"\']+)["\']',
        html,
        flags=re.I,
    )
    return clean_html_to_text(match.group(1)) if match else None


def extract_provider_company(html: str) -> Optional[str]:
    title_match = re.search(r"<title>(.*?)</title>", html, flags=re.I | re.S)
    if not title_match:
        return None
    title_text = clean_html_to_text(title_match.group(1))
    if not title_text:
        return None
    if "|" in title_text:
        suffix = title_text.split("|")[-1].strip()
        return suffix or None
    return None


def extract_pdf_links(html: str, base_url: str) -> List[str]:
    links = set()
    for href in re.findall(r'href=["\']([^"\']+\.pdf(?:\?[^"\']*)?)["\']', html, flags=re.I):
        abs_url = absolute_http_url(href, base_url)
        if abs_url:
            links.add(abs_url)
    for raw in re.findall(r'"((?:https?:)?//[^"\s]+\.pdf(?:\?[^"\s]*)?)"', html, flags=re.I):
        abs_url = absolute_http_url(raw, base_url)
        if abs_url:
            links.add(abs_url)
    return sorted(links)


def choose_brochure_url(pdf_links: List[str]) -> Optional[str]:
    if not pdf_links:
        return None

    def score(url: str) -> int:
        low = url.lower()
        s = 0
        if "product-brochure" in low:
            s += 200
        if "brochure" in low:
            s += 100
        if "promotion" in low:
            s -= 120
        if "offer" in low or "tnc" in low:
            s -= 80
        return s

    ranked = sorted(pdf_links, key=lambda u: (-score(u), u))
    best = ranked[0]
    return best if score(best) > 0 else None


def blank_row() -> Dict[str, Optional[object]]:
    return {k: None for k in SCHEMA_KEYS}


def build_rows() -> List[Dict[str, Optional[object]]]:
    session = requests.Session()
    session.headers.update({"User-Agent": "Mozilla/5.0 (row-15-parser)"})

    listing_data = session.get(MODEL_ENDPOINT, timeout=30).json()
    categories = listing_data.get("categories") or []
    tab_index = extract_tab_index(LISTING_URL)
    if tab_index < 0 or tab_index >= len(categories):
        tab_index = 0

    products = categories[tab_index].get("products") or []

    seen = set()
    rows: List[Dict[str, Optional[object]]] = []

    for product in products:
        plan_name = clean_html_to_text(product.get("title"))
        detail_url = absolute_http_url(product.get("link"), LISTING_URL)
        if not plan_name or not detail_url:
            continue

        dedupe_key = (detail_url.rstrip("/"), re.sub(r"\s+", " ", plan_name).strip().lower())
        if dedupe_key in seen:
            continue
        seen.add(dedupe_key)

        detail_html = session.get(detail_url, timeout=30).text
        card_description = clean_html_to_text(product.get("description"))
        meta_description = extract_meta_description(detail_html)
        provider_company = extract_provider_company(detail_html)

        pdf_links = extract_pdf_links(detail_html, detail_url)
        brochure_url = choose_brochure_url(pdf_links)

        row = blank_row()
        row.update(
            {
                "Plan_ID": clean_html_to_text(product.get("id")),
                "Plan_Name": plan_name,
                "Provider_Company": provider_company,
                "Coverage_Description": card_description,
                "Plan_Description": meta_description or card_description,
                "URL": detail_url,
                "Product_Brochure_route": brochure_url,
            }
        )
        rows.append(row)

    return rows


def main() -> None:
    rows = build_rows()
    OUTPUT_JSON.parent.mkdir(parents=True, exist_ok=True)
    with OUTPUT_JSON.open("w", encoding="utf-8") as f:
        json.dump(rows, f, ensure_ascii=False, indent=2)


if __name__ == "__main__":
    main()
