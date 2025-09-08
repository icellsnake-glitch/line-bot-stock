import os
import time
import datetime as dt
from typing import List, Tuple

import requests
from flask import Flask, request, abort
from linebot import LineBotApi, WebhookHandler
from linebot.exceptions import InvalidSignatureError, LineBotApiError
from linebot.models import MessageEvent, TextMessage, TextSendMessage

app = Flask(__name__)

# ========= 環境變數 =========
CHANNEL_ACCESS_TOKEN = os.getenv("LINE_CHANNEL_ACCESS_TOKEN", "").replace("\n", "").strip()
CHANNEL_SECRET       = os.getenv("LINE_CHANNEL_SECRET", "").strip()
USER_ID              = os.getenv("LINE_USER_ID", "").strip()
CRON_SECRET          = os.getenv("CRON_SECRET", "").strip()

# 追蹤清單、漲幅門檻、成交量門檻，都從環境變數讀
WATCHLIST   = os.getenv("WATCHLIST", "2330,2454,2317,2603,2882").split(",")
MIN_CHANGE  = float(os.getenv("MIN_CHANGE_PCT", "2.0"))       # 預設 2%
MIN_VOLUME  = int(os.getenv("MIN_VOLUME", "1000000"))         # 預設 100 萬股
TOP_K       = int(os.getenv("TOP_K", "10"))

if not CHANNEL_ACCESS_TOKEN or not CHANNEL_SECRET:
    raise RuntimeError("缺少 LINE_CHANNEL_ACCESS_TOKEN 或 LINE_CHANNEL_SECRET")

line_bot_api = LineBotApi(CHANNEL_ACCESS_TOKEN)
handler = WebhookHandler(CHANNEL_SECRET)

# ========= 健康檢查 =========
@app.get("/")
def root():
    return "Bot is running! 🚀", 200

# ========= Webhook（回聲）=========
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

# ========= Yahoo Finance 工具 =========
def _yahoo_symbol(tw_code: str) -> str:
    tw_code = tw_code.strip().upper()
    if tw_code.endswith(".TW") or tw_code.endswith(".TWO"):
        return tw_code
    return f"{tw_code}.TW"

def fetch_change_pct_and_volume(tw_code: str) -> Tuple[float, int]:
    symbol = _yahoo_symbol(tw_code)
    urls = [
        f"https://query1.finance.yahoo.com/v8/finance/chart/{symbol}?range=1d&interval=1m",
        f"https://query1.finance.yahoo.com/v8/finance/chart/{symbol}?range=5d&interval=1d",
    ]
    last_close, last_price, last_volume = None, None, 0

    for url in urls:
        r = requests.get(url, timeout=12)
        r.raise_for_status()
        j = r.json()
        result = j.get("chart", {}).get("result", [])
        if not result:
            continue
        indicators = result[0]["indicators"]["quote"][0]
        closes = indicators.get("close") or []
        volumes = indicators.get("volume") or []

        for i in range(len(closes) - 1, -1, -1):
            if closes[i] is not None:
                last_price = closes[i]
                last_volume = int(volumes[i] or 0)
                break
        for i in range(len(closes) - 2, -1, -1):
            if closes[i] is not None:
                last_close = closes[i]
                break
        if last_price and last_close:
            break

    if not last_price or not last_close:
        return 0.0, 0
    return round((last_price - last_close) / last_close * 100.0, 2), last_volume

def pick_rising_stocks(codes: List[str]) -> List[str]:
    rows = []
    for code in codes:
        try:
            chg, vol = fetch_change_pct_and_volume(code)
            rows.append((code, chg, vol))
        except Exception:
            continue
    rows = [r for r in rows if r[1] >= MIN_CHANGE and r[2] >= MIN_VOLUME]
    rows.sort(key=lambda x: x[1], reverse=True)
    return [f"{i+1}. {c} 漲幅 {chg:.2f}% 量 {vol:,}" for i, (c, chg, vol) in enumerate(rows[:TOP_K])]

# ========= 工具 =========
def tw_now():
    return dt.datetime.now(dt.timezone(dt.timedelta(hours=8)))

def wait_until(target: dt.datetime):
    while True:
        if tw_now() >= target:
            return
        time.sleep(15)

# ========= 測試推播 =========
@app.get("/test-push")
def test_push():
    if not USER_ID:
        return "Missing LINE_USER_ID", 500
    msg = request.args.get("msg", f"測試推播 OK：{tw_now():%Y-%m-%d %H:%M}")
    line_bot_api.push_message(USER_ID, TextSendMessage(text=msg))
    return f"Sent: {msg}", 200

# ========= 立即推清單 =========
@app.get("/daily-push")
def daily_push():
    if CRON_SECRET and request.args.get("key") != CRON_SECRET:
        return "Forbidden", 403
    picked = pick_rising_stocks(WATCHLIST)
    today = tw_now().strftime("%Y-%m-%d")
    msg = f"【{today} 起漲清單】\n" + ("\n".join(picked) if picked else "尚無符合條件")
    line_bot_api.push_message(USER_ID, TextSendMessage(text=msg))
    return "Daily push sent!", 200

# ========= 一次排程 → 07:00 / 07:30 / 08:00 =========
@app.get("/onejob-push")
def onejob_push():
    if CRON_SECRET and request.args.get("key") != CRON_SECRET:
        return "Forbidden", 403
    today = tw_now().date()
    tz = dt.timezone(dt.timedelta(hours=8))
    targets = [
        (dt.datetime.combine(today, dt.time(7, 0), tzinfo=tz), "07:00"),
        (dt.datetime.combine(today, dt.time(7, 30), tzinfo=tz), "07:30"),
        (dt.datetime.combine(today, dt.time(8, 0), tzinfo=tz), "08:00"),
    ]
    pushed = []
    for target_dt, label in targets:
        if tw_now() < target_dt:
            wait_until(target_dt)
        picked = pick_rising_stocks(WATCHLIST)
        msg = f"【{tw_now():%Y-%m-%d} 起漲清單】\n" + ("\n".join(picked) if picked else "尚無符合條件")
        msg += f"\n⏰ 預設推送時間 {label}"
        line_bot_api.push_message(USER_ID, TextSendMessage(text=msg))
        pushed.append(label)
    return f"One job done. Pushed at {', '.join(pushed)}", 200

if __name__ == "__main__":
    port = int(os.getenv("PORT", "5000"))
    app.run(host="0.0.0.0", port=port)