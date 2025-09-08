# app.py
import os
import re
import io
import csv
import hashlib
import requests
from datetime import datetime, time, timedelta, timezone
from typing import List, Tuple

from flask import Flask, request, abort
from linebot import LineBotApi, WebhookHandler
from linebot.exceptions import InvalidSignatureError, LineBotApiError
from linebot.models import MessageEvent, TextMessage, TextSendMessage

# ================== Flask / LINE 基本設定 ==================
app = Flask(__name__)

CHANNEL_ACCESS_TOKEN = os.getenv("LINE_CHANNEL_ACCESS_TOKEN", "").strip()
CHANNEL_SECRET       = os.getenv("LINE_CHANNEL_SECRET", "").strip()
USER_ID              = os.getenv("LINE_USER_ID", "").strip()

if not CHANNEL_ACCESS_TOKEN or not CHANNEL_SECRET:
    raise RuntimeError("Missing env: LINE_CHANNEL_ACCESS_TOKEN or LINE_CHANNEL_SECRET")

line_bot_api = LineBotApi(CHANNEL_ACCESS_TOKEN)
handler      = WebhookHandler(CHANNEL_SECRET)

# ================== 參數與預設值（可用環境變數覆蓋） ==================
WATCHLIST         = os.getenv("WATCHLIST", "ALL").strip()  # 'ALL' 或 '2330,2317,...'
CRON_SECRET       = os.getenv("CRON_SECRET", "").strip()

MIN_CHANGE_PCT    = float(os.getenv("MIN_CHANGE_PCT", "2.0"))
MIN_VOLUME        = int(float(os.getenv("MIN_VOLUME", "1000000")))
TOP_K             = int(os.getenv("TOP_K", "50"))

MAX_LINES_PER_MSG = int(os.getenv("MAX_LINES_PER_MSG", "18"))
MAX_CHARS_PER_MSG = int(os.getenv("MAX_CHARS_PER_MSG", "4500"))

# 盤中交易時段控制（台北時間）
MARKET_OPEN_STR   = os.getenv("MARKET_OPEN", "09:00")
MARKET_CLOSE_STR  = os.getenv("MARKET_CLOSE", "13:30")
TZ_NAME           = os.getenv("TZ", "Asia/Taipei")  # 簡化：一律用 UTC+8
TZ8               = timezone(timedelta(hours=8))
HOLIDAYS_RAW      = os.getenv("HOLIDAYS", "").strip()
HOLIDAYS          = {h.strip() for h in HOLIDAYS_RAW.split(",") if h.strip()}

# 收盤後「隔日觀察」門檻
MIN_CHANGE_PCT_EOD = float(os.getenv("MIN_CHANGE_PCT_EOD", "1.5"))
MIN_VOLUME_EOD     = int(float(os.getenv("MIN_VOLUME_EOD", "500000")))
EOD_TIME_STR       = os.getenv("EOD_TIME", "14:10")  # 台北時間

# Emoji（可換）
EMOJI_LISTED = os.getenv("EMOJI_LISTED", "📊")
EMOJI_OTC    = os.getenv("EMOJI_OTC", "📈")
EMOJI_ETF    = os.getenv("EMOJI_ETF", "📦")

# 去重狀態（盤中 / 收盤分開記錄）
LAST_HASH     = {"date": None, "digest": None}
LAST_HASH_EOD = {"date": None, "digest": None}

# ================== 時間/工具 ==================
def _tw_now():
    return datetime.now(TZ8)

def _parse_hhmm(s: str) -> time:
    hh, mm = s.split(":")
    return time(int(hh), int(mm))

def is_trading_window() -> bool:
    """僅在工作日、非假日、交易時間內回 True。"""
    now = _tw_now()
    dstr = now.strftime("%Y-%m-%d")
    if dstr in HOLIDAYS:
        return False
    if now.weekday() > 4:  # 0~4 = Mon~Fri
        return False
    start = _parse_hhmm(MARKET_OPEN_STR)
    end   = _parse_hhmm(MARKET_CLOSE_STR)
    return start <= now.time() <= end

def _calc_digest(lines: List[str]) -> str:
    m = hashlib.md5()
    for s in lines:
        m.update(s.encode("utf-8"))
        m.update(b"\n")
    return m.hexdigest()

def should_push(lines: List[str]) -> bool:
    """盤中去重：同日相同內容不再推。"""
    today  = _tw_now().strftime("%Y-%m-%d")
    digest = _calc_digest(lines)
    if LAST_HASH["date"] == today and LAST_HASH["digest"] == digest:
        return False
    LAST_HASH["date"]   = today
    LAST_HASH["digest"] = digest
    return True

