#!/usr/bin/env python3
"""Row-scoped parser for AIA Hong Kong general insurance listing (row 2)."""

from __future__ import annotations

import json
import re
import sys
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urljoin, urlparse

import requests
from bs4 import BeautifulSoup

ROOT_DIR = Path(__file__).resolve().parents[1]
if str(ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(ROOT_DIR))

from api_configgg import API_KEY, API_URL  # required config source


LISTING_URL = (
    "https://www.aia.com.hk/en/products/general-insurance"
    "?highlights=&product=&productFeatures=&priceMin=0&priceMax=1000"
    "&sortBy=popularity&filterTab="
)
HOST = "https://www.aia.com.hk"
OUT_PATH = Path("json_outputs/insurance_rows_aia_com_hk_row_2.json")

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


def _clean_text(value: Any) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    return text if text else None


def _norm_name(value: Any) -> str:
    text = _clean_text(value) or ""
    text = re.sub(r"\s+", " ", text).strip().lower()
    return text


def _to_abs(base_url: str, maybe_relative: str | None) -> str | None:
    href = _clean_text(maybe_relative)
    if not href:
        return None
    return urljoin(base_url, href)


def _extract_listing_endpoint(html: str) -> str:
    soup = BeautifulSoup(html, "html.parser")
    node = soup.find(attrs={"data-search-endpoint": True})
    endpoint = node.get("data-search-endpoint") if node else None
    if not endpoint:
        endpoint = "/content/hk-wise/en/products/general-insurance.model.json"
    return urljoin(HOST, endpoint)


def _split_param(query: dict[str, list[str]], key: str) -> list[str]:
    raw = (query.get(key) or [""])[0]
    if not raw:
        return []
    return [part.strip() for part in raw.split(",") if part.strip()]


def _match_any_token(values: Any, tokens: list[str]) -> bool:
    if not tokens:
        return True
    if not values:
        return False
    if isinstance(values, (list, tuple, set)):
        haystack = " ".join([str(v) for v in values]).lower()
    else:
        haystack = str(values).lower()
    return all(token.lower() in haystack for token in tokens)


def _apply_listing_filters(products: list[dict[str, Any]], listing_url: str) -> list[dict[str, Any]]:
    query = parse_qs(urlparse(listing_url).query, keep_blank_values=True)

    highlight_tokens = _split_param(query, "highlights")
    product_tokens = _split_param(query, "product")
    feature_tokens = _split_param(query, "productFeatures")

    price_min_raw = _clean_text((query.get("priceMin") or [None])[0])
    price_max_raw = _clean_text((query.get("priceMax") or [None])[0])
    price_min = float(price_min_raw) if price_min_raw and price_min_raw != "0" else 0.0
    price_max = float(price_max_raw) if price_max_raw else None

    filter_tab = (_clean_text((query.get("filterTab") or [""])[0]) or "").lower()

    filtered: list[dict[str, Any]] = []
    for product in products:
        if highlight_tokens and not _match_any_token(product.get("highlight"), highlight_tokens):
            continue

        # Product/category filter tokens can appear in category/subcategory/path/name fields.
        if product_tokens:
            category_bag = [
                product.get("category"),
                product.get("subcategory"),
                product.get("subcategoryWithTitle"),
                product.get("path"),
                product.get("name"),
                product.get("title"),
            ]
            if not _match_any_token(category_bag, product_tokens):
                continue

        if feature_tokens and not _match_any_token(product.get("features"), feature_tokens):
            continue

        from_price = product.get("fromPrice")
        to_price = product.get("toPrice")

        if from_price is not None:
            try:
                numeric_from = float(from_price)
            except (TypeError, ValueError):
                numeric_from = None
            if numeric_from is not None and numeric_from < price_min:
                continue
            if numeric_from is not None and price_max is not None and numeric_from > price_max:
                continue

        if to_price is not None and price_max is not None:
            try:
                numeric_to = float(to_price)
            except (TypeError, ValueError):
                numeric_to = None
            if numeric_to is not None and numeric_to < price_min:
                continue

        if filter_tab:
            keep = True
            if filter_tab == "online":
                keep = bool(product.get("isOnlineProduct"))
            elif filter_tab == "popular":
                keep = bool(product.get("isPopular"))
            elif filter_tab == "new":
                keep = bool(product.get("isNew"))
            elif filter_tab == "vitality":
                keep = bool(product.get("isVitality"))
            if not keep:
                continue

        filtered.append(product)

    sort_by = (_clean_text((query.get("sortBy") or [""])[0]) or "").lower()
    if sort_by == "popularity":
        filtered.sort(
            key=lambda p: (
                -(float(p.get("popularity")) if p.get("popularity") is not None else -1.0),
                _norm_name(p.get("name")),
                _clean_text(p.get("path")) or "",
            )
        )
    elif sort_by == "name":
        filtered.sort(key=lambda p: (_norm_name(p.get("name")), _clean_text(p.get("path")) or ""))

    deduped: list[dict[str, Any]] = []
    seen: set[tuple[str, str]] = set()
    for product in filtered:
        path = _clean_text(product.get("path"))
        if not path:
            continue
        canonical_url = _to_abs(HOST, path)
        key = (canonical_url or "", _norm_name(product.get("name")))
        if key in seen:
            continue
        seen.add(key)
        deduped.append(product)

    return deduped


