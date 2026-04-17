"""
FastAPI 網頁服務：讓使用者在瀏覽器輸入公司名稱 → 回傳 email
支援單筆搜尋與批量上傳（SSE 即時串流進度）
"""

import json
import time
import threading
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor, as_completed
from fastapi import FastAPI, Request, HTTPException
from fastapi.responses import HTMLResponse, JSONResponse, StreamingResponse
from pydantic import BaseModel

from config import MAX_WORKERS
from searcher import search_104_website, extract_emails_from_website

app = FastAPI(title="公司 Email 搜尋器")

BULK_LIMIT = 200

# ── 快取 ──────────────────────────────────────────────────────────────────────
_cache: dict[str, dict] = {}
_cache_lock = threading.Lock()
CACHE_TTL = 3600

def cache_get(key: str):
    with _cache_lock:
        entry = _cache.get(key)
        if entry and time.time() < entry["expires"]:
            return entry["data"]
        return None

def cache_set(key: str, data: dict):
    with _cache_lock:
        _cache[key] = {"data": data, "expires": time.time() + CACHE_TTL}

# ── Rate limiting ──────────────────────────────────────────────────────────────
_rate_data: dict[str, list] = defaultdict(list)
_rate_lock = threading.Lock()
RATE_LIMIT = 10
RATE_WINDOW = 60

def check_rate_limit(ip: str, cost: int = 1) -> bool:
    now = time.time()
    with _rate_lock:
        _rate_data[ip] = [t for t in _rate_data[ip] if now - t < RATE_WINDOW]
        if len(_rate_data[ip]) + cost > RATE_LIMIT:
            return False
        _rate_data[ip].extend([now] * cost)
        return True

# ── 共用搜尋邏輯 ──────────────────────────────────────────────────────────────

def _do_search(name: str) -> dict:
    cached = cache_get(name)
    if cached:
        return {**cached, "cached": True}
    website = search_104_website(name)
    if not website:
        result = {"company_name": name, "website": None, "emails": [], "error": "找不到公司官網"}
        cache_set(name, result)
        return {**result, "cached": False}
    emails = extract_emails_from_website(website)
    result = {
        "company_name": name,
        "website": website,
        "emails": emails,
        "error": None if emails else "官網上找不到公開 email",
    }
    cache_set(name, result)
    return {**result, "cached": False}

# ── API：單筆搜尋 ──────────────────────────────────────────────────────────────

class SearchRequest(BaseModel):
    company_name: str

@app.post("/api/search")
async def search_email(req: SearchRequest, request: Request):
    ip = request.client.host
    if not check_rate_limit(ip):
        raise HTTPException(status_code=429, detail="請求太頻繁，請稍後再試（每分鐘限 10 次）")
    name = req.company_name.strip()
    if not name:
        raise HTTPException(status_code=400, detail="請輸入公司名稱")
    if len(name) > 100:
        raise HTTPException(status_code=400, detail="公司名稱過長")
    return JSONResponse(_do_search(name))

# ── API：批量搜尋（SSE 串流）─────────────────────────────────────────────────

class BulkRequest(BaseModel):
    companies: list[str]

@app.post("/api/bulk-search")
async def bulk_search(req: BulkRequest, request: Request):
    ip = request.client.host
    companies = [c.strip() for c in req.companies if c.strip()][:BULK_LIMIT]
    if not companies:
        raise HTTPException(status_code=400, detail="請輸入至少一間公司名稱")
    cost = min(len(companies), RATE_LIMIT)
    if not check_rate_limit(ip, cost):
        raise HTTPException(status_code=429, detail="請求太頻繁，請稍後再試")

    def event_stream():
        total = len(companies)
        completed = 0
        with ThreadPoolExecutor(max_workers=MAX_WORKERS) as pool:
            future_map = {pool.submit(_do_search, name): name for name in companies}
            for future in as_completed(future_map):
                completed += 1
                try:
                    data = future.result()
                except Exception as e:
                    name = future_map[future]
                    data = {"company_name": name, "website": None, "emails": [],
                            "error": str(e), "cached": False}
                payload = json.dumps({**data, "progress": completed, "total": total},
                                     ensure_ascii=False)
                yield f"data: {payload}\n\n"
        yield "data: [DONE]\n\n"

    return StreamingResponse(event_stream(), media_type="text/event-stream",
                             headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})