def should_push_eod(lines: List[str]) -> bool:
    """收盤版去重：同日相同內容不再推。"""
    today  = _tw_now().strftime("%Y-%m-%d")
    digest = _calc_digest(lines)
    if LAST_HASH_EOD["date"] == today and LAST_HASH_EOD["digest"] == digest:
        return False
    LAST_HASH_EOD["date"]   = today
    LAST_HASH_EOD["digest"] = digest
    return True

def chunk_messages(lines: List[str]) -> List[str]:
    """依行數/字數限制自動分頁。"""
    msgs, buf = [], []
    for line in lines:
        candidate = ("\n".join(buf + [line])) if buf else line
        if (len(buf) >= MAX_LINES_PER_MSG) or (len(candidate) > MAX_CHARS_PER_MSG):
            msgs.append("\n".join(buf))
            buf = [line]
        else:
            buf.append(line)
    if buf:
        msgs.append("\n".join(buf))
    return msgs

# ================== 市場/代號處理 ==================
def classify_market(code: str) -> str:
    """粗分：'上市' / '上櫃' / 'ETF'"""
    cc = code.upper()
    plain = cc.replace(".TW", "").replace(".TWO", "")
    if cc.endswith(".TWO"):
        return "上櫃"
    # 簡式 ETF 規則：數字且 4~5 碼、且以 0 開頭（0050/00878...）
    if plain.isdigit() and len(plain) in (4, 5) and plain.startswith("0"):
        return "ETF"
    return "上市"

def label_with_market(code: str) -> Tuple[str, str]:
    """(純代號, 市場)"""
    if code.endswith(".TWO"):
        return (code[:-4], "上櫃")
    if code.endswith(".TW"):
        return (code[:-3], "上市")
    return (code, classify_market(code))

# ================== 抓價量（Yahoo Finance） ==================
def _yahoo_symbol(tw_code: str) -> str:
    tw_code = tw_code.strip().upper()
    if tw_code.endswith(".TW") or tw_code.endswith(".TWO"):
        return tw_code
    return f"{tw_code}.TW"

def fetch_change_pct_and_volume(tw_code: str) -> Tuple[float, int]:
    """
    回傳：(當日漲跌幅%, 當日成交量)
    先試 1d/1m，失敗退 5d/1d 的最後一根。
    """
    symbol = _yahoo_symbol(tw_code)
    urls = [
        f"https://query1.finance.yahoo.com/v8/finance/chart/{symbol}?range=1d&interval=1m",
        f"https://query1.finance.yahoo.com/v8/finance/chart/{symbol}?range=5d&interval=1d",
    ]
    last_close = None
    last_price = None
    last_volume = 0

    for url in urls:
        r = requests.get(url, timeout=12)
        r.raise_for_status()
        j = r.json()
        result = j.get("chart", {}).get("result", [])
        if not result:
            continue
        indicators = result[0].get("indicators", {})
        quote = (indicators.get("quote") or [{}])[0]
        closes  = quote.get("close") or []
        volumes = quote.get("volume") or []

        # 取最後有效值
        for i in range(len(closes) - 1, -1, -1):
            c = closes[i]
            v = volumes[i] if i < len(volumes) else 0
            if c is not None:
                last_price  = c
                last_volume = int(v or 0)
                break

        # 取昨收
        for i in range(len(closes) - 2, -1, -1):
            c = closes[i]
            if c is not None:
                last_close = c
                break

        if last_price is not None and last_close is not None:
            break

    if not last_price or not last_close:
        return 0.0, 0

    chg = (last_price - last_close) / last_close * 100.0
    return round(chg, 2), int(last_volume)

# ================== 全市場清單（WATCHLIST=ALL） ==================
def load_watchlist() -> List[str]:
    """
    WATCHLIST:
      - 'ALL'  → 自動抓全市場（上市 + 上櫃）
      - '2330,2317,2454' → 逗號清單
      - 代號可混 .TWO
    """
    wl_env = WATCHLIST
    if not wl_env:
        return []

    if wl_env.upper() != "ALL":
        return [c.strip() for c in wl_env.split(",") if c.strip()]

    codes: List[str] = []

    # 上市：TWSE ISIN 公開頁（HTML/TSV 混合，抓第一欄、前綴數字）
    try:
        r1 = requests.get("https://isin.twse.com.tw/isin/C_public.jsp?strMode=2", timeout=20)
        r1.encoding = "utf-8"  # 官方近年大多回 UTF-8；若遇到 Big5 也能自動解
        for line in r1.text.splitlines():
            # 以「\t」切，第一欄常見「2330　台積電」
            cells = [c.strip() for c in line.split("\t") if c.strip()]
            if not cells:
                continue
            head = cells[0]
            # 代號在最前面，之後是全形空白 + 名稱
            code = head.split(" ")[0].split("　")[0].strip()
            if code.isdigit():
                codes.append(code)  # 上市預設 .TW
    except Exception as e:
        app.logger.warning(f"抓上市清單失敗：{e}")

    # 上櫃：TPEx JSON，代號需加 .TWO
    try:
        r2 = requests.get(
            "https://www.tpex.org.tw/web/stock/aftertrading/otc_quotes_no1430/stk_wn1430.php?l=zh-tw",
            timeout=20
        )
        j = r2.json()
        for row in j.get("aaData", []):
            code = str(row[0]).strip()
            if code.isdigit():
                codes.append(code + ".TWO")
    except Exception as e:
        app.logger.warning(f"抓上櫃清單失敗：{e}")

    # 去重
    seen, uniq = set(), []
    for c in codes:
        if c not in seen:
            uniq.append(c)
            seen.add(c)
    return uniq

