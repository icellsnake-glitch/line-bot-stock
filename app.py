import os
import time
import csv
from io import StringIO
import datetime as dt
from typing import List, Tuple

import requests
from flask import Flask, request, abort
from linebot import LineBotApi, WebhookHandler
from linebot.exceptions import InvalidSignatureError
from linebot.models import MessageEvent, TextMessage, TextSendMessage

app = Flask(__name__)

# ========= 基本環境變數 =========
CHANNEL_ACCESS_TOKEN = os.getenv("LINE_CHANNEL_ACCESS_TOKEN", "").replace("\n", "").strip()
CHANNEL_SECRET       = os.getenv("LINE_CHANNEL_SECRET", "").strip()
USER_ID              = os.getenv("LINE_USER_ID", "").strip()  # 相容舊版
CRON_SECRET          = os.getenv("CRON_SECRET", "").strip()
if not CHANNEL_ACCESS_TOKEN or not CHANNEL_SECRET:
    raise RuntimeError("缺少 LINE_CHANNEL_ACCESS_TOKEN 或 LINE_CHANNEL_SECRET")

# ========= 選股參數（Environment 可調）=========
WATCHLIST         = os.getenv("WATCHLIST", "2330,2454,2317").split(",")  # 或填 ALL
UNIVERSE_CSV_URL  = os.getenv("UNIVERSE_CSV_URL", "").strip()
MAX_SCAN          = int(os.getenv("MAX_SCAN", "500"))

# 盤中門檻
MIN_CHANGE        = float(os.getenv("MIN_CHANGE_PCT", "2.0"))        # 起漲：漲幅門檻(%)
MIN_VOLUME        = int(os.getenv("MIN_VOLUME", "1000000"))          # 起漲：今日量門檻(股)

# 開盤前門檻（07:00~08:59）
MIN_CHANGE_PRE    = float(os.getenv("MIN_CHANGE_PCT_PRE", "1.5"))
MIN_VOLUME_PRE    = int(os.getenv("MIN_VOLUME_PRE", "500000"))

TOP_K             = int(os.getenv("TOP_K", "10"))                    # 顯示前N檔

# 量能條件（開關＋參數）
USE_TODAY_VOL_SPIKE   = int(os.getenv("USE_TODAY_VOL_SPIKE", "1"))     # 今日量能放大(預設開)
VOL_MA_N              = int(os.getenv("VOL_MA_N", "5"))                # 均量N日
VOL_SPIKE_RATIO       = float(os.getenv("VOL_SPIKE_RATIO", "1.5"))     # 今量/近N日均量

USE_YDAY_VOL_BREAKOUT = int(os.getenv("USE_YDAY_VOL_BREAKOUT", "0"))   # 昨量突破(預設關)
YDAY_BREAKOUT_RATIO   = float(os.getenv("YDAY_BREAKOUT_RATIO", "1.3")) # 昨量/其前N日均量

# 多人推送與分段限制
TARGET_IDS        = [x.strip() for x in os.getenv("LINE_TARGET_IDS", USER_ID).split(",") if x.strip()]
MAX_LINES_PER_MSG = int(os.getenv("MAX_LINES_PER_MSG", "12"))    # 每則最多幾檔
MAX_CHARS_PER_MSG = int(os.getenv("MAX_CHARS_PER_MSG", "3500"))  # 每則最多字數（安全邊際）

# ETF 來源（可選）
ETF_CODES_RAW     = os.getenv("ETF_CODES", "").strip()
ETF_LIST_URL      = os.getenv("ETF_LIST_URL", "").strip()

# 分群標題 emoji（可自訂）
EMOJI_LISTED = os.getenv("EMOJI_LISTED", "📊")
EMOJI_OTC    = os.getenv("EMOJI_OTC", "📈")
EMOJI_ETF    = os.getenv("EMOJI_ETF", "📦")

line_bot_api = LineBotApi(CHANNEL_ACCESS_TOKEN)
handler = WebhookHandler(CHANNEL_SECRET)

# ========= 時間工具 =========
def tw_now():
    return dt.datetime.now(dt.timezone(dt.timedelta(hours=8)))

def wait_until(target: dt.datetime):
    while True:
        if tw_now() >= target:
            return
        time.sleep(15)

def is_pre_market() -> bool:
    """台北時間 09:00 前視為開盤前"""
    t = tw_now().time()
    return t < dt.time(9, 0)

