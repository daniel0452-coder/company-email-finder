"""
搜尋公司官網（DuckDuckGo）+ 官網 email 擷取
"""

import re
import time
import logging
from typing import Optional
from urllib.parse import quote, urljoin, urlparse, parse_qs

import requests
from bs4 import BeautifulSoup

from config import HEADERS, REQUEST_DELAY, PAGE_CRAWL_LIMIT, EXCLUDE_EMAIL_DOMAINS

log = logging.getLogger(__name__)

EMAIL_REGEX = re.compile(r"[a-zA-Z0-9._%+\-]+@[a-zA-Z0-9.\-]+\.[a-zA-Z]{2,}")

CONTACT_PAGE_HINTS = [
    "contact", "contact-us", "contactus", "about", "about-us",
    "联系", "聯絡", "聯絡我們", "關於", "關於我們",
]

# 排除不太可能是官網的域名
SKIP_DOMAINS = {
    "104.com.tw", "1111.com.tw", "facebook.com", "linkedin.com",
    "instagram.com", "twitter.com", "x.com", "youtube.com",
    "line.me", "google.com", "apple.com", "wikipedia.org",
    "duckduckgo.com", "amazon.com", "shopee.tw", "ruten.com.tw",
}


def _get(url: str, timeout: int = 10) -> Optional[requests.Response]:
    time.sleep(REQUEST_DELAY)
    try:
        resp = requests.get(url, headers=HEADERS, timeout=timeout, allow_redirects=True)
        resp.raise_for_status()
        return resp
    except Exception as e:
        log.debug(f"GET {url} 失敗: {e}")
        return None


# ── 公司官網搜尋（DuckDuckGo HTML）────────────────────────────────────────────

def search_104_website(company_name: str) -> Optional[str]:
    """
    用 DuckDuckGo 搜尋公司官網，回傳 URL。
    函數名稱維持相容性，但已改用 DDG 搜尋。
    """
    return _search_ddg_website(company_name)


def _search_ddg_website(company_name: str) -> Optional[str]:
    query = f"{company_name} official website"
    url = f"https://html.duckduckgo.com/html/?q={quote(query)}&kl=tw-tzh"
    resp = _get(url)
    if not resp:
        return None

    soup = BeautifulSoup(resp.text, "html.parser")

    for result in soup.select(".result__body"):
        link_tag = result.select_one(".result__url")
        if not link_tag:
            continue

        # 從 href 的 uddg 參數取出真實 URL
        href = link_tag.get("href", "")
        if "uddg=" in href:
            qs = parse_qs(href.split("?", 1)[-1])
            real_urls = qs.get("uddg", [])
            if real_urls:
                href = real_urls[0]

        # 備用：直接用顯示文字
        if not href.startswith("http"):
            raw = link_tag.get_text(strip=True)
            href = f"https://{raw}" if raw else ""

        if not href.startswith("http"):
            continue

        parsed = urlparse(href)
        domain = parsed.netloc.lstrip("www.")
        if not domain:
            continue
        if any(skip in domain for skip in SKIP_DOMAINS):
            continue

        log.info(f"[{company_name}] DDG 找到官網: {href}")
        return f"https://{parsed.netloc}"

    log.warning(f"[{company_name}] DDG 搜尋無結果")
    return None


def _is_valid_website(url: str) -> bool:
    parsed = urlparse(url)
    skip = {"facebook.com", "linkedin.com", "instagram.com", "twitter.com",
            "youtube.com", "line.me", "google.com", "apple.com"}
    return parsed.netloc and not any(s in parsed.netloc for s in skip)


# ── 官網 email 擷取 ───────────────────────────────────────────────────────────

def extract_emails_from_website(website_url: str) -> list[str]:
    """
    爬取官網（首頁 + 聯絡頁面）擷取所有公開 email。
    """
    if not website_url:
        return []

    visited: set[str] = set()
    emails: set[str] = set()
    base_domain = urlparse(website_url).netloc

    # 第一輪：爬首頁，收集所有內部連結
    urls_to_visit = [website_url]
    all_links = _collect_links(website_url, base_domain)

    # 優先訪問聯絡頁面
    priority = [l for l in all_links if any(h in l.lower() for h in CONTACT_PAGE_HINTS)]
    others = [l for l in all_links if l not in priority]
    queue = [website_url] + priority + others

    for url in queue:
        if url in visited or len(visited) >= PAGE_CRAWL_LIMIT:
            break
        visited.add(url)
        found = _extract_emails_from_page(url)
        emails.update(found)
        if found:
            log.info(f"  {url} → {found}")

    filtered = _filter_emails(emails)
    return sorted(filtered)


def _decode_cf_email(encoded: str) -> str:
    """解碼 Cloudflare data-cfemail 屬性的混淆 email"""
    try:
        r = int(encoded[:2], 16)
        email = "".join(
            chr(int(encoded[i:i+2], 16) ^ r)
            for i in range(2, len(encoded), 2)
        )
        return email
    except Exception:
        return ""


def _detect_encoding(resp: requests.Response) -> str:
    """從 Content-Type header 或 meta charset 偵測編碼"""
    ct = resp.headers.get("Content-Type", "")
    if "charset=" in ct:
        return ct.split("charset=")[-1].strip()
    # 從 HTML meta 偵測
    snippet = resp.content[:2048].decode("ascii", errors="replace")
    m = re.search(r'charset=["\']?([\w-]+)', snippet, re.IGNORECASE)
    if m:
        return m.group(1)
    return "utf-8"


def _collect_links(url: str, base_domain: str) -> list[str]:
    resp = _get(url)
    if not resp:
        return []
    enc = _detect_encoding(resp)
    resp.encoding = enc
    soup = BeautifulSoup(resp.text, "html.parser")
    links = set()
    # 收集 <a> 連結
    for a in soup.find_all("a", href=True):
        full = urljoin(url, a["href"])
        if urlparse(full).netloc == base_domain:
            links.add(full.split("#")[0])
    # 收集同域名的 <iframe> src（聯絡頁面常用 iframe 嵌入）
    for iframe in soup.find_all("iframe", src=True):
        full = urljoin(url, iframe["src"])
        if urlparse(full).netloc == base_domain:
            links.add(full.split("#")[0])
    return list(links)


def _extract_emails_from_page(url: str) -> set[str]:
    resp = _get(url)
    if not resp:
        return set()
    enc = _detect_encoding(resp)
    resp.encoding = enc
    soup = BeautifulSoup(resp.text, "html.parser")

    emails = set()

    # 1. Cloudflare data-cfemail 混淆
    for tag in soup.find_all(attrs={"data-cfemail": True}):
        decoded = _decode_cf_email(tag["data-cfemail"])
        if decoded and EMAIL_REGEX.match(decoded):
            emails.add(decoded.lower())

    # 2. mailto: 連結
    for a in soup.find_all("a", href=True):
        if a["href"].startswith("mailto:"):
            addr = a["href"][7:].split("?")[0].strip()
            if EMAIL_REGEX.match(addr):
                emails.add(addr.lower())

    # 3. 頁面全文 regex 掃描
    text = soup.get_text(" ")
    for match in EMAIL_REGEX.findall(text):
        emails.add(match.lower())

    return emails


def _filter_emails(emails: set[str]) -> set[str]:
    result = set()
    for email in emails:
        domain = email.split("@")[-1]
        if domain not in EXCLUDE_EMAIL_DOMAINS and not domain.endswith(".png"):
            result.add(email)
    return result