# ================== 起漲挑選 ==================
def pick_rising_stocks(watchlist: List[str],
                       min_change_pct: float,
                       min_volume: int,
                       top_k: int) -> List[Tuple[str, float, int]]:
    rows = []
    for code in watchlist:
        try:
            chg, vol = fetch_change_pct_and_volume(code)
            if chg >= min_change_pct and vol >= min_volume:
                rows.append((code, chg, vol))
        except Exception:
            continue
    rows.sort(key=lambda x: x[1], reverse=True)
    return rows[:top_k]

# ================== 盤中：單次掃描（分群 + 分頁） ==================
def run_intraday_once() -> Tuple[List[str], str]:
    watchlist = load_watchlist()
    if not watchlist:
        return [], "Watchlist 為空（請設定 WATCHLIST）"

    # 先掃描全部 → 蒐集 (code, chg, vol)
    scanned = []
    for code in watchlist:
        try:
            chg, vol = fetch_change_pct_and_volume(code)
            scanned.append((code, chg, vol))
        except Exception:
            continue

    # 分群 + 過濾
    buckets = {"上市": [], "上櫃": [], "ETF": []}
    for code, chg, vol in scanned:
        group = classify_market(code)
        if chg >= MIN_CHANGE_PCT and vol >= MIN_VOLUME:
            buckets[group].append((code, chg, vol))

    # 排序 + 取前 TOP_K
    for k in buckets:
        buckets[k].sort(key=lambda x: x[1], reverse=True)
        buckets[k] = buckets[k][:TOP_K]

    today = _tw_now().strftime("%Y-%m-%d")
    segments_all: List[str] = []
    any_hit = any(buckets[k] for k in buckets)
    if not any_hit:
        header = f"【{today} 起漲清單】目前無符合條件（或資料未更新）\n門檻：漲幅≥{MIN_CHANGE_PCT}%，量≥{MIN_VOLUME:,}"
        return [header], "Empty picks"

    def fmt_rows(rows):
        out = []
        for i, (code, chg, vol) in enumerate(rows, 1):
            name, tag = label_with_market(code)
            out.append(f"{i:>2}. {name:<6} ({tag})  漲幅 {chg:>6.2f}%  量 {vol:,}")
        return out

    for cat, icon in (("上市", EMOJI_LISTED), ("上櫃", EMOJI_OTC), ("ETF", EMOJI_ETF)):
        rows = buckets[cat]
        if not rows:
            continue
        title = (
            f"【{today} 起漲清單】{icon} {cat}（{len(rows)} 檔）\n"
            f"門檻：漲幅≥{MIN_CHANGE_PCT}%，量≥{MIN_VOLUME:,}"
        )
        pages = chunk_messages(fmt_rows(rows))
        if pages:
            pages[0] = title + "\n" + pages[0]
        segments_all.extend(pages)

    info = f"Listed:{len(buckets['上市'])}, OTC:{len(buckets['上櫃'])}, ETF:{len(buckets['ETF'])}"
    return segments_all, info

