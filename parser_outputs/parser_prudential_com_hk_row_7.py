#!/usr/bin/env python3
"""Row-scoped parser for Prudential HK health medical tab (row 7)."""

from __future__ import annotations

import json
import re
from html import unescape
from pathlib import Path
import sys
from typing import Any
from urllib.parse import urljoin, urlparse

import requests
from bs4 import BeautifulSoup

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))
from api_configgg import API_KEY, API_URL  # required API config source

LISTING_URL = "https://www.prudential.com.hk/sc/products/health/#medical-1-tab"
ENDPOINT_URL = (
    "https://www.prudential.com.hk/content/prudential-aem-lbu/phkl/zh_cn/products/health/"
    "jcr:content/root/containerextension/column0/containerextension_543280936/column0/"
    "containerextension_c/column0/productcomparisonsel.model.json"
)
TARGET_TAB_ID = "medical"
TRUE_PRODUCT_NUMBER = 16
OUTPUT_JSON = Path("json_outputs/insurance_rows_prudential_com_hk_row_7.json")
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


def build_root_model_url(product_url: str) -> str | None:
    parsed = urlparse(product_url)
    path = parsed.path.rstrip("/")
    if not path.startswith("/sc/"):
        return None
    content_path = "/content/prudential-aem-lbu/phkl/zh_cn/" + path[len("/sc/") :]
    return absolute_http_url(LISTING_URL, f"{content_path}/jcr:content/root.model.json")


def collect_pdf_nodes(obj: Any, out: list[dict[str, Any]]) -> None:
    if isinstance(obj, dict):
        node_url = obj.get("url")
        if isinstance(node_url, str) and ".pdf" in node_url.lower():
            out.append(
                {
                    "url": node_url,
                    "title": obj.get("title"),
                    "filename": obj.get("filename"),
                    "jcr_title": obj.get("jcr:title"),
                }
            )
        for value in obj.values():
            collect_pdf_nodes(value, out)
    elif isinstance(obj, list):
        for value in obj:
            collect_pdf_nodes(value, out)


def pick_brochure_from_nodes(nodes: list[dict[str, Any]], detail_url: str) -> str | None:
    if not nodes:
        return None

    def score(node: dict[str, Any]) -> int:
        text = " ".join(str(node.get(k) or "") for k in ("title", "filename", "jcr_title")).lower()
        node_url = str(node.get("url") or "").lower()
        s = 0
        if "产品小册子" in text or "product brochure" in text or "brochure" in text:
            s += 100
        if "香港" in text or "/sc/" in node_url:
            s += 15
        if "澳门" in text or "/macau/" in node_url:
            s -= 20
        if "/brochure/" in node_url:
            s += 10
        return s

    best = sorted(nodes, key=score, reverse=True)[0]
    return absolute_http_url(detail_url, best.get("url"))


def pick_brochure_url(product_url: str, session: requests.Session) -> str | None:
    if ".pdf" in product_url.lower():
        return product_url

    root_model_url = build_root_model_url(product_url)
    if not root_model_url:
        return None

    try:
        resp = session.get(root_model_url, timeout=30)
        resp.raise_for_status()
        data = resp.json()
    except Exception:
        return None

    nodes: list[dict[str, Any]] = []
    collect_pdf_nodes(data, nodes)
    return pick_brochure_from_nodes(nodes, product_url)


def extract_rows() -> list[dict[str, Any]]:
    session = requests.Session()
    # Keep API config sourced from api_configgg.py for consistency with pipeline requirements.
    session.headers.update({"X-Api-Base": API_URL, "X-Api-Key-Present": str(bool(API_KEY))})

    listing_data = session.get(ENDPOINT_URL, timeout=30).json()
    categories = listing_data.get("categories", [])
    category = next((c for c in categories if c.get("tabLabelId") == TARGET_TAB_ID), None)
    if not category:
        raise RuntimeError(f"Cannot find target category: {TARGET_TAB_ID}")

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
        rows.append({k: row.get(k) for k in SCHEMA_KEYS})

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
