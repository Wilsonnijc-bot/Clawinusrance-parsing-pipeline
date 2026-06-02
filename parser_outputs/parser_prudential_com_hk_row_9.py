#!/usr/bin/env python3
"""Row-scoped parser for Prudential HK life products (row 9)."""

from __future__ import annotations

import html
import json
import re
from pathlib import Path
from typing import Any
from urllib.parse import urljoin

import requests
from bs4 import BeautifulSoup

try:
    # Required config source when API/model config is needed by pipeline conventions.
    from api_configgg import API_KEY, API_URL  # noqa: F401
except Exception:  # pragma: no cover
    API_KEY = None
    API_URL = None

LISTING_URL = "https://www.prudential.com.hk/sc/products/life/"
BASE_URL = "https://www.prudential.com.hk"
MODEL_PATH = (
    "/content/prudential-aem-lbu/phkl/zh_cn/products/life/jcr:content/root/"
    "containerextension/column0/containerextension/column0/"
    "containerextension_c/column0/productcomparisonsel.model.json"
)
MODEL_URL = urljoin(BASE_URL, MODEL_PATH)
OUT_PATH = Path("json_outputs/insurance_rows_prudential_com_hk_row_9.json")

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


def clean_text(value: Any) -> str | None:
    if value is None:
        return None
    text = str(value)
    text = re.sub(r"<[^>]+>", " ", text)
    text = html.unescape(text)
    text = re.sub(r"\s+", " ", text).strip()
    return text or None


def get_provider_company(listing_html: str) -> str | None:
    for pat in [r"保诚保险有限公司", r"Prudential Hong Kong Limited", r"Prudential"]:
        m = re.search(pat, listing_html, flags=re.I)
        if m:
            return m.group(0)
    return None


def extract_brochure_url(session: requests.Session, detail_url: str) -> str | None:
    if detail_url.lower().endswith(".pdf"):
        return detail_url if detail_url.startswith("http") else urljoin(BASE_URL, detail_url)

    resp = session.get(detail_url, timeout=30)
    resp.raise_for_status()
    soup = BeautifulSoup(resp.text, "html.parser")

    candidates: list[tuple[int, str]] = []
    for a in soup.select("a.cmp-download__action[href]"):
        href = (a.get("href") or "").strip()
        if not href or ".pdf" not in href.lower():
            continue

        data_layer = a.get("data-cmp-data-layer") or ""
        media_name = ""
        if data_layer:
            try:
                media_name = str(json.loads(data_layer).get("media_name") or "")
            except Exception:
                media_name = ""

        joined_text = " ".join([" ".join(a.stripped_strings), media_name]).strip()
        lower_blob = f"{href} {joined_text}".lower()

        score = 0
        if "product-brochure" in lower_blob:
            score += 120
        if "产品小册子" in joined_text:
            score += 90
        if "brochure" in lower_blob:
            score += 50
        if "/sc/" in lower_blob:
            score += 15
        if "香港" in joined_text or "hong kong" in lower_blob:
            score += 10

        if "guide" in lower_blob or "flyer" in lower_blob or "单张" in joined_text:
            score -= 40
        if "macau" in lower_blob or "澳门" in joined_text:
            score -= 20

        absolute_href = urljoin(detail_url, href)
        if absolute_href.startswith("http://") or absolute_href.startswith("https://"):
            candidates.append((score, absolute_href))

    if not candidates:
        return None

    # deterministic tie-break: highest score, then lexicographically smallest URL
    candidates.sort(key=lambda x: (-x[0], x[1]))
    return candidates[0][1]


def parse() -> list[dict[str, Any]]:
    session = requests.Session()
    session.headers.update({"User-Agent": "Mozilla/5.0"})

    listing_resp = session.get(LISTING_URL, timeout=30)
    listing_resp.raise_for_status()
    provider_company = get_provider_company(listing_resp.text)

    model_resp = session.get(MODEL_URL, timeout=30)
    model_resp.raise_for_status()
    model_data = model_resp.json()

    categories = model_data.get("categories") or []
    products: list[dict[str, Any]] = []
    for cat in categories:
        products.extend(cat.get("products") or [])

    # Deduplicate by canonical URL + normalized name.
    seen: set[tuple[str, str]] = set()
    rows: list[dict[str, Any]] = []
    for product in products:
        plan_name = clean_text(product.get("title"))
        plan_link = urljoin(LISTING_URL, product.get("link") or "")
        if not plan_name or not plan_link:
            continue

        key = (plan_link.rstrip("/"), plan_name.casefold())
        if key in seen:
            continue
        seen.add(key)

        description = clean_text(product.get("description"))
        geo_tags = product.get("productTag") or None
        geo_coverage = ", ".join([clean_text(x) or "" for x in geo_tags]).strip(", ") if geo_tags else None

        brochure_url = extract_brochure_url(session, plan_link)
        if brochure_url and not brochure_url.startswith(("http://", "https://")):
            brochure_url = urljoin(plan_link, brochure_url)

        row = {
            "Plan_ID": clean_text(product.get("id")),
            "Plan_Name": plan_name,
            "Provider_Company": provider_company,
            "Monthly_Premium_Min": None,
            "Monthly_Premium_Max": None,
            "Deductible_Range": None,
            "Out_of_Pocket_Max_Est": None,
            "Cost_Tier": None,
            "Copays_Description": None,
            "Coverage_Description": description,
            "Network_Type": None,
            "Geographic_Coverage": geo_coverage,
            "Campus_Compatible": None,
            "Min_Age": None,
            "Max_Age": None,
            "Student_Eligible": None,
            "International_Student_Compatible": None,
            "Overall_Rating": None,
            "Plan_Description": description,
            "URL": plan_link,
            "Last_Updated": None,
            "Product_Brochure_route": brochure_url,
        }

        rows.append({k: row.get(k) for k in SCHEMA_KEYS})

    return rows


def main() -> None:
    rows = parse()
    OUT_PATH.parent.mkdir(parents=True, exist_ok=True)
    OUT_PATH.write_text(json.dumps(rows, ensure_ascii=False, indent=2), encoding="utf-8")


if __name__ == "__main__":
    main()