# ========= 全市場清單 =========
def load_universe(max_scan: int = None) -> list[str]:
    if max_scan is None: max_scan = MAX_SCAN

    # 1) 你自備 CSV（建議最穩；第一欄=代號）
    if UNIVERSE_CSV_URL:
        try:
            r = requests.get(UNIVERSE_CSV_URL, timeout=15)
            r.raise_for_status()
            rows = list(csv.reader(StringIO(r.text)))
            codes = []
            for row in rows:
                if not row or not row[0]: continue
                code = row[0].strip().upper()
                if code.endswith(".TW") or code.endswith(".TWO") or code.isdigit():
                    codes.append(code)
            return codes[:max_scan]
        except Exception:
            pass

    # 2) 官方上/櫃清單
    codes = []
    try:
        j = requests.get("https://openapi.twse.com.tw/v1/opendata/t187ap03_L", timeout=15).json()
        for it in j:
            code = (it.get("Code") or it.get("公司代號") or it.get("證券代號") or "").strip()
            if code and code.isdigit():
                codes.append(code)
    except Exception:
        pass
    try:
        j2 = requests.get("https://www.tpex.org.tw/openapi/v1/mopsfin_t187ap07_O", timeout=15).json()
        for it in j2:
            code = (it.get("Code") or it.get("公司代號") or it.get("證券代號") or "").strip()
            if code and code.isdigit():
                codes.append(f"{code}.TWO")
    except Exception:
        pass

    uniq, seen = [], set()
    for c in codes:
        if c not in seen:
            uniq.append(c); seen.add(c)
        if len(uniq) >= max_scan:
            break
    return uniq

# ========= ETF 清單 =========
def load_etf_set() -> set[str]:
    s = set()
    # 1) 直接填在環境變數 ETF_CODES
    if ETF_CODES_RAW:
        for c in ETF_CODES_RAW.split(","):
            c = c.strip().upper()
            if c:
                s.add(c if c.endswith(".TW") or c.endswith(".TWO") else c)
    # 2) CSV 來源
    if ETF_LIST_URL:
        try:
            r = requests.get(ETF_LIST_URL, timeout=15); r.raise_for_status()
            rows = list(csv.reader(StringIO(r.text)))
            for row in rows:
                if not row or not row[0]: continue
                c = row[0].strip().upper()
                s.add(c if c.endswith(".TW") or c.endswith(".TWO") else c)
        except Exception:
            pass
    return s

ETF_SET = load_etf_set()

# ========= Yahoo Finance 工具 =========
def _yahoo_symbol(tw_code: str) -> str:
    tw_code = tw_code.strip().upper()
    if tw_code.endswith(".TW") or tw_code.endswith(".TWO"):
        return tw_code
    return f"{tw_code}.TW"

def fetch_change_pct_and_volume(tw_code: str) -> Tuple[float, int]:
    """
    回傳：(今日漲跌幅%, 今日成交量)
    先用 1d/1m（分K），拿最後一筆；拿不到退 5d/1d（日K）。
    """
    symbol = _yahoo_symbol(tw_code)
    urls = [
        f"https://query1.finance.yahoo.com/v8/finance/chart/{symbol}?range=1d&interval=1m",
        f"https://query1.finance.yahoo.com/v8/finance/chart/{symbol}?range=5d&interval=1d",
    ]
    last_close = last_price = None
    last_volume = 0
    for url in urls:
        try:
            r = requests.get(url, timeout=12); r.raise_for_status()
            j = r.json()
            result = j.get("chart", {}).get("result", [])
            if not result: continue
            q = result[0]["indicators"]["quote"][0]
            closes = q.get("close") or []
            volumes = q.get("volume") or []
            for i in range(len(closes)-1, -1, -1):
                if closes[i] is not None:
                    last_price = closes[i]; last_volume = int(volumes[i] or 0); break
            for i in range(len(closes)-2, -1, -1):
                if closes[i] is not None:
                    last_close = closes[i]; break
            if last_price and last_close: break
        except Exception:
            continue
    if not last_price or not last_close or last_close == 0:
        return 0.0, 0
    chg_pct = round((last_price - last_close) / last_close * 100.0, 2)
    return chg_pct, last_volume

def fetch_daily_vol_series(tw_code: str, months: int = 6) -> List[int]:
    """回傳最近 months 的『日量』陣列（末端=最近交易日量，不含即時量）"""
    symbol = _yahoo_symbol(tw_code)
    r = requests.get(
        f"https://query1.finance.yahoo.com/v8/finance/chart/{symbol}?range={months}mo&interval=1d",
        timeout=12
    )
    r.raise_for_status()
    j = r.json()
    result = j.get("chart", {}).get("result", [])
    if not result: return []
    vols = result[0]["indicators"]["quote"][0].get("volume") or []
    return [int(v or 0) for v in vols if v is not None]

