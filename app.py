import os
import re
import json
import time
import threading
import datetime as dt
from typing import List, Tuple, Iterable, Optional

import requests
import pytz
import schedule
from flask import Flask, request, abort, jsonify

from linebot import LineBotApi, WebhookHandler
from linebot.exceptions import InvalidSignatureError, LineBotApiError
from linebot.models import MessageEvent, TextMessage, TextSendMessage

# ------------------ 基本設定 ------------------
app = Flask(__name__)
TZ = pytz.timezone("Asia/Taipei")

# LINE 環境變數
CHANNEL_ACCESS_TOKEN = os.getenv("LINE_CHANNEL_ACCESS_TOKEN", "").strip()
CHANNEL_SECRET = os.getenv("LINE_CHANNEL_SECRET", "").strip()
USER_ID = os.getenv("LINE_USER_ID", "").strip()   # 個人 User ID（測試推播用）

if not CHANNEL_ACCESS_TOKEN or not CHANNEL_SECRET:
    raise RuntimeError("Missing env: LINE_CHANNEL_ACCESS_TOKEN or LINE_CHANNEL_SECRET")

line_bot_api = LineBotApi(CHANNEL_ACCESS_TOKEN)
handler = WebhookHandler(CHANNEL_SECRET)

# 掃描門檻（沒有就用預設）
def _get_float(name: str, default: float) -> float:
    val = os.getenv(name, "").strip()
    return float(val) if val != "" else default

def _get_int(name: str, default: int) -> int:
    val = os.getenv(name, "").strip()
    return int(val) if val != "" else default

MIN_CHANGE_PCT  = _get_float("MIN_CHANGE_PCT", 0.5)       # 當日漲幅門檻（%）
MIN_VOLUME      = _get_int("MIN_VOLUME", 100)             # 當日量門檻（股）
MAX_LINES_PER_MSG = _get_int("MAX_LINES_PER_MSG", 25)     # 每則訊息最多幾行
MAX_CHARS_PER_MSG = _get_int("MAX_CHARS_PER_MSG", 1900)   # 每則訊息字數上限

WATCHLIST_ENV   = os.getenv("WATCHLIST", "2330,2317,2454,2603,2882,2303,2412").strip()
LIST_SOURCES    = os.getenv("LIST_SOURCES", "").strip()   # 逗號分隔 CSV/JSON URL（第一欄為代號）

# ------------------ 工具：代號正規化 ------------------
def normalize_code(token: str) -> Optional[str]:
    """回傳合法台股代號：上市/上櫃四碼 + 可選 .TW/.TWO；ETF 也保留四碼。"""
    t = token.strip().upper()
    if not t:
        return None
    if t.endswith(".TW") or t.endswith(".TWO"):
        core = t.split(".")[0]
        return core if re.fullmatch(r"\d{4}", core) else None
    # 僅數字四碼
    return t if re.fullmatch(r"\d{4}", t) else None

def to_yahoo_symbol(code: str) -> str:
    """將四碼代號轉 Yahoo 代號，先假設上市 .TW；若想自訂上櫃代號可在來源就附 .TWO"""
    if code.endswith(".TW") or code.endswith(".TWO"):
        return code
    return f"{code}.TW"

# ------------------ 取得 Watchlist ------------------
def load_watchlist() -> List[str]:
    """
    - WATCHLIST=ALL -> 從 LIST_SOURCES 讀；若沒設來源，就回傳常見權值股以免空集合
    - 否則 WATCHLIST 可逗號分隔：2330,2317 或 2330.TWO
    """
    if WATCHLIST_ENV.upper() == "ALL":
        urls = [u.strip() for u in LIST_SOURCES.split(",") if u.strip()]
        codes: List[str] = []
        for url in urls:
            try:
                r = requests.get(url, timeout=10)
                r.raise_for_status()
                # 粗略同時支援 JSON/CSV：第一欄或 key 名含「code/代號」
                ctype = (r.headers.get("Content-Type") or "").lower()
                if "json" in ctype:
                    arr = r.json()
                    for row in arr:
                        # 常見欄位名
                        for k in ("code", "Code", "證券代號", "stock_id", "ticker"):
                            if k in row:
                                c = normalize_code(str(row[k]))
                                if c:
                                    codes.append(c)
                                break
                else:
                    # 當作 CSV
                    for line in r.text.splitlines():
                        first = line.split(",")[0]
                        c = normalize_code(first)
                        if c:
                            codes.append(c)
            except Exception:
                continue
        # 去重 + 保序
        uniq: List[str] = []
        seen = set()
        for c in codes:
            if c not in seen:
                uniq.append(c)
                seen.add(c)
        if uniq:
            return uniq

        # 沒有來源/抓不到 → 給一份安全的預設（不讓清單為空）
        return ["2330", "2317", "2454", "2303", "2412", "2882", "1303", "1101"]

    # 非 ALL：逗號清單
    out: List[str] = []
    for tok in WATCHLIST_ENV.split(","):
        c = normalize_code(tok)
        if c:
            out.append(c)
    return out or ["2330"]