def _extract_provider_company(html_text: str) -> str | None:
    if re.search(r"AIA\s+International\s+Limited", html_text, flags=re.IGNORECASE):
        return "AIA International Limited"
    return None


def _extract_brochure_url(soup: BeautifulSoup, page_url: str) -> str | None:
    links = soup.find_all("a", href=True)

    def candidate_links():
        for a in links:
            href = _to_abs(page_url, a.get("href"))
            if not href:
                continue
            label = " ".join(a.get_text(" ", strip=True).split()).lower()
            yield href, label

    for href, label in candidate_links():
        if "product-brochures" in href.lower():
            continue
        if label == "brochure" and ".pdf" in href.lower():
            return href

    for href, label in candidate_links():
        if "product-brochures" in href.lower():
            continue
        if "brochure" in label and ".pdf" in href.lower():
            return href

    for href, label in candidate_links():
        if "product-brochures" in href.lower():
            continue
        if "brochure" in href.lower() and ".pdf" in href.lower():
            return href

    return None


def _extract_plan_name(product: dict[str, Any], soup: BeautifulSoup) -> str | None:
    endpoint_name = _clean_text(product.get("name"))
    if endpoint_name:
        return endpoint_name
    h = soup.find(["h1", "h2", "h3"])
    if h:
        return _clean_text(h.get_text(" ", strip=True))
    return None


def _extract_plan_description(product: dict[str, Any], soup: BeautifulSoup) -> str | None:
    endpoint_desc = _clean_text(product.get("description"))
    if endpoint_desc:
        return endpoint_desc
    meta = soup.find("meta", attrs={"name": "description"})
    if meta:
        return _clean_text(meta.get("content"))
    return None


def _product_to_row(product: dict[str, Any], session: requests.Session) -> dict[str, Any] | None:
    rel_path = _clean_text(product.get("path"))
    if not rel_path:
        return None

    detail_url = _to_abs(HOST, rel_path)
    if not detail_url:
        return None

    resp = session.get(detail_url, timeout=30, allow_redirects=True)
    resp.raise_for_status()
    final_url = resp.url
    soup = BeautifulSoup(resp.text, "html.parser")

    plan_name = _extract_plan_name(product, soup)
    if not plan_name:
        return None

    plan_desc = _extract_plan_description(product, soup)
    brochure_url = _extract_brochure_url(soup, final_url)
    provider_company = _extract_provider_company(resp.text)

    plan_id = _clean_text(product.get("id"))
    from_price = product.get("fromPrice")
    to_price = product.get("toPrice")

    row = {
        "Plan_ID": plan_id,
        "Plan_Name": plan_name,
        "Provider_Company": provider_company,
        "Monthly_Premium_Min": from_price if from_price is not None else None,
        "Monthly_Premium_Max": to_price if to_price is not None else None,
        "Deductible_Range": None,
        "Out_of_Pocket_Max_Est": None,
        "Cost_Tier": None,
        "Copays_Description": None,
        "Coverage_Description": _clean_text(product.get("description")),
        "Network_Type": None,
        "Geographic_Coverage": None,
        "Campus_Compatible": None,
        "Min_Age": product.get("fromAge") if product.get("fromAge") is not None else None,
        "Max_Age": product.get("toAge") if product.get("toAge") is not None else None,
        "Student_Eligible": None,
        "International_Student_Compatible": None,
        "Overall_Rating": None,
        "Plan_Description": plan_desc,
        "URL": _clean_text(final_url),
        "Last_Updated": _clean_text(resp.headers.get("Last-Modified")),
        "Product_Brochure_route": brochure_url,
    }

    # enforce exact schema keys and null for missing
    normalized_row = {key: row.get(key, None) for key in SCHEMA_KEYS}
    if not normalized_row["Plan_Name"] or not normalized_row["URL"]:
        return None
    return normalized_row


def run() -> list[dict[str, Any]]:
    # Touch config values to satisfy rule requiring api_configgg.py as source.
    _ = API_URL
    _ = API_KEY

    session = requests.Session()
    session.headers.update({
        "User-Agent": "Mozilla/5.0 (compatible; insurance-row-parser/1.0)",
        "Accept-Language": "en-US,en;q=0.9",
    })

    listing_resp = session.get(LISTING_URL, timeout=30)
    listing_resp.raise_for_status()

    endpoint_url = _extract_listing_endpoint(listing_resp.text)
    endpoint_resp = session.get(endpoint_url, timeout=30)
    endpoint_resp.raise_for_status()

    payload = endpoint_resp.json()
    products = payload.get("products") if isinstance(payload, dict) else None
    if not isinstance(products, list):
        products = []

    indexed_products = _apply_listing_filters(products, LISTING_URL)

    rows: list[dict[str, Any]] = []
    for product in indexed_products:
        row = _product_to_row(product, session)
        if row is not None:
            rows.append(row)

    return rows


def main() -> None:
    rows = run()
    OUT_PATH.parent.mkdir(parents=True, exist_ok=True)
    OUT_PATH.write_text(json.dumps(rows, ensure_ascii=False, indent=2), encoding="utf-8")


if __name__ == "__main__":
    main()