# ========= 量能條件 =========
def pass_volume_rules(today_vol: int, day_vols: List[int]) -> bool:
    ok_today = True
    ok_yday  = True

    if USE_TODAY_VOL_SPIKE:
        base = sum(day_vols[-VOL_MA_N:]) / VOL_MA_N if len(day_vols) >= VOL_MA_N else 0
        ok_today = (base > 0) and (today_vol / base >= VOL_SPIKE_RATIO)

    if USE_YDAY_VOL_BREAKOUT and len(day_vols) >= VOL_MA_N + 1:
        yday = day_vols[-1]
        prev_ma = sum(day_vols[-(VOL_MA_N+1):-1]) / VOL_MA_N
        ok_yday = (prev_ma > 0) and (yday / prev_ma >= YDAY_BREAKOUT_RATIO)

    return ok_today and ok_yday

# ========= 日K快篩（兩段式第一階段）=========
def quick_filter_dayline(code: str, min_change: float, min_vol: int) -> bool:
    """只用日K最後一根做粗篩，節省大量時間"""
    try:
        symbol = _yahoo_symbol(code)
        url = f"https://query1.finance.yahoo.com/v8/finance/chart/{symbol}?range=5d&interval=1d"
        r = requests.get(url, timeout=10); r.raise_for_status()
        j = r.json()
        result = j.get("chart", {}).get("result", [])
        if not result: return False
        q = result[0]["indicators"]["quote"][0]
        closes = q.get("close") or []
        vols = q.get("volume") or []
        if len(closes) < 2: return False
        last_price, prev_close = closes[-1], closes[-2]
        last_vol = int(vols[-1] or 0)
        if not last_price or not prev_close: return False
        chg = (last_price - prev_close) / prev_close * 100
        return chg >= min_change and last_vol >= min_vol
    except Exception:
        return False

# ========= 起漲邏輯（兩段式：快篩→精篩）=========
def pick_rising_stocks(codes: List[str]) -> List[str]:
    # 動態門檻（開盤前 vs 盤中）
    if is_pre_market():
        min_chg = MIN_CHANGE_PRE
        min_vol = MIN_VOLUME_PRE
    else:
        min_chg = MIN_CHANGE
        min_vol = MIN_VOLUME

    # 第一階段快篩（日K）
    candidates = [c for c in codes if quick_filter_dayline(c, min_chg, min_vol)]

    # 第二階段精篩（1m + 量能規則）
    rows = []
    for code in candidates:
        try:
            chg, today_vol = fetch_change_pct_and_volume(code)
            if chg < min_chg or today_vol < min_vol:
                continue
            day_vols = fetch_daily_vol_series(code, months=6)
            if not pass_volume_rules(today_vol, day_vols):
                continue
            rows.append((code, chg, today_vol))
        except Exception:
            continue

    rows.sort(key=lambda x: x[1], reverse=True)
    return [f"{i+1}. {c} 漲幅 {chg:.2f}% 量 {vol:,}" for i, (c, chg, vol) in enumerate(rows[:TOP_K])]

# ========= 推送與排版 =========
def push_to_targets(text: str):
    for tid in TARGET_IDS:
        line_bot_api.push_message(tid, TextSendMessage(text=text))

def _yahoo_link(code: str) -> str:
    sym = code if code.endswith(".TW") or code.endswith(".TWO") else f"{code}.TW"
    return f"https://tw.stock.yahoo.com/quote/{sym}"

def format_report(rows: List[str], title: str) -> str:
    header = "代號   漲幅(%)   成交量"
    sep    = "--------------------------"
    parsed = []
    for r in rows:
        try:
            parts = r.split()
            code, pct, vol = parts[1], parts[3].replace("%",""), parts[5]
            parsed.append((code, pct, vol))
        except Exception:
            continue

    code_w = max([4] + [len(p[0]) for p in parsed]) if parsed else 4
    pct_w  = max([6] + [len(p[1]) for p in parsed]) if parsed else 6
    vol_w  = max([6] + [len(p[2]) for p in parsed]) if parsed else 6

    lines = [f"", header, sep]
    for code, pct, vol in parsed:
        lines.append(f"{code:<{code_w}}  {pct:>{pct_w}}    {vol:>{vol_w}}")
    if not parsed:
        lines.append("（尚無符合條件）")
    else:
        links = [f"{c} ▶ {_yahoo_link(c)}" for c, _, _ in parsed[:3]]
        lines.append("")
        lines.append("🔗 快速查價")
        lines.extend(links)

    lines.append("\n— 由起漲掃描 · 自動戰報 —")
    return "\n".join(lines)

def split_rows_into_pages(rows: list[str], per_page: int) -> list[list[str]]:
    pages = []
    for i in range(0, len(rows), per_page):
        pages.append(rows[i:i+per_page])
    return pages