# ------------------ Yahoo 取價/量 ------------------
def fetch_change_pct_and_volume(tw_code: str) -> Tuple[float, int]:
    """
    回傳：(當日漲跌幅%, 當日成交量)
    先用 1d/1m；拿不到退 5d/1d。拿不到就 (0,0)。
    """
    symbol = to_yahoo_symbol(tw_code)
    urls = [
        f"https://query1.finance.yahoo.com/v8/finance/chart/{symbol}?range=1d&interval=1m",
        f"https://query1.finance.yahoo.com/v8/finance/chart/{symbol}?range=5d&interval=1d",
    ]
    last_close = None
    last_price = None
    last_volume = 0

    for url in urls:
        r = requests.get(url, timeout=10)
        r.raise_for_status()
        j = r.json()
        result = j.get("chart", {}).get("result", [])
        if not result:
            continue
        indicators = result[0].get("indicators", {})
        quote = (indicators.get("quote") or [{}])[0]
        closes = quote.get("close") or []
        volumes = quote.get("volume") or []

        # 最後一筆有效價
        for i in range(len(closes) - 1, -1, -1):
            c = closes[i]
            v = volumes[i] if i < len(volumes) else 0
            if c is not None:
                last_price = c
                last_volume = int(v or 0)
                break
        # 昨收（上一筆有效價）
        for i in range(len(closes) - 2, -1, -1):
            c = closes[i]
            if c is not None:
                last_close = c
                break

        if last_price is not None and last_close is not None and last_close != 0:
            break

    if last_price is None or last_close is None or last_close == 0:
        return 0.0, 0

    change_pct = (last_price - last_close) / last_close * 100.0
    return round(change_pct, 2), last_volume

# ------------------ 起漲篩選 ------------------
def pick_rising_stocks(codes: Iterable[str]) -> List[str]:
    rows = []
    for code in codes:
        try:
            chg, vol = fetch_change_pct_and_volume(code)
            if chg >= MIN_CHANGE_PCT and vol >= MIN_VOLUME:
                rows.append((code, chg, vol))
        except Exception:
            continue
    rows.sort(key=lambda x: x[1], reverse=True)
    pretty = [f"{i+1}. {code}  漲幅 {chg:.2f}%  量 {vol:,}"
              for i, (code, chg, vol) in enumerate(rows)]
    return pretty

def split_messages(lines: List[str], title: str) -> List[str]:
    """依行數與字數切段"""
    msgs: List[str] = []
    buf: List[str] = [title]
    size = len(title)
    cnt = 0
    for line in lines:
        if cnt >= MAX_LINES_PER_MSG or size + 1 + len(line) > MAX_CHARS_PER_MSG:
            msgs.append("\n".join(buf))
            buf = [title]
            size = len(title)
            cnt = 0
        buf.append(line)
        size += 1 + len(line)
        cnt += 1
    msgs.append("\n".join(buf))
    return msgs

# ------------------ 推播主流程 ------------------
def do_scan_and_push() -> str:
    codes = load_watchlist()
    picked = pick_rising_stocks(codes)
    now = dt.datetime.now(TZ).strftime("%Y-%m-%d %H:%M")
    title = f"【{now} 起漲清單】(漲幅 ≥{MIN_CHANGE_PCT}%, 量 ≥{MIN_VOLUME})"

    if not picked:
        msg = f"{title}\n尚無符合條件的個股（或資料未更新）"
        if USER_ID:
            line_bot_api.push_message(USER_ID, TextSendMessage(text=msg))
        return "no-picked"

    chunks = split_messages(picked, title)
    if USER_ID:
        for c in chunks:
            line_bot_api.push_message(USER_ID, TextSendMessage(text=c))
    return "ok"

# ------------------ 盤中每5分鐘排程 ------------------
def is_trading_now() -> bool:
    now = dt.datetime.now(TZ)
    if now.weekday() > 4:
        return False
    hhmm = now.hour * 100 + now.minute
    return 910 <= hhmm <= 1330

def job_every_5min():
    if is_trading_now():
        try:
            do_scan_and_push()
        except Exception as e:
            app.logger.exception(e)

def scheduler_thread():
    schedule.every(5).minutes.do(job_every_5min)
    while True:
        schedule.run_pending()
        time.sleep(1)

threading.Thread(target=scheduler_thread, daemon=True).start()

# ------------------ Flask 路由 ------------------
@app.get("/")
def root():
    return "Bot is running! 🚀", 200

@app.get("/ping")
def ping():
    return "pong", 200

@app.get("/daily-push")
def daily_push_route():
    try:
        if not USER_ID:
            return "Missing env: LINE_USER_ID", 400
        do_scan_and_push()
        return "Push sent!", 200
    except Exception as e:
        app.logger.exception(e)
        return str(e), 500

@app.get("/debug/watchlist")
def debug_watchlist():
    codes = load_watchlist()
    return jsonify({"count": len(codes), "sample": codes[:20]})

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
    text = (event.message.text or "").strip()
    if text == "測試推播":
        status = do_scan_and_push()
        reply = f"測試推播 OK（{status}）"
    else:
        reply = text
    line_bot_api.reply_message(event.reply_token, TextSendMessage(text=reply))

if __name__ == "__main__":
    port = int(os.getenv("PORT", "5000"))
    app.run(host="0.0.0.0", port=port)