import os
import re
import time
import json
import datetime as dt
from typing import List, Tuple, Dict

import requests
import urllib3
from bs4 import BeautifulSoup
from flask import Flask, request, jsonify

# 關閉 requests 對 verify=False 的警告（TWSE/TPEx 憑證鏈在某些環境會驗證失敗）
urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

# ========= Flask =========
app = Flask(__name__)

# ========= LINE =========
LINE_CHANNEL_ACCESS_TOKEN = os.getenv("LINE_CHANNEL_ACCESS_TOKEN", "").strip()
LINE_USER_ID = os.getenv("LINE_USER_ID", "").strip()

def line_push(text: str) -> Tuple[bool, str]:
    """用 Messaging API 直接打 HTTPS 送訊息（不依賴 line-bot-sdk）"""
    if not LINE_CHANNEL_ACCESS_TOKEN or not LINE_USER_ID:
        return False, "Missing LINE env"
    url = "https://api.line.me/v2/bot/message/push"
    headers = {
        "Authorization": f"Bearer {LINE_CHANNEL_ACCESS_TOKEN}",
        "Content-Type": "application/json"
    }
    payload = {
        "to": LINE_USER_ID,
        "messages": [{"type": "text", "text": text}]
    }
    r = requests.post(url, headers=headers, data=json.dumps(payload), timeout=15)
    ok = (200 <= r.status_code < 300)
    return ok, f"{r.status_code} {r.text[:200]}"

# ========= 環境參數（預設值）=========
def env_float(key: str, default: float) -> float:
    v = os.getenv(key, str(default)).strip()
    return float(v) if v != "" else default

def env_int(key: str, default: int) -> int:
    v = os.getenv(key, str(default)).strip()
    return int(v) if v != "" else default

MIN_CHANGE_PCT     = env_float("MIN_CHANGE_PCT",  0.5)     # 當日漲幅門檻(%)
MIN_VOLUME         = env_int  ("MIN_VOLUME",      100)     # 當日量門檻（股）
MAX_LINES_PER_MSG  = env_int  ("MAX_LINES_PER_MSG", 25)    # 每則訊息最多行數
MAX_CHARS_PER_MSG  = env_int  ("MAX_CHARS_PER_MSG", 1800)  # 每則訊息最多字元（留安全餘裕）

# ========= 代號清單快取 =========
SYMBOLS_CACHE: Dict[str, dict] = {
    "ts": 0.0,
    "items": []  # 每筆：{"code": "2330", "name": "台積電", "market": "上市|上櫃|ETF", "yahoo": "2330.TW"}
}
CACHE_TTL_SEC = 60 * 60 * 6  # 6 小時

TWSE_LISTED_URL  = "https://isin.twse.com.tw/isin/C_public.jsp?strMode=2"  # 上市（含 ETF）
TPEX_OTC_URL     = "https://isin.twse.com.tw/isin/C_public.jsp?strMode=4"  # 上櫃

# ========= 工具：抓上市/上櫃 HTML 表格，回傳 (code,name,market) =========
def _fetch_isin_table(url: str, market_label: str) -> List[Tuple[str, str, str]]:
    # verify=False 解決憑證鏈驗證問題
    r = requests.get(url, timeout=20, verify=False)
    r.raise_for_status()
    soup = BeautifulSoup(r.text, "html.parser")
    rows = soup.select("table.h4 tr") or soup.find_all("tr")
    out: List[Tuple[str, str, str]] = []
    for tr in rows:
        tds = [td.get_text(strip=True) for td in tr.find_all("td")]
        if not tds or len(tds) < 2:
            continue
        raw = tds[0]  # 例：2330 台積電
        m = re.match(r"^([A-Z0-9]+)\s+(.+)$", raw)
        if not m:
            continue
        code, name = m.group(1), m.group(2)
        # 只收常見的股票與 ETF 代號
        if re.fullmatch(r"\d{4}[A-Z]?", code) or re.fullmatch(r"[A-Z]{2}\d{2}", code):
            out.append((code, name, market_label))
    return out

def _yahoo_symbol(code: str, market: str) -> str:
    # 上櫃用 .TWO，其餘（上市/ETF）用 .TW
    suffix = ".TWO" if market == "上櫃" else ".TW"
    return f"{code}{suffix}"

def get_all_symbols(force: bool = False) -> List[dict]:
    now = time.time()
    if not force and (now - SYMBOLS_CACHE["ts"] < CACHE_TTL_SEC) and SYMBOLS_CACHE["items"]:
        return SYMBOLS_CACHE["items"]

    items: List[dict] = []
    try:
        listed = _fetch_isin_table(TWSE_LISTED_URL, "上市")     # 含 ETF
        otc    = _fetch_isin_table(TPEX_OTC_URL, "上櫃")
        for code, name, market in listed + otc:
            items.append({
                "code": code,
                "name": name,
                "market": market,
                "yahoo": _yahoo_symbol(code, market)
            })
        # 去重（以 code 為主）
        seen = set()
        uniq: List[dict] = []
        for it in items:
            if it["code"] in seen:
                continue
            seen.add(it["code"])
            uniq.append(it)
        SYMBOLS_CACHE["ts"] = now
        SYMBOLS_CACHE["items"] = uniq
        return uniq
    except Exception as e:
        # 失敗時仍回舊快取（若有）
        if SYMBOLS_CACHE["items"]:
            return SYMBOLS_CACHE["items"]
        raise e

