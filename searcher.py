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
    # 人力銀行
    "104.com.tw", "1111.com.tw", "518.com.tw", "yes123.com.tw",
    # 社群媒體
    "facebook.com", "linkedin.com", "instagram.com", "twitter.com",
    "x.com", "youtube.com", "line.me", "threads.net",
    # 搜尋 / 入口
    "google.com", "google.com.tw", "duckduckgo.com", "bing.com", "yahoo.com",
    # 電商 / 購物
    "shopee.tw", "ruten.com.tw", "pchome.com.tw", "amazon.com",
    "momo.com.tw", "rakuten.com.tw",
    # 商業查詢 / 公司登記
    "findcompany.com.tw", "twincn.com", "gcis.nat.gov.tw",
    "company.g0v.tw", "opengovtw.com",
    # 新聞 / 媒體
    "wikipedia.org", "apple.com", "news.com.tw",
    # 地圖
    "maps.google.com", "openstreetmap.org",
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


def _startpage_search(query: str) -> Optional[str]:
    """用 Startpage（Google proxy）搜尋，回傳第一個有效非黑名單網址"""
    url = f"https://www.startpage.com/search?q={quote(query)}"
    resp = _get(url)
    if not resp:
        return None

    soup = BeautifulSoup(resp.text, "html.parser")
    for a in soup.select(".result-link"):
        href = a.get("href", "")
        if not href.startswith("http"):
            continue
        parsed = urlparse(href)
        domain = parsed.netloc.lstrip("www.")
        if not domain or any(skip in domain for skip in SKIP_DOMAINS):
            continue
        return f"https://{parsed.netloc}"
    return None


def _ddg_search(query: str) -> Optional[str]:
    """用 DDG Lite 搜尋，備援用"""
    url = f"https://lite.duckduckgo.com/lite/?q={quote(query)}"
    resp = _get(url)
    if not resp:
        return None

    soup = BeautifulSoup(resp.text, "html.parser")
    for tr in soup.find_all("tr"):
        text = tr.get_text(strip=True)
        m = re.search(r'([\w\-]+\.(com\.tw|tw|com|net|org|io)[\w/\-\.]*)', text)
        if not m:
            continue
        raw = m.group(1).split("/")[0]
        domain = raw.lstrip("www.")
        if not domain or any(skip in domain for skip in SKIP_DOMAINS):
            continue
        return f"https://www.{domain}" if not raw.startswith("www.") else f"https://{raw}"
    return None


_COMPANY_SUFFIXES = re.compile(
    r'(股份有限公司|有限公司|股份有限|有限|國際有限|實業有限|科技有限|生技有限'
    r'|Co\.,? ?Ltd\.?|Inc\.?|Corp\.?|LLC\.?)$',
    re.IGNORECASE
)

def _short_name(name: str) -> str:
    """去掉常見公司後綴，取得簡短名稱用於搜尋"""
    return _COMPANY_SUFFIXES.sub("", name).strip()


def _search_ddg_website(company_name: str) -> Optional[str]:
    """嘗試多種搜尋策略找到公司官網"""
    short = _short_name(company_name)
    queries = [
        f"{company_name} 官網",
        f"{short} 官網",
        f"{short} 官方網站",
        f"{short} official website",
        short,
    ]
    # 去重（短名和全名相同時）
    seen = set()
    deduped = [q for q in queries if q not in seen and not seen.add(q)]

    for query in deduped:
        result = _startpage_search(query) or _ddg_search(query)
        if result:
            log.info(f"[{company_name}] 找到官網: {result} (query: {query})")
            return result
        time.sleep(0.5)

    log.warning(f"[{company_name}] 所有搜尋策略均無結果")
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
    回傳 (emails, sources) 其中 sources 是 {email: 來源頁面URL}
    """
    if not website_url:
        return [], {}

    visited: set[str] = set()
    email_sources: dict[str, str] = {}  # email → 找到的頁面 URL
    base_domain = urlparse(website_url).netloc

    all_links = _collect_links(website_url, base_domain)
    priority = [l for l in all_links if any(h in l.lower() for h in CONTACT_PAGE_HINTS)]
    others = [l for l in all_links if l not in priority]
    queue = [website_url] + priority + others

    for url in queue:
        if url in visited or len(visited) >= PAGE_CRAWL_LIMIT:
            break
        visited.add(url)
        found = _extract_emails_from_page(url)
        for email in found:
            if email not in email_sources:
                email_sources[email] = url
        if found:
            log.info(f"  {url} → {found}")

    filtered = _filter_emails(set(email_sources.keys()))
    sorted_emails = sorted(filtered)
    filtered_sources = {e: email_sources[e] for e in sorted_emails}
    return sorted_emails, filtered_sources


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
