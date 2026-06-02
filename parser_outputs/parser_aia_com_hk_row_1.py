#!/usr/bin/env python3
"""Row-scoped parser for AIA Hong Kong save products listing (row 1)."""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from typing import Any
from urllib.parse import parse_qs, urljoin, urlparse

import requests
from bs4 import BeautifulSoup

LISTING_URL = "https://www.aia.com.hk/en/products/save?highlights=&product=&productFeatures=&priceMin=0&priceMax=1000&sortBy=popularity&filterTab="
BASE_URL = "https://www.aia.com.hk"
OUTPUT_JSON = "json_outputs/insurance_rows_aia_com_hk_row_1.json"

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


@dataclass
class QueryFilters:
    highlights: list[str]
    product: list[str]
    product_features: list[str]
    price_min: float | None
    price_max: float | None
    sort_by: str


def _split_csv_param(raw: str | None) -> list[str]:
    if not raw:
        return []
    return [part.strip() for part in raw.split(",") if part.strip()]


def _as_float(raw: str | None) -> float | None:
    if raw is None or raw == "":
        return None
    try:
        return float(raw)
    except ValueError:
        return None


def _parse_filters(listing_url: str) -> QueryFilters:
    qs = parse_qs(urlparse(listing_url).query, keep_blank_values=True)
    return QueryFilters(
        highlights=_split_csv_param((qs.get("highlights") or [""])[0]),
        product=_split_csv_param((qs.get("product") or [""])[0]),
        product_features=_split_csv_param((qs.get("productFeatures") or [""])[0]),
        price_min=_as_float((qs.get("priceMin") or [None])[0]),
        price_max=_as_float((qs.get("priceMax") or [None])[0]),
        sort_by=((qs.get("sortBy") or [""])[0] or "").strip().lower(),
    )


def _extract_search_endpoint(html: str) -> tuple[str, str | None]:
    soup = BeautifulSoup(html, "html.parser")
    container = soup.find(attrs={"data-search-endpoint": True})
    if container is None:
        raise RuntimeError("Could not find data-search-endpoint on listing page.")

    endpoint = container.get("data-search-endpoint")
    if not endpoint:
        raise RuntimeError("data-search-endpoint attribute was empty.")

    page_title = soup.title.get_text(" ", strip=True) if soup.title else ""
    provider_company = page_title.split("|")[-1].strip() if "|" in page_title else None
    return urljoin(BASE_URL, endpoint), (provider_company or None)


def _as_list(value: Any) -> list[Any]:
    if value is None:
        return []
    return value if isinstance(value, list) else [value]


def _extract_numeric(value: Any) -> float | None:
    if value is None:
        return None
    if isinstance(value, (int, float)):
        return float(value)
    if isinstance(value, str):
        match = re.search(r"-?\d+(?:\.\d+)?", value.replace(",", ""))
        if match:
            try:
                return float(match.group(0))
            except ValueError:
                return None
    return None


def _passes_filters(product: dict[str, Any], filters: QueryFilters) -> bool:
    highlights = set(_as_list(product.get("highlight")))
    subcats = set(_as_list(product.get("subcategory")))
    features = set(_as_list(product.get("features")))

    if filters.highlights and not highlights.intersection(filters.highlights):
        return False
    if filters.product and not subcats.intersection(filters.product):
        return False
    if filters.product_features and not features.intersection(filters.product_features):
        return False

    from_price = _extract_numeric(product.get("fromPrice"))
    if from_price is not None:
        if filters.price_min is not None and from_price < filters.price_min:
            return False
        if filters.price_max is not None and from_price > filters.price_max:
            return False

    return True


def _sort_products(products: list[dict[str, Any]], sort_by: str) -> list[dict[str, Any]]:
    if sort_by == "popularity":
        return sorted(
            products,
            key=lambda p: (
                -(_extract_numeric(p.get("popularity")) or -1e12),
                (p.get("title") or p.get("name") or "").lower(),
            ),
        )
    if sort_by in {"recent", "latest", "recency", "mostrecent", "date"}:
        return sorted(
            products,
            key=lambda p: (
                p.get("dateUpdated") or p.get("dateCreated") or "",
                (p.get("title") or p.get("name") or "").lower(),
            ),
            reverse=True,
        )
    return sorted(products, key=lambda p: (p.get("title") or p.get("name") or "").lower())


def _normalize_brochure_path(href: str | None) -> str | None:
    if not href:
        return None
    href = href.strip()
    if not href:
        return None
    if href.startswith("http://") or href.startswith("https://"):
        parsed = urlparse(href)
        if parsed.netloc and "aia.com.hk" not in parsed.netloc:
            return None
        return parsed.path or None
    return href


