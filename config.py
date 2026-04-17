# ── 執行參數 ─────────────────────────────────────────────────────────────────
MAX_WORKERS = 3          # 並發公司數量（太高容易被 104 封鎖）
REQUEST_DELAY = 1.5      # 每次 HTTP 請求間隔（秒）
PAGE_CRAWL_LIMIT = 5     # 每個官網最多爬幾個子頁面找 email

# ── 過濾掉的無效 email domain ──────────────────────────────────────────────
EXCLUDE_EMAIL_DOMAINS = {
    "example.com", "sentry.io", "wixpress.com", "jquery.com",
    "w3.org", "schema.org", "google.com", "facebook.com",
    "yourdomain.com", "company.com", "email.com",
}

# ── HTTP Headers（模擬瀏覽器）─────────────────────────────────────────────
HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/120.0.0.0 Safari/537.36"
    ),
    "Accept-Language": "zh-TW,zh;q=0.9,en;q=0.8",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
}