# ========= 抓 Yahoo 當日變化（簡易、免金鑰）=========
def fetch_change_pct_and_volume(yahoo_symbol: str) -> Tuple[float, int]:
    """
    回傳：(當日漲跌幅%, 當日成交量)
    先試 1d/1m 內盤；若拿不到改 5d/1d（最近日線）。
    """
    urls = [
        f"https://query1.finance.yahoo.com/v8/finance/chart/{yahoo_symbol}?range=1d&interval=1m",
        f"https://query1.finance.yahoo.com/v8/finance/chart/{yahoo_symbol}?range=5d&interval=1d",
    ]
    last_close = None
    last_price = None
    last_volume = 0

    for url in urls:
        r = requests.get(url, timeout=10)
        if r.status_code != 200:
            continue
        j = r.json()
        result = j.get("chart", {}).get("result", [])
        if not result:
            continue
        indicators = result[0].get("indicators", {})
        quote = (indicators.get("quote") or [{}])[0]
        closes = quote.get("close") or []
        volumes = quote.get("volume") or []
        # 最後有效值
        for i in range(len(closes) - 1, -1, -1):
            c = closes[i]
            v = volumes[i] if i < len(volumes) else 0
            if c is not None:
                last_price = c
                last_volume = int(v or 0)
                break
        # 前一筆視為昨收
        for i in range(len(closes) - 2, -1, -1):
            c = closes[i]
            if c is not None:
                last_close = c
                break
        if last_price is not None and last_close is not None:
            break

    if not last_price or not last_close:
        return 0.0, 0
    change_pct = (last_price - last_close) / last_close * 100.0
    return round(change_pct, 2), last_volume

# ========= 過濾 + 排序 =========
def pick_rising_all(
    min_change_pct: float,
    min_volume: int,
    top_k: int = 20
) -> Dict[str, List[Tuple[str, str, float, int]]]:
    """
    掃全市場，回 { "上市": [...], "上櫃": [...], "ETF": [...] }
    內容每筆：(code, name, chg%, vol)
    """
    symbols = get_all_symbols()
    groups = {"上市": [], "上櫃": [], "ETF": []}

    for s in symbols:
        code, name, market, ysym = s["code"], s["name"], s["market"], s["yahoo"]
        # 粗略判定 ETF：名稱含「ETF」
        sub_group = "ETF" if ("ETF" in name.upper()) else ("上櫃" if market == "上櫃" else "上市")
        try:
            chg, vol = fetch_change_pct_and_volume(ysym)
        except Exception:
            continue
        if chg >= min_change_pct and vol >= min_volume:
            groups[sub_group].append((code, name, chg, vol))

    for k in groups:
        groups[k].sort(key=lambda x: (x[2], x[3]), reverse=True)
        groups[k] = groups[k][:top_k]
    return groups

# ========= 格式化成多則訊息 =========
def make_messages(groups: Dict[str, List[Tuple[str, str, float, int]]]) -> List[str]:
    now = dt.datetime.now(dt.timezone(dt.timedelta(hours=8)))
    ts = now.strftime("%Y-%m-%d %H:%M")
    parts: List[str] = []
    for label in ("上市", "上櫃", "ETF"):
        rows = groups.get(label, [])
        if not rows:
            continue
        lines = [f"【{ts} 起漲清單】📈 {label}"]
        for i, (code, name, chg, vol) in enumerate(rows, 1):
            lines.append(f"{i}. {code} {name}  漲幅 {chg:.2f}%  量 {vol:,}")
        msg = "\n".join(lines)
        parts.append(msg)

    if not parts:
        parts = [f"【{ts} 起漲清單】\n尚無符合條件的個股（或資料未更新）"]
    # 分段（字數/行數保護）
    final: List[str] = []
    for p in parts:
        block = []
        curr = 0
        for line in p.splitlines():
            if len("\n".join(block + [line])) > MAX_CHARS_PER_MSG or len(block) >= MAX_LINES_PER_MSG:
                final.append("\n".join(block))
                block = [line]
            else:
                block.append(line)
        if block:
            final.append("\n".join(block))
    return final

# ========= 路由 =========
@app.get("/")
def home():
    return "OK", 200

@app.get("/refresh-list")
def refresh_list():
    try:
        n = len(get_all_symbols(force=True))
        return f"Refreshed symbols OK: {n}", 200
    except Exception as e:
        app.logger.exception(e)
        return str(e), 500

@app.get("/list")
def list_info():
    items = get_all_symbols(force=False)
    return jsonify({
        "count": len(items),
        "sample": items[:10],
        "cached_at": SYMBOLS_CACHE["ts"]
    })

@app.get("/daily-push")
def daily_push():
    try:
        groups = pick_rising_all(
            min_change_pct=MIN_CHANGE_PCT,
            min_volume=MIN_VOLUME,
            top_k=10
        )
        messages = make_messages(groups)
        errors = []
        for m in messages:
            ok, info = line_push(m)
            if not ok:
                errors.append(info)
            time.sleep(0.4)
        if errors:
            return "Sent with errors: " + " | ".join(errors), 206
        return "Push sent!", 200
    except Exception as e:
        app.logger.exception(e)
        return str(e), 500

# 簡易 webhook（選用）
@app.post("/callback")
def callback():
    # 保留給 LINE Webhook（若有需要）
    return "ok", 200

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.getenv("PORT", "10000")))