# ================== 收盤：單次掃描（隔日觀察） ==================
def run_eod_once() -> Tuple[List[str], str]:
    watchlist = load_watchlist()
    if not watchlist:
        return [], "Watchlist 為空（請設定 WATCHLIST）"

    scanned = []
    for code in watchlist:
        try:
            chg, vol = fetch_change_pct_and_volume(code)
            scanned.append((code, chg, vol))
        except Exception:
            continue

    buckets = {"上市": [], "上櫃": [], "ETF": []}
    for code, chg, vol in scanned:
        group = classify_market(code)
        if chg >= MIN_CHANGE_PCT_EOD and vol >= MIN_VOLUME_EOD:
            buckets[group].append((code, chg, vol))

    for k in buckets:
        buckets[k].sort(key=lambda x: x[1], reverse=True)
        buckets[k] = buckets[k][:TOP_K]

    today = _tw_now().strftime("%Y-%m-%d")
    segments_all: List[str] = []
    any_hit = any(buckets[k] for k in buckets)
    if not any_hit:
        header = (
            f"【{today} 隔日觀察清單】目前無符合條件（或資料未更新）\n"
            f"門檻：漲幅≥{MIN_CHANGE_PCT_EOD}%，量≥{MIN_VOLUME_EOD:,}"
        )
        return [header], "Empty picks"

    def fmt_rows(rows):
        out = []
        for i, (code, chg, vol) in enumerate(rows, 1):
            name, tag = label_with_market(code)
            out.append(f"{i:>2}. {name:<6} ({tag})  漲幅 {chg:>6.2f}%  量 {vol:,}")
        return out

    for cat, icon in (("上市", EMOJI_LISTED), ("上櫃", EMOJI_OTC), ("ETF", EMOJI_ETF)):
        rows = buckets[cat]
        if not rows:
            continue
        title = (
            f"【{today} 隔日觀察清單】{icon} {cat}（{len(rows)} 檔）\n"
            f"門檻：漲幅≥{MIN_CHANGE_PCT_EOD}%，量≥{MIN_VOLUME_EOD:,}"
        )
        pages = chunk_messages(fmt_rows(rows))
        if pages:
            pages[0] = title + "\n" + pages[0]
        segments_all.extend(pages)

    info = f"EOD Listed:{len(buckets['上市'])}, OTC:{len(buckets['上櫃'])}, ETF:{len(buckets['ETF'])}"
    return segments_all, info

# ================== 路由：健康檢查 / Webhook / 測試 ==================
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
    # Echo
    line_bot_api.reply_message(event.reply_token, TextSendMessage(text=event.message.text))

@app.get("/test-push")
def test_push():
    msg = request.args.get("msg", "Test")
    try:
        if not USER_ID:
            return "Missing env: LINE_USER_ID", 500
        line_bot_api.push_message(USER_ID, TextSendMessage(text=f"測試推播 OK：{msg}"))
        return "OK", 200
    except LineBotApiError as e:
        app.logger.exception(e)
        return str(e), 500

# ================== 路由：手動即時掃描 ==================
@app.get("/daily-push")
def daily_push():
    try:
        segments, info = run_intraday_once()
        if not segments:
            return f"Skip ({info})", 204
        # 標註時間
        stamp = _tw_now().strftime("%H:%M")
        segments[0] = segments[0] + f"\n⏱ 更新時間 {stamp}"
        for seg in segments:
            line_bot_api.push_message(USER_ID, TextSendMessage(text=seg))
        return f"OK ({info})", 200
    except Exception as e:
        app.logger.exception(e)
        return str(e), 500

# ================== 路由：每 30 分鐘（交易時段才推 + 去重 + 金鑰） ==================
@app.get("/cron-scan-30m")
def cron_scan_30m():
    if CRON_SECRET and request.args.get("key") != CRON_SECRET:
        return "Unauthorized", 401
    if not is_trading_window():
        return "Skip (off trading window)", 204
    segments, info = run_intraday_once()
    if not segments:
        return f"Skip ({info})", 204
    if not should_push(segments):
        return "Skip (duplicate content)", 204
    stamp = _tw_now().strftime("%H:%M")
    segments[0] = segments[0] + f"\n⏱ 更新時間 {stamp}"
    for seg in segments:
        line_bot_api.push_message(USER_ID, TextSendMessage(text=seg))
    return f"OK ({info})", 200

# ================== 路由：收盤後隔日觀察（固定時點 + 去重 + 金鑰） ==================
def _parse_eod_time(s: str) -> time:
    hh, mm = s.split(":")
    return time(int(hh), int(mm))

@app.get("/cron-eod")
def cron_eod():
    if CRON_SECRET and request.args.get("key") != CRON_SECRET:
        return "Unauthorized", 401

    now = _tw_now()
    dstr = now.strftime("%Y-%m-%d")
    if dstr in HOLIDAYS or now.weekday() > 4:
        return "Skip (holiday or weekend)", 204

    target_t = _parse_eod_time(EOD_TIME_STR)
    if now.time() < target_t:
        return f"Skip (not yet {EOD_TIME_STR} TST)", 204

    segments, info = run_eod_once()
    if not segments:
        return f"Skip ({info})", 204
    if not should_push_eod(segments):
        return "Skip (duplicate EOD content)", 204

    stamp = now.strftime("%H:%M")
    segments[0] = segments[0] + f"\n⏱ 產生時間 {stamp}"
    for seg in segments:
        line_bot_api.push_message(USER_ID, TextSendMessage(text=seg))
    return f"OK ({info})", 200

# ================== 本地開發用（Render 會用 gunicorn 啟動） ==================
if __name__ == "__main__":
    port = int(os.getenv("PORT", "5000"))
    app.run(host="0.0.0.0", port=port)