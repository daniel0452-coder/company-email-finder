#!/usr/bin/env python3
"""
公司 Email 搜尋器（爬蟲部分）

這支腳本負責：
  1. 從 104 人力銀行找到公司官網
  2. 爬取官網上所有公開 email
  3. 輸出 JSON 結果，讓 Claude 透過 MCP 處理 HubSpot 更新

用法:
  python main.py
  → 貼上公司名稱（一行一個），按 Ctrl+D 結束輸入

  python main.py companies.txt
  → 從文字檔讀取
"""

import sys
import json
import logging
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field, asdict
from typing import Optional

from config import MAX_WORKERS
from searcher import search_104_website, extract_emails_from_website

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[
        logging.FileHandler("scrape.log", encoding="utf-8"),
        logging.StreamHandler(sys.stderr),  # log 輸出到 stderr，stdout 留給 JSON
    ],
)
log = logging.getLogger(__name__)


@dataclass
class CompanyResult:
    company_name: str
    website: Optional[str] = None
    emails: list[str] = field(default_factory=list)
    error: Optional[str] = None


def process_company(company_name: str) -> CompanyResult:
    log.info(f"處理: {company_name}")
    result = CompanyResult(company_name=company_name)

    website = search_104_website(company_name)
    if not website:
        result.error = "104 找不到官網"
        log.warning(f"[{company_name}] ⚠ {result.error}")
        return result

    result.website = website
    emails = extract_emails_from_website(website)
    result.emails = emails

    if not emails:
        result.error = "官網找不到任何 email"
        log.warning(f"[{company_name}] ⚠ {result.error}")
    else:
        log.info(f"[{company_name}] ✅ 找到 {len(emails)} 個 email")

    return result


def main():
    if len(sys.argv) > 1:
        with open(sys.argv[1], encoding="utf-8") as f:
            raw = f.read()
    else:
        print("請貼上公司名稱（一行一個），完成後按 Ctrl+D：", file=sys.stderr)
        try:
            raw = sys.stdin.read()
        except KeyboardInterrupt:
            print("\n已取消", file=sys.stderr)
            return

    companies = [line.strip() for line in raw.splitlines() if line.strip()]
    if not companies:
        print("沒有輸入任何公司名稱", file=sys.stderr)
        return

    log.info(f"共 {len(companies)} 間公司，開始搜尋...")

    results: list[CompanyResult] = []
    with ThreadPoolExecutor(max_workers=MAX_WORKERS) as pool:
        future_map = {pool.submit(process_company, name): name for name in companies}
        for future in as_completed(future_map):
            try:
                results.append(future.result())
            except Exception as e:
                name = future_map[future]
                log.error(f"[{name}] 未預期錯誤: {e}")
                results.append(CompanyResult(company_name=name, error=str(e)))

    # 輸出 JSON 到 stdout，讓 Claude 讀取
    print(json.dumps([asdict(r) for r in results], ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