def push_chunked_formatted_report(all_rows: list[str], base_title: str, prefix_note: str = ""):
    pages = split_rows_into_pages(all_rows, MAX_LINES_PER_MSG)
    for idx, rows in enumerate(pages, start=1):
        title = f"{base_title}（{idx}/{len(pages)}）" if len(pages) > 1 else base_title
        msg = format_report(rows, title)
        if prefix_note:
            msg = f"{msg}\n{prefix_note}"

        if len(msg) <= MAX_CHARS_PER_MSG:
            push_to_targets(msg)
        else:
            head, *body = msg.split("\n")
            chunk = head
            for line in body:
                if len(chunk) + 1 + len(line) > MAX_CHARS_PER_MSG:
                    push_to_targets(chunk)
                    chunk = head
                chunk = f"{chunk}\n{line}"
            if chunk.strip():
                push_to_targets(chunk)

# —— 分群（上市 / 上櫃 / ETF）——
def classify_code(code: str) -> str:
    """分類：上市 / 上櫃 / ETF（優先以你的 ETF 清單判定）"""
    cc = code.upper()
    plain = cc.replace(".TW", "").replace(".TWO", "")
    if cc in ETF_SET or plain in ETF_SET:
        return "ETF"
    if cc.endswith(".TWO"):
        return "上櫃"
    if plain.isdigit() and len(plain) in (4, 5) and plain.startswith("0"):
        return "ETF"
    if any(x in plain for x in ["R", "L", "T", "U"]) and not plain.isdigit():
        return "ETF"
    return "上市"

def group_rows(rows: List[str]) -> dict[str, List[str]]:
    groups = {"上市": [], "上櫃": [], "ETF": []}
    for r in rows:
        try:
            parts = r.split()
            code = parts[1]
            cat = classify_code(code)
            groups[cat].append(r)
        except Exception:
            continue
    return {k: v for k, v in groups.items() if v}

def push_grouped_report(all_rows: List[str], base_title: str, prefix_note: str = ""):
    groups = group_rows(all_rows)
    em = {"上市": EMOJI_LISTED, "上櫃": EMOJI_OTC, "ETF": EMOJI_ETF}
    for cat in ["上市", "上櫃", "ETF"]:
        if cat not in groups: 
            continue
        title = f"{em.get(cat,'')} {base_title} · {cat}".strip()
        push_chunked_formatted_report(groups[cat], title, prefix_note=prefix_note)

# ========= 路由 =========
@app.get("/")
def root():
    return "Bot is running! 🚀", 200

@app.post("/callback")
def callback():
    signature = request.headers.get("X-Line-Signature", "")
    body = request.get_data(as_text=True)
    try:
        handler.handle(body, signature)
    except InvalidSignatureError:
        abort(400)
    return "OK"

@handler.add(MessageEvent, message=TextMessage)
def handle_message(event):
    line_bot_api.reply_message(event.reply_token, TextSendMessage(text=event.message.text))

@app.get("/test-push")
def test_push():
    if not TARGET_IDS:
        return "Missing LINE_USER_ID or LINE_TARGET_IDS", 500
    msg = request.args.get("msg", f"測試推播 OK：{tw_now():%Y-%m-%d %H:%M}")
    push_to_targets(msg)
    return f"Sent: {msg}", 200

def _resolve_watchlist() -> List[str]:
    if os.getenv("WATCHLIST","").strip().upper() == "ALL":
        return load_universe(MAX_SCAN)
    return [c.strip() for c in WATCHLIST if c.strip()]

@app.get("/daily-push")
def daily_push():
    if CRON_SECRET and request.args.get("key") != CRON_SECRET:
        return "Forbidden", 403
    wl = _resolve_watchlist()
    picked = pick_rising_stocks(wl)
    today = tw_now().strftime("%Y-%m-%d")
    push_grouped_report(picked, f"{today} 起漲清單")
    return "Daily push sent!", 200

@app.get("/onejob-push")
def onejob_push():
    if CRON_SECRET and request.args.get("key") != CRON_SECRET:
        return "Forbidden", 403
    wl = _resolve_watchlist()
    today = tw_now().date()
    tz = dt.timezone(dt.timedelta(hours=8))
    targets = [
        (dt.datetime.combine(today, dt.time(7,0), tzinfo=tz),  "07:00"),
        (dt.datetime.combine(today, dt.time(7,30), tzinfo=tz), "07:30"),
        (dt.datetime.combine(today, dt.time(8,0), tzinfo=tz),  "08:00"),
    ]
    pushed = []
    for target_dt, label in targets:
        if tw_now() < target_dt:
            wait_until(target_dt)
        picked = pick_rising_stocks(wl)
        date_txt = tw_now().strftime("%Y-%m-%d")
        push_grouped_report(picked, f"{date_txt} 起漲清單", prefix_note=f"⏰ 預設推送時間 {label}")
        pushed.append(label)
    return f"One job done. Pushed at {', '.join(pushed)}", 200

if __name__ == "__main__":
    port = int(os.getenv("PORT", "5000"))
    app.run(host="0.0.0.0", port=port)