def _fallback_brochure_from_detail(html: str, product_path: str) -> str | None:
    soup = BeautifulSoup(html, "html.parser")
    slug = (product_path.rsplit("/", 1)[-1] or "").replace(".html", "").lower()
    slug_tokens = [token for token in slug.split("-") if token]

    # Strong signal: visible link text exactly "Brochure".
    for a in soup.find_all("a", href=True):
        href = a.get("href", "")
        if ".pdf" not in href.lower():
            continue
        txt = " ".join(a.get_text(" ", strip=True).split()).lower()
        if txt == "brochure":
            return _normalize_brochure_path(href)

    # Fallback: PDF path containing slug tokens.
    candidates: list[str] = []
    for a in soup.find_all("a", href=True):
        href = a.get("href", "")
        if ".pdf" not in href.lower():
            continue
        low = href.lower()
        token_hits = sum(1 for tok in slug_tokens if tok and tok in low)
        if token_hits >= max(2, len(slug_tokens) // 2):
            candidates.append(href)

    if candidates:
        candidates = sorted(set(candidates))
        return _normalize_brochure_path(candidates[0])

    return None


def _ensure_brochure_reachable(session: requests.Session, brochure_path: str | None) -> str | None:
    if brochure_path is None:
        return None
    brochure_url = urljoin(BASE_URL, brochure_path)
    try:
        response = session.get(brochure_url, timeout=30, stream=True)
        ok = response.status_code < 400
        response.close()
        return brochure_path if ok else None
    except requests.RequestException:
        return None


def _make_row(
    product: dict[str, Any],
    provider_company: str | None,
    product_url: str,
    brochure_route: str | None,
) -> dict[str, Any]:
    row = {
        "Plan_ID": product.get("id") or None,
        "Plan_Name": product.get("title") or product.get("name") or None,
        "Provider_Company": provider_company,
        "Monthly_Premium_Min": _extract_numeric(product.get("fromPrice")),
        "Monthly_Premium_Max": None,
        "Deductible_Range": None,
        "Out_of_Pocket_Max_Est": None,
        "Cost_Tier": None,
        "Copays_Description": None,
        "Coverage_Description": None,
        "Network_Type": None,
        "Geographic_Coverage": None,
        "Campus_Compatible": None,
        "Min_Age": _extract_numeric(product.get("fromAge")),
        "Max_Age": _extract_numeric(product.get("toAge")),
        "Student_Eligible": None,
        "International_Student_Compatible": None,
        "Overall_Rating": None,
        "Plan_Description": product.get("description") or None,
        "URL": product_url,
        "Last_Updated": product.get("dateUpdated") or product.get("dateCreated") or None,
        "Product_Brochure_route": brochure_route,
    }
    return {key: row.get(key, None) for key in SCHEMA_KEYS}


def run() -> list[dict[str, Any]]:
    session = requests.Session()
    session.headers.update(
        {
            "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
            "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36",
            "Accept-Language": "en-US,en;q=0.9",
        }
    )

    listing_response = session.get(LISTING_URL, timeout=30)
    listing_response.raise_for_status()
    endpoint_url, provider_company = _extract_search_endpoint(listing_response.text)

    products_response = session.get(endpoint_url, timeout=30)
    products_response.raise_for_status()
    endpoint_data = products_response.json()
    raw_products = endpoint_data.get("products") or []

    filters = _parse_filters(LISTING_URL)
    filtered_products = [p for p in raw_products if _passes_filters(p, filters)]
    ordered_products = _sort_products(filtered_products, filters.sort_by)

    seen: set[tuple[str, str]] = set()
    rows: list[dict[str, Any]] = []

    for product in ordered_products:
        plan_name = (product.get("title") or product.get("name") or "").strip()
        product_path = product.get("path") or ""
        if not plan_name or not product_path:
            continue

        product_url = urljoin(BASE_URL, product_path)
        canonical_key = (product_url.rstrip("/"), " ".join(plan_name.lower().split()))
        if canonical_key in seen:
            continue
        seen.add(canonical_key)

        detail_html = ""
        try:
            detail_response = session.get(product_url, timeout=30)
            detail_response.raise_for_status()
            detail_html = detail_response.text
        except requests.RequestException:
            detail_html = ""

        brochure_route: str | None = None
        brochure_list = product.get("brochureUrls") or []
        if isinstance(brochure_list, list) and brochure_list:
            first = brochure_list[0]
            if isinstance(first, dict):
                brochure_route = _normalize_brochure_path(first.get("value"))
            elif isinstance(first, str):
                brochure_route = _normalize_brochure_path(first)

        if brochure_route is None and detail_html:
            brochure_route = _fallback_brochure_from_detail(detail_html, product_path)

        brochure_route = _ensure_brochure_reachable(session, brochure_route)

        row = _make_row(product, provider_company, product_url, brochure_route)
        if row["Plan_Name"] and row["URL"]:
            rows.append(row)

    return rows


def main() -> None:
    rows = run()
    with open(OUTPUT_JSON, "w", encoding="utf-8") as f:
        json.dump(rows, f, ensure_ascii=False, indent=2)


if __name__ == "__main__":
    main()
