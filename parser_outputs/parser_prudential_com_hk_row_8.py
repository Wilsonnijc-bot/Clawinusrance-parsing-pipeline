import json
import os
import re
import sys
from html import unescape
from urllib.parse import urljoin

import requests
from bs4 import BeautifulSoup

ROOT_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT_DIR not in sys.path:
    sys.path.append(ROOT_DIR)

from api_configgg import API_KEY, API_URL  # required config source (not used for scraping)

LISTING_URL = "https://www.prudential.com.hk/sc/products/health/#accident-and-disability-2-tab"
ENDPOINT_URL = (
    "https://www.prudential.com.hk/content/prudential-aem-lbu/phkl/zh_cn/products/health/"
    "jcr:content/root/containerextension/column0/containerextension_543280936/column0/"
    "containerextension_c/column0/productcomparisonsel.model.json"
)
OUTPUT_JSON = "json_outputs/insurance_rows_prudential_com_hk_row_8.json"
TRUE_PRODUCT_NUMBER = 6
PROVIDER = "Prudential Hong Kong"


session = requests.Session()
session.headers.update(
    {
        "User-Agent": "Mozilla/5.0 (compatible; insurance-parser/1.0)",
        "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
    }
)


def html_to_text(value):
    if not value:
        return None
    text = re.sub(r"<[^>]+>", " ", value)
    text = unescape(text)
    text = re.sub(r"\s+", " ", text).strip()
    return text or None


def looks_like_pdf(url):
    u = (url or "").lower()
    return ".pdf" in u or u.endswith(".pdf")


def get_final_url(url):
    try:
        r = session.get(url, timeout=40, allow_redirects=True)
        return r.url
    except Exception:
        return url


def choose_best_pdf(candidates):
    if not candidates:
        return None
    unique = []
    seen = set()
    for c in candidates:
        if c and c not in seen:
            unique.append(c)
            seen.add(c)
    unique.sort(key=lambda x: (".coredownload." in x.lower(), len(x), x))
    return unique[0]


def extract_pdf_from_detail(detail_url):
    try:
        r = session.get(detail_url, timeout=40, allow_redirects=True)
    except Exception:
        return None

    if looks_like_pdf(r.url):
        return r.url

    html = r.text
    soup = BeautifulSoup(html, "html.parser")

    pdfs = []
    for a in soup.find_all("a", href=True):
        href = a.get("href", "").strip()
        if ".pdf" in href.lower():
            pdfs.append(urljoin(r.url, href))

    # fallback: regex scan in raw HTML
    if not pdfs:
        for match in re.findall(r'https?://[^"\'\s>]+?\.pdf(?:[^"\'\s>]*)?', html, flags=re.I):
            pdfs.append(match)
        for match in re.findall(r'/(?:[^"\'\s>]+?\.pdf(?:[^"\'\s>]*)?)', html, flags=re.I):
            pdfs.append(urljoin(r.url, match))

    return choose_best_pdf(pdfs)


def build_rows():
    payload = session.get(ENDPOINT_URL, timeout=40).json()
    categories = payload.get("categories") or []
    target = None
    for c in categories:
        if c.get("tabLabelId") == "accident-and-disability":
            target = c
            break
    if target is None:
        raise RuntimeError("Target tab not found: accident-and-disability")

    products = target.get("products") or []
    rows = []

    for product in products:
        plan_name = (product.get("title") or "").strip()
        raw_link = (product.get("link") or "").strip()
        if not plan_name or not raw_link:
            continue

        plan_url = urljoin(LISTING_URL, raw_link)

        if looks_like_pdf(plan_url):
            brochure_url = get_final_url(plan_url)
            if not brochure_url.startswith("http"):
                brochure_url = urljoin(plan_url, brochure_url)
        else:
            brochure_url = extract_pdf_from_detail(plan_url)
            if brochure_url and not brochure_url.startswith("http"):
                brochure_url = urljoin(plan_url, brochure_url)

        desc = html_to_text(product.get("description"))

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
            "Coverage_Description": desc,
            "Network_Type": None,
            "Geographic_Coverage": None,
            "Campus_Compatible": None,
            "Min_Age": None,
            "Max_Age": None,
            "Student_Eligible": None,
            "International_Student_Compatible": None,
            "Overall_Rating": None,
            "Plan_Description": desc,
            "URL": plan_url,
            "Last_Updated": None,
            "Product_Brochure_route": brochure_url if brochure_url and brochure_url.startswith("http") else None,
        }
        rows.append(row)

    # deterministic ordering by original endpoint order and uniqueness
    deduped = []
    seen = set()
    for row in rows:
        key = (row["URL"], row["Plan_Name"])
        if key not in seen:
            deduped.append(row)
            seen.add(key)

    if len(deduped) != TRUE_PRODUCT_NUMBER:
        raise RuntimeError(f"Expected {TRUE_PRODUCT_NUMBER} products, got {len(deduped)}")

    return deduped


def main():
    _ = (API_URL, API_KEY)  # ensures api_configgg.py is used as required
    rows = build_rows()
    with open(OUTPUT_JSON, "w", encoding="utf-8") as f:
        json.dump(rows, f, ensure_ascii=False, indent=2)


if __name__ == "__main__":
    main()
