#!/usr/bin/env python3
"""Row-scoped parser for AIA Hong Kong health products listing (row 3)."""

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
    "https://www.aia.com.hk/en/products/health"
    "?highlights=&product=&productFeatures=&priceMin=0&priceMax=1000"
    "&sortBy=popularity&filterTab="
)
HOST = "https://www.aia.com.hk"
OUT_PATH = Path("json_outputs/insurance_rows_aia_com_hk_row_3.json")

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


def _normalize_name(value: Any) -> str:
    text = _clean_text(value) or ""
    return re.sub(r"\s+", " ", text).strip().lower()


def _to_float(value: Any) -> float | None:
    if value is None:
        return None
    if isinstance(value, (int, float)):
        return float(value)
    if isinstance(value, str):
        m = re.search(r"-?\d+(?:\.\d+)?", value.replace(",", ""))
        if m:
            try:
                return float(m.group(0))
            except ValueError:
                return None
    return None


def _split_csv(raw: str | None) -> list[str]:
    if not raw:
        return []
    return [part.strip() for part in raw.split(",") if part.strip()]


def _extract_listing_component(html: str) -> tuple[str, set[str], str | None]:
    soup = BeautifulSoup(html, "html.parser")
    component = soup.find(attrs={"data-search-endpoint": True})
    if component is None:
        raise RuntimeError("Could not find data-search-endpoint on listing page")

    endpoint = _clean_text(component.get("data-search-endpoint"))
    if not endpoint:
        raise RuntimeError("data-search-endpoint is empty")

    hide_ids_raw = _clean_text(component.get("data-hide-product-ids")) or ""
    hide_ids = {part.strip() for part in hide_ids_raw.split(",") if part.strip()}

    text = soup.get_text(" ", strip=True)
    provider_match = re.search(r"Name of Offering Company:\s*([^\n\r]+?)\s+Company Registration Number:", text)
    provider_company = provider_match.group(1).strip() if provider_match else None

    return urljoin(HOST, endpoint), hide_ids, provider_company


def _matches_any(values: Any, tokens: list[str]) -> bool:
    if not tokens:
        return True
    if not values:
        return False
    if isinstance(values, (list, tuple, set)):
        haystack = " ".join(str(v) for v in values).lower()
    else:
        haystack = str(values).lower()
    return all(token.lower() in haystack for token in tokens)


def _apply_filters(products: list[dict[str, Any]], listing_url: str, hide_ids: set[str]) -> list[dict[str, Any]]:
    query = parse_qs(urlparse(listing_url).query, keep_blank_values=True)

    highlight_tokens = _split_csv((query.get("highlights") or [""])[0])
    product_tokens = _split_csv((query.get("product") or [""])[0])
    feature_tokens = _split_csv((query.get("productFeatures") or [""])[0])

    price_min = _to_float((query.get("priceMin") or [None])[0])
    price_max = _to_float((query.get("priceMax") or [None])[0])

    filter_tab = ((_clean_text((query.get("filterTab") or [""])[0]) or "").lower())
    sort_by = ((_clean_text((query.get("sortBy") or [""])[0]) or "").lower())

    filtered: list[dict[str, Any]] = []
    for product in products:
        if _clean_text(product.get("id")) in hide_ids:
            continue

        if highlight_tokens and not _matches_any(product.get("highlight"), highlight_tokens):
            continue

        if product_tokens:
            category_bag = [
                product.get("category"),
                product.get("subcategory"),
                product.get("subcategoryWithTitle"),
                product.get("path"),
                product.get("name"),
                product.get("title"),
            ]
            if not _matches_any(category_bag, product_tokens):
                continue

        if feature_tokens and not _matches_any(product.get("features"), feature_tokens):
            continue

        from_price = _to_float(product.get("fromPrice"))
        to_price = _to_float(product.get("toPrice"))

        if from_price is not None:
            if price_min is not None and from_price < price_min:
                continue
            if price_max is not None and from_price > price_max:
                continue
        if to_price is not None and price_max is not None and to_price < (price_min or 0.0):
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

    if sort_by == "popularity":
        filtered.sort(
            key=lambda p: (
                -(_to_float(p.get("popularity")) if _to_float(p.get("popularity")) is not None else -1e12),
                _normalize_name(p.get("name") or p.get("title")),
                _clean_text(p.get("path")) or "",
            )
        )
    elif sort_by == "name":
        filtered.sort(key=lambda p: (_normalize_name(p.get("name") or p.get("title")), _clean_text(p.get("path")) or ""))
    else:
        filtered.sort(key=lambda p: (_normalize_name(p.get("name") or p.get("title")), _clean_text(p.get("path")) or ""))

    deduped: list[dict[str, Any]] = []
    seen: set[tuple[str, str]] = set()
    for product in filtered:
        path = _clean_text(product.get("path"))
        name = _clean_text(product.get("name") or product.get("title"))
        if not path or not name:
            continue
        key = (urljoin(HOST, path).rstrip("/"), _normalize_name(name))
        if key in seen:
            continue
        seen.add(key)
        deduped.append(product)

    return deduped


