#!/usr/bin/env python3
"""Row-scoped parser for Prudential Hong Kong home-and-pet products (row 13)."""

from __future__ import annotations

import json
import sys
from pathlib import Path
from urllib.parse import urljoin

import requests
from bs4 import BeautifulSoup

sys.path.append(str(Path(__file__).resolve().parent.parent))
from api_configgg import API_KEY, API_URL  # required config source

LISTING_URL = "https://www.prudential.com.hk/tc/products/home-and-pet/"
ENDPOINT_URL = (
    "https://www.prudential.com.hk/content/prudential-aem-lbu/phkl/zh_hk/products/"
    "home-and-pet/jcr:content/root/containerextension/column0/containerextension/"
    "column0/containerextension_c/column0/productcomparisonsel.model.json"
)
TRUE_PRODUCT_NUMBER = 5
OUTPUT_JSON = Path("json_outputs/insurance_rows_prudential_com_hk_row_13.json")

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
    text = BeautifulSoup(value, "html.parser").get_text(" ", strip=True)
    text = " ".join(text.split())
    return text or None


def extract_last_updated(soup: BeautifulSoup) -> str | None:
    selectors = [
        ("meta", {"property": "article:modified_time"}),
        ("meta", {"property": "og:updated_time"}),
        ("meta", {"name": "last-modified"}),
        ("meta", {"name": "lastModified"}),
    ]
    for tag, attrs in selectors:
        node = soup.find(tag, attrs=attrs)
        if node and node.get("content"):
            return node["content"].strip() or None
    return None


def extract_detail_fields(session: requests.Session, detail_url: str) -> tuple[str | None, str | None]:
    resp = session.get(detail_url, timeout=30)
    resp.raise_for_status()
    soup = BeautifulSoup(resp.text, "html.parser")

    candidates: list[tuple[int, int, str]] = []
    for index, anchor in enumerate(soup.select("a[href]")):
        href = (anchor.get("href") or "").strip()
        if ".pdf" not in href.lower():
            continue

        absolute_href = urljoin(detail_url, href)
        if not absolute_href.lower().startswith(("http://", "https://")):
            continue

        anchor_text = " ".join(anchor.stripped_strings)
        joined = f"{href} {anchor_text}".lower()

        score = 0
        if "product-brochure" in joined:
            score += 10
        if "/brochure/" in joined:
            score += 5
        if "brochure" in joined:
            score += 3
        if "產品小冊子" in anchor_text or "小冊子" in anchor_text:
            score += 4
        if "promotion" in joined or "tnc" in joined or "vas" in joined:
            score -= 8

        candidates.append((score, index, absolute_href))

    brochure_url = None
    if candidates:
        candidates.sort(key=lambda item: (-item[0], item[1], item[2]))
        brochure_url = candidates[0][2]

    return brochure_url, extract_last_updated(soup)


def build_rows() -> list[dict]:
    # Touch required config source without external API usage.
    _ = API_URL, API_KEY

    session = requests.Session()
    session.headers.update(
        {
            "User-Agent": (
                "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36"
            )
        }
    )

    resp = session.get(ENDPOINT_URL, timeout=30)
    resp.raise_for_status()
    payload = resp.json()

    products = []
    for category in payload.get("categories") or []:
        products.extend(category.get("products") or [])

    rows: list[dict] = []
    seen: set[tuple[str, str]] = set()

    for product in products:
        plan_name = clean_text(product.get("title"))
        detail_url = urljoin(LISTING_URL, product.get("link") or "")
        key = (plan_name or "", detail_url)

        if not plan_name or not detail_url.startswith(("http://", "https://")) or key in seen:
            continue
        seen.add(key)

        brochure_url, last_updated = extract_detail_fields(session, detail_url)

        row = {field: None for field in SCHEMA_KEYS}
        row["Plan_ID"] = product.get("id") or None
        row["Plan_Name"] = plan_name
        row["Provider_Company"] = "Prudential Hong Kong Limited"
        row["Coverage_Description"] = clean_text(product.get("description"))
        row["Plan_Description"] = clean_text(product.get("description"))
        row["URL"] = detail_url
        row["Last_Updated"] = last_updated
        row["Product_Brochure_route"] = brochure_url

        rows.append(row)

    if len(rows) != TRUE_PRODUCT_NUMBER:
        raise RuntimeError(f"Expected {TRUE_PRODUCT_NUMBER} rows, got {len(rows)}")

    for row in rows:
        if set(row.keys()) != set(SCHEMA_KEYS):
            raise RuntimeError("Schema keys mismatch")
        if not row["Plan_Name"] or not row["URL"]:
            raise RuntimeError("Plan_Name and URL are required")
        brochure = row["Product_Brochure_route"]
        if brochure is not None and not brochure.startswith(("http://", "https://")):
            raise RuntimeError("Product_Brochure_route must be absolute http(s) URL")

    return rows


def main() -> None:
    rows = build_rows()
    OUTPUT_JSON.parent.mkdir(parents=True, exist_ok=True)
    OUTPUT_JSON.write_text(json.dumps(rows, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


if __name__ == "__main__":
    main()