# ── 首頁 HTML ─────────────────────────────────────────────────────────────────

HTML = r"""<!DOCTYPE html>
<html lang="zh-TW">
<head>
  <meta charset="UTF-8">
  <meta name="viewport" content="width=device-width, initial-scale=1.0">
  <title>公司 Email 搜尋器</title>
  <style>
    *, *::before, *::after { box-sizing: border-box; margin: 0; padding: 0; }

    body {
      font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", "Helvetica Neue", sans-serif;
      background: #f0f2f5;
      min-height: 100vh;
      color: #111827;
    }

    /* ── Header ── */
    header {
      background: #fff;
      border-bottom: 1px solid #e5e7eb;
      padding: 0 32px;
      height: 60px;
      display: flex;
      align-items: center;
      gap: 10px;
    }
    header .logo {
      font-size: 1.1rem;
      font-weight: 700;
      color: #2563eb;
    }
    header .logo-sub {
      font-size: 0.85rem;
      color: #9ca3af;
    }

    /* ── Main layout ── */
    .main {
      max-width: 1100px;
      margin: 32px auto;
      padding: 0 24px;
      display: grid;
      grid-template-columns: 320px 1fr;
      gap: 24px;
      align-items: start;
    }

    /* ── Panels ── */
    .panel {
      background: #fff;
      border-radius: 12px;
      border: 1px solid #e5e7eb;
      padding: 24px;
    }
    .panel-title {
      font-size: 0.95rem;
      font-weight: 700;
      color: #111;
      margin-bottom: 16px;
      padding-bottom: 12px;
      border-bottom: 1px solid #f3f4f6;
    }

    /* ── Form elements ── */
    label { display: block; font-size: 0.82rem; font-weight: 600; color: #374151; margin-bottom: 6px; }
    input[type="text"], textarea, select {
      width: 100%;
      padding: 10px 14px;
      border: 1.5px solid #d1d5db;
      border-radius: 8px;
      font-size: 0.92rem;
      outline: none;
      font-family: inherit;
      transition: border-color 0.2s;
      background: #fff;
    }
    input[type="text"]:focus, textarea:focus { border-color: #2563eb; }
    textarea { resize: vertical; min-height: 160px; line-height: 1.5; }

    .btn {
      display: inline-flex;
      align-items: center;
      justify-content: center;
      gap: 6px;
      padding: 10px 20px;
      border: none;
      border-radius: 8px;
      font-size: 0.92rem;
      font-weight: 600;
      cursor: pointer;
      transition: background 0.15s, opacity 0.15s;
      white-space: nowrap;
      user-select: none;
    }
    .btn:disabled { opacity: 0.55; cursor: not-allowed; }
    .btn-primary { background: #2563eb; color: #fff; }
    .btn-primary:not(:disabled):hover { background: #1d4ed8; }
    .btn-secondary { background: #f3f4f6; color: #374151; border: 1px solid #d1d5db; }
    .btn-secondary:not(:disabled):hover { background: #e5e7eb; }
    .btn-full { width: 100%; margin-top: 12px; }

    /* ── Upload area ── */
    .upload-zone {
      border: 2px dashed #d1d5db;
      border-radius: 8px;
      padding: 18px;
      text-align: center;
      color: #9ca3af;
      font-size: 0.85rem;
      cursor: pointer;
      transition: border-color 0.2s, background 0.2s, color 0.2s;
      margin-bottom: 10px;
      user-select: none;
    }
    .upload-zone:hover, .upload-zone.over {
      border-color: #2563eb;
      background: #eff6ff;
      color: #2563eb;
    }
    .upload-zone svg { display: block; margin: 0 auto 6px; }

    /* ── Divider ── */
    .or-divider {
      display: flex;
      align-items: center;
      gap: 10px;
      color: #9ca3af;
      font-size: 0.78rem;
      margin: 10px 0;
    }
    .or-divider::before, .or-divider::after {
      content: '';
      flex: 1;
      height: 1px;
      background: #e5e7eb;
    }

    /* ── Progress ── */
    .progress-wrap {
      margin-top: 16px;
      display: none;
    }
    .progress-track {
      height: 6px;
      background: #e5e7eb;
      border-radius: 99px;
      overflow: hidden;
      margin-bottom: 6px;
    }
    .progress-fill {
      height: 100%;
      background: #2563eb;
      border-radius: 99px;
      transition: width 0.3s ease;
      width: 0%;
    }
    .progress-label {
      font-size: 0.8rem;
      color: #6b7280;
    }

    /* ── Result area ── */
    .result-area { min-height: 200px; }
    .placeholder {
      height: 200px;
      display: flex;
      align-items: center;
      justify-content: center;
      color: #d1d5db;
      font-size: 0.9rem;
      flex-direction: column;
      gap: 8px;
    }
    .placeholder svg { opacity: 0.4; }

    /* ── Single result ── */
    .website-chip {
      display: inline-flex;
      align-items: center;
      gap: 6px;
      background: #eff6ff;
      border: 1px solid #bfdbfe;
      color: #1d4ed8;
      border-radius: 6px;
      padding: 6px 12px;
      font-size: 0.85rem;
      margin-bottom: 14px;
      word-break: break-all;
    }
    .website-chip a { color: inherit; text-decoration: none; }
    .website-chip a:hover { text-decoration: underline; }

    .email-cards { display: flex; flex-direction: column; gap: 6px; }
    .email-card {
      display: flex;
      align-items: center;
      justify-content: space-between;
      background: #f9fafb;
      border: 1px solid #e5e7eb;
      border-radius: 8px;
      padding: 10px 14px;
    }
    .email-addr { font-size: 0.9rem; font-family: monospace; color: #111; word-break: break-all; }
    .copy-btn {
      flex-shrink: 0;
      margin-left: 12px;
      padding: 4px 12px;
      background: #fff;
      border: 1px solid #d1d5db;
      border-radius: 6px;
      font-size: 0.78rem;
      font-weight: 600;
      cursor: pointer;
      color: #374151;
      transition: all 0.15s;
    }
    .copy-btn:hover { background: #f3f4f6; }
    .copy-btn.ok { background: #d1fae5; border-color: #6ee7b7; color: #065f46; }

    .alert-box {
      border-radius: 8px;
      padding: 12px 16px;
      font-size: 0.88rem;
    }
    .alert-error { background: #fef2f2; border: 1px solid #fecaca; color: #b91c1c; }
    .alert-warn  { background: #fffbeb; border: 1px solid #fde68a; color: #92400e; }

    /* ── Bulk table ── */
    .result-header {
      display: flex;
      align-items: center;
      justify-content: space-between;
      margin-bottom: 12px;
    }
    .result-header .stat { font-size: 0.85rem; color: #6b7280; }
    .result-header .stat strong { color: #111; }

    .table-wrap { overflow-x: auto; }
    table { width: 100%; border-collapse: collapse; font-size: 0.85rem; }
    thead th {
      text-align: left;
      padding: 9px 12px;
      background: #f9fafb;
      border-bottom: 2px solid #e5e7eb;
      font-weight: 600;
      color: #374151;
      white-space: nowrap;
    }
    tbody td { padding: 9px 12px; border-bottom: 1px solid #f3f4f6; vertical-align: middle; }
    tbody tr:last-child td { border-bottom: none; }
    tbody tr:hover td { background: #fafafa; }

    .badge {
      display: inline-block;
      border-radius: 4px;
      padding: 2px 8px;
      font-size: 0.75rem;
      font-weight: 600;
    }
    .badge-ok   { background: #d1fae5; color: #065f46; }
    .badge-fail { background: #fef2f2; color: #b91c1c; }

    .email-pills { display: flex; flex-wrap: wrap; gap: 4px; }
    .email-pill {
      background: #eff6ff;
      border: 1px solid #bfdbfe;
      color: #1e40af;
      border-radius: 4px;
      padding: 2px 9px;
      font-size: 0.78rem;
      font-family: monospace;
      cursor: pointer;
      transition: background 0.15s;
    }
    .email-pill:hover { background: #dbeafe; }
    .email-pill.ok { background: #d1fae5; border-color: #6ee7b7; color: #065f46; }

    /* ── Spinner ── */
    .spin {
      display: inline-block;
      width: 15px; height: 15px;
      border: 2px solid currentColor;
      border-top-color: transparent;
      border-radius: 50%;
      animation: spin 0.7s linear infinite;
      flex-shrink: 0;
    }
    @keyframes spin { to { transform: rotate(360deg); } }

    .hint { font-size: 0.78rem; color: #9ca3af; margin-top: 6px; }

    @media (max-width: 700px) {
      .main { grid-template-columns: 1fr; }
    }
  </style>
</head>
<body>

<header>
  <span class="logo">Email Finder</span>
  <span class="logo-sub">公司官網 Email 搜尋器</span>
</header>

<div class="main">

  <!-- ── 左欄：輸入 ── -->
  <div style="display:flex;flex-direction:column;gap:16px;">

    <!-- 單筆搜尋 -->
    <div class="panel">
      <div class="panel-title">單筆搜尋</div>
      <label for="single-input">公司或品牌名稱</label>
      <input type="text" id="single-input" placeholder="例：銨誌科技有限公司" autocomplete="off">
      <button class="btn btn-primary btn-full" id="single-btn" onclick="singleSearch()">搜尋</button>
    </div>

    <!-- 批量搜尋 -->
    <div class="panel">
      <div class="panel-title">批量搜尋</div>

      <!-- 上傳區 -->
      <div class="upload-zone" id="upload-zone">
        <svg width="24" height="24" fill="none" stroke="currentColor" stroke-width="1.5" viewBox="0 0 24 24">
          <path stroke-linecap="round" stroke-linejoin="round" d="M3 16.5v2.25A2.25 2.25 0 0 0 5.25 21h13.5A2.25 2.25 0 0 0 21 18.75V16.5m-13.5-9L12 3m0 0 4.5 4.5M12 3v13.5"/>
        </svg>
        拖曳 CSV / TXT，或點此上傳
      </div>
      <input type="file" id="file-input" accept=".csv,.txt" style="display:none">

      <div class="or-divider">或直接貼上</div>

      <label for="bulk-input">公司名稱（一行一間）</label>
      <textarea id="bulk-input" placeholder="台積電&#10;鴻海精密&#10;聯發科技"></textarea>
      <p class="hint">每次最多 200 間</p>

      <div style="display:flex;gap:8px;margin-top:12px;">
        <button class="btn btn-primary" style="flex:1" id="bulk-btn" type="button">開始搜尋</button>
        <button class="btn btn-secondary" id="dl-btn" type="button" style="display:none" onclick="downloadCSV()">下載 CSV</button>
      </div>

      <div class="progress-wrap" id="progress-wrap">
        <div class="progress-track"><div class="progress-fill" id="progress-fill"></div></div>
        <div class="progress-label" id="progress-label">準備中...</div>
      </div>
    </div>

  </div>

  <!-- ── 右欄：結果 ── -->
  <div class="panel result-area" id="result-panel">
    <div class="placeholder" id="placeholder">
      <svg width="48" height="48" fill="none" stroke="currentColor" stroke-width="1.2" viewBox="0 0 24 24">
        <path stroke-linecap="round" stroke-linejoin="round" d="m21.75 6.75-10.5 10.5m0 0-4.5-4.5m4.5 4.5L3.75 3.75"/>
      </svg>
      結果將顯示在這裡
    </div>
    <div id="result-content" style="display:none"></div>
  </div>

</div>

<script>
// ── Upload zone ───────────────────────────────────────────────────────────────
const uploadZone = document.getElementById('upload-zone');
const fileInput  = document.getElementById('file-input');

uploadZone.addEventListener('click', () => fileInput.click());
uploadZone.addEventListener('dragover',  e => { e.preventDefault(); uploadZone.classList.add('over'); });
uploadZone.addEventListener('dragleave', () => uploadZone.classList.remove('over'));
uploadZone.addEventListener('drop', e => {
  e.preventDefault();
  uploadZone.classList.remove('over');
  readFile(e.dataTransfer.files[0]);
});
fileInput.addEventListener('change', e => readFile(e.target.files[0]));

function readFile(file) {
  if (!file) return;
  const reader = new FileReader();
  reader.onload = ev => {
    const lines = ev.target.result.split(/\r?\n/).map(l => l.split(',')[0].trim()).filter(Boolean);
    document.getElementById('bulk-input').value = lines.join('\n');
  };
  reader.readAsText(file, 'UTF-8');
}

// ── Single search ─────────────────────────────────────────────────────────────
document.getElementById('single-input').addEventListener('keydown', e => {
  if (e.key === 'Enter') singleSearch();
});

async function singleSearch() {
  const name = document.getElementById('single-input').value.trim();
  if (!name) return;
  const btn = document.getElementById('single-btn');
  btn.disabled = true;
  btn.innerHTML = '<span class="spin"></span> 搜尋中...';
  showPlaceholder('搜尋中...');
  try {
    const res  = await fetch('/api/search', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ company_name: name }),
    });
    const data = await res.json();
    showResult(renderSingle(data));
  } catch {
    showResult('<div class="alert-box alert-error">連線失敗，請稍後再試</div>');
  } finally {
    btn.disabled = false;
    btn.textContent = '搜尋';
  }
}

function renderSingle(d) {
  let h = '';
  if (d.website) h += `<div class="website-chip"><a href="${esc(d.website)}" target="_blank" rel="noopener">${esc(d.website)}</a></div>`;
  if (d.emails && d.emails.length) {
    h += `<div style="font-size:.82rem;font-weight:600;color:#374151;margin-bottom:8px;">找到 ${d.emails.length} 個 Email</div>`;
    h += '<div class="email-cards">';
    for (const e of d.emails)
      h += `<div class="email-card"><span class="email-addr">${esc(e)}</span><button class="copy-btn" data-email="${esc(e)}">複製</button></div>`;
    h += '</div>';
  } else {
    h += `<div class="alert-box alert-warn">${esc(d.error || '找不到 Email')}</div>`;
  }
  return h;
}

// ── Bulk search ───────────────────────────────────────────────────────────────
let bulkResults = [];

document.getElementById('bulk-btn').addEventListener('click', bulkSearch);

async function bulkSearch() {
  const raw = document.getElementById('bulk-input').value;
  const companies = raw.split('\n').map(l => l.trim()).filter(Boolean).slice(0, 200);
  if (!companies.length) {
    document.getElementById('bulk-input').focus();
    return;
  }

  bulkResults = [];
  const btn          = document.getElementById('bulk-btn');
  const progressWrap = document.getElementById('progress-wrap');
  const progressFill = document.getElementById('progress-fill');
  const progressLbl  = document.getElementById('progress-label');
  const dlBtn        = document.getElementById('dl-btn');

  btn.disabled = true;
  btn.innerHTML = '<span class="spin"></span> 搜尋中...';
  progressWrap.style.display = 'block';
  progressFill.style.width = '0%';
  dlBtn.style.display = 'none';
  showPlaceholder('搜尋中，結果將陸續出現...');

  try {
    const res = await fetch('/api/bulk-search', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ companies }),
    });
    if (!res.ok) {
      const err = await res.json();
      showResult(`<div class="alert-box alert-error">${esc(err.detail || '搜尋失敗')}</div>`);
      return;
    }

    const reader  = res.body.getReader();
    const decoder = new TextDecoder();
    let buffer = '';

    while (true) {
      const { done, value } = await reader.read();
      if (done) break;
      buffer += decoder.decode(value, { stream: true });
      const lines = buffer.split('\n');
      buffer = lines.pop();
      for (const line of lines) {
        if (!line.startsWith('data: ')) continue;
        const raw = line.slice(6).trim();
        if (raw === '[DONE]') break;
        try {
          const d = JSON.parse(raw);
          progressFill.style.width = (d.progress / d.total * 100) + '%';
          progressLbl.textContent  = `${d.progress} / ${d.total} 完成`;
          bulkResults.push(d);
          renderBulkTable();
        } catch {}
      }
    }
    dlBtn.style.display = 'inline-flex';
  } catch (e) {
    showResult(`<div class="alert-box alert-error">連線失敗：${esc(e.message)}</div>`);
  } finally {
    btn.disabled = false;
    btn.textContent = '開始搜尋';
  }
}

function renderBulkTable() {
  const found = bulkResults.filter(r => r.emails && r.emails.length > 0).length;
  let h = `<div class="result-header">
    <div class="stat">共 <strong>${bulkResults.length}</strong> 筆 &nbsp;·&nbsp; 找到 Email <strong>${found}</strong> 筆</div>
  </div>
  <div class="table-wrap">
  <table>
    <thead><tr><th>#</th><th>公司名稱</th><th>Email</th><th>狀態</th></tr></thead>
    <tbody>`;
  for (let i = 0; i < bulkResults.length; i++) {
    const r = bulkResults[i];
    const ok = r.emails && r.emails.length > 0;
    const emailsHtml = ok
      ? '<div class="email-pills">' + r.emails.map(e => `<span class="email-pill" data-email="${esc(e)}">${esc(e)}</span>`).join('') + '</div>'
      : '—';
    const badge = ok
      ? `<span class="badge badge-ok">✓ ${r.emails.length} 個</span>`
      : `<span class="badge badge-fail">${esc(r.error || '無')}</span>`;
    h += `<tr><td style="color:#9ca3af">${i+1}</td><td>${esc(r.company_name)}</td><td>${emailsHtml}</td><td>${badge}</td></tr>`;
  }
  h += '</tbody></table></div>';
  showResult(h);
}

// ── Download CSV ──────────────────────────────────────────────────────────────
function downloadCSV() {
  const rows = [['公司名稱', '官網', 'Email', '備註']];
  for (const r of bulkResults) {
    if (r.emails && r.emails.length) {
      for (const e of r.emails) rows.push([r.company_name, r.website || '', e, '']);
    } else {
      rows.push([r.company_name, r.website || '', '', r.error || '無結果']);
    }
  }
  const csv  = '\uFEFF' + rows.map(r => r.map(v => `"${String(v).replace(/"/g,'""')}"`).join(',')).join('\n');
  const blob = new Blob([csv], { type: 'text/csv;charset=utf-8' });
  const a    = document.createElement('a');
  a.href     = URL.createObjectURL(blob);
  a.download = 'email_results.csv';
  a.click();
}

// ── Copy handlers (event delegation) ─────────────────────────────────────────
document.getElementById('result-content').addEventListener('click', e => {
  const btn  = e.target.closest('.copy-btn');
  const pill = e.target.closest('.email-pill');
  const el   = btn || pill;
  if (!el) return;
  const email = el.dataset.email;
  if (!email) return;
  navigator.clipboard.writeText(email).then(() => {
    el.classList.add('ok');
    const orig = el.textContent;
    if (btn) el.textContent = '已複製！';
    setTimeout(() => { el.classList.remove('ok'); if (btn) el.textContent = '複製'; }, 2000);
  });
});

// ── Helpers ───────────────────────────────────────────────────────────────────
function showPlaceholder(msg) {
  document.getElementById('placeholder').style.display = 'flex';
  document.getElementById('placeholder').innerHTML = `<span style="color:#9ca3af">${msg}</span>`;
  document.getElementById('result-content').style.display = 'none';
}
function showResult(html) {
  document.getElementById('placeholder').style.display = 'none';
  const rc = document.getElementById('result-content');
  rc.style.display = 'block';
  rc.innerHTML = html;
}
function esc(s) {
  return String(s).replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;').replace(/"/g,'&quot;');
}
</script>
</body>
</html>"""


@app.get("/", response_class=HTMLResponse)
async def index():
    return HTML