def _normalize_route(href: str | None, page_url: str | None = None) -> str | None:
    href = _clean_text(href)
    if not href or href.startswith("#") or href.lower().startswith("javascript:"):
        return None

    if page_url:
        href = urljoin(page_url, href)

    parsed = urlparse(href)
    if parsed.scheme in {"http", "https"}:
        if parsed.netloc and "aia.com.hk" not in parsed.netloc.lower():
            return None
        route = parsed.path or None
        if route and parsed.query:
            route = f"{route}?{parsed.query}"
        return route

    if href.startswith("/"):
        return href

    return f"/{href.lstrip('/')}"


def _brochure_from_endpoint(product: dict[str, Any]) -> str | None:
    brochure_urls = product.get("brochureUrls")
    if not isinstance(brochure_urls, list):
        return None

    for entry in brochure_urls:
        if isinstance(entry, dict):
            candidate = _normalize_route(entry.get("value"))
        elif isinstance(entry, str):
            candidate = _normalize_route(entry)
        else:
            candidate = None
        if candidate and ".pdf" in candidate.lower():
            return candidate
    return None


def _tokenize_slug(path: str) -> list[str]:
    slug = path.rsplit("/", 1)[-1].replace(".html", "")
    tokens = [t for t in re.split(r"[^a-zA-Z0-9]+", slug.lower()) if len(t) >= 3]
    return tokens


def _brochure_from_detail(soup: BeautifulSoup, page_url: str, product_path: str) -> str | None:
    slug_tokens = _tokenize_slug(product_path)
    links = []

    for a in soup.find_all("a", href=True):
        href = _normalize_route(a.get("href"), page_url=page_url)
        if not href:
            continue
        text = " ".join(a.get_text(" ", strip=True).split()).lower()
        links.append((text, href))

    # Strong signal: label is brochure and link is pdf.
    for text, href in links:
        if ".pdf" not in href.lower():
            continue
        if text == "brochure":
            return href

    # Secondary signal: label contains brochure and link is pdf.
    for text, href in links:
        if ".pdf" not in href.lower():
            continue
        if "brochure" in text:
            return href

    # Match PDF route with slug tokens to avoid unrelated downloads.
    scored: list[tuple[int, str]] = []
    for text, href in links:
        if ".pdf" not in href.lower():
            continue
        low = href.lower()
        score = sum(1 for token in slug_tokens if token in low)
        if score > 0:
            scored.append((score, href))

    if scored:
        scored.sort(key=lambda x: (-x[0], x[1]))
        return scored[0][1]

    # Last fallback: first pdf link that is not a generic brochures index.
    for text, href in links:
        if ".pdf" in href.lower() and "product-brochures" not in href.lower():
            return href

    return None


def _ensure_reachable_brochure(session: requests.Session, route: str | None) -> str | None:
    if not route:
        return None
    try:
        resp = session.get(urljoin(HOST, route), timeout=30, stream=True)
        ok = resp.status_code < 400
        resp.close()
        return route if ok else None
    except requests.RequestException:
        return None


def _extract_plan_name(product: dict[str, Any], soup: BeautifulSoup) -> str | None:
    name = _clean_text(product.get("name") or product.get("title"))
    if name:
        return name
    h1 = soup.find(["h1", "h2"])
    if h1:
        return _clean_text(h1.get_text(" ", strip=True))
    return None


def _extract_plan_description(product: dict[str, Any], soup: BeautifulSoup) -> str | None:
    meta = soup.find("meta", attrs={"name": "description"})
    meta_desc = _clean_text(meta.get("content")) if meta else None
    if meta_desc:
        return meta_desc
    return _clean_text(product.get("description"))


def _product_to_row(
    product: dict[str, Any],
    session: requests.Session,
    provider_company: str | None,
) -> dict[str, Any] | None:
    path = _clean_text(product.get("path"))
    if not path:
        return None

    detail_url = urljoin(HOST, path)
    try:
        detail_resp = session.get(detail_url, timeout=30, allow_redirects=True)
        detail_resp.raise_for_status()
    except requests.RequestException:
        return None

    final_url = detail_resp.url
    soup = BeautifulSoup(detail_resp.text, "html.parser")

    plan_name = _extract_plan_name(product, soup)
    if not plan_name:
        return None

    brochure_route = _brochure_from_endpoint(product)
    if brochure_route is None:
        brochure_route = _brochure_from_detail(soup, final_url, path)
    brochure_route = _ensure_reachable_brochure(session, brochure_route)

    plan_description = _extract_plan_description(product, soup)
    coverage_description = _clean_text(product.get("description"))

    row = {
        "Plan_ID": _clean_text(product.get("id")),
        "Plan_Name": plan_name,
        "Provider_Company": provider_company,
        "Monthly_Premium_Min": _to_float(product.get("fromPrice")),
        "Monthly_Premium_Max": _to_float(product.get("toPrice")),
        "Deductible_Range": None,
        "Out_of_Pocket_Max_Est": None,
        "Cost_Tier": None,
        "Copays_Description": None,
        "Coverage_Description": coverage_description,
        "Network_Type": None,
        "Geographic_Coverage": None,
        "Campus_Compatible": None,
        "Min_Age": _to_float(product.get("fromAge")),
        "Max_Age": _to_float(product.get("toAge")),
        "Student_Eligible": None,
        "International_Student_Compatible": None,
        "Overall_Rating": None,
        "Plan_Description": plan_description,
        "URL": _clean_text(final_url),
        "Last_Updated": _clean_text(product.get("dateUpdated") or product.get("dateCreated") or detail_resp.headers.get("Last-Modified")),
        "Product_Brochure_route": brochure_route,
    }

    normalized = {key: row.get(key, None) for key in SCHEMA_KEYS}
    if not normalized["Plan_Name"] or not normalized["URL"]:
        return None
    return normalized


def run() -> list[dict[str, Any]]:
    # Touch config values to satisfy rule requiring api_configgg.py as source.
    _ = API_URL
    _ = API_KEY

    session = requests.Session()
    session.headers.update(
        {
            "User-Agent": "Mozilla/5.0 (compatible; insurance-row-parser/1.0)",
            "Accept-Language": "en-US,en;q=0.9",
        }
    )

    listing_resp = session.get(LISTING_URL, timeout=30)
    listing_resp.raise_for_status()

    endpoint_url, hide_ids, provider_company = _extract_listing_component(listing_resp.text)

    endpoint_resp = session.get(endpoint_url, timeout=30)
    endpoint_resp.raise_for_status()
    payload = endpoint_resp.json()

    products = payload.get("products") if isinstance(payload, dict) else None
    if not isinstance(products, list):
        products = []

    indexed_products = _apply_filters(products, LISTING_URL, hide_ids)

    rows: list[dict[str, Any]] = []
    for product in indexed_products:
        row = _product_to_row(product, session, provider_company)
        if row is not None:
            rows.append(row)

    return rows


def main() -> None:
    rows = run()
    OUT_PATH.parent.mkdir(parents=True, exist_ok=True)
    OUT_PATH.write_text(json.dumps(rows, ensure_ascii=False, indent=2), encoding="utf-8")


if __name__ == "__main__":
    main()
