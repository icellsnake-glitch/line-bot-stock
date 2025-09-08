# app.py
import os
import datetime as dt
from typing import List, Tuple

import requests
from flask import Flask, request, abort
from linebot import LineBotApi, WebhookHandler
from linebot.exceptions import InvalidSignatureError, LineBotApiError
from linebot.models import MessageEvent, TextMessage, TextSendMessage

app = Flask(__name__)

# ====== 讀取環境變數 ======
CHANNEL_ACCESS_TOKEN = os.getenv("LINE_CHANNEL_ACCESS_TOKEN")
CHANNEL_SECRET = os.getenv("LINE_CHANNEL_SECRET")
USER_ID = os.getenv("LINE_USER_ID")  # 你的「Your user ID」

if not CHANNEL_ACCESS_TOKEN or not CHANNEL_SECRET:
    raise RuntimeError("缺少環境變數：LINE_CHANNEL_ACCESS_TOKEN 或 LINE_CHANNEL_SECRET")
# USER_ID 允許先空，因為 /test-push /daily-push 會檢查並回報

line_bot_api = LineBotApi(CHANNEL_ACCESS_TOKEN)
handler = WebhookHandler(CHANNEL_SECRET)

# ====== 基本健康檢查 ======
@app.get("/")
def root():
    return "Bot is running! 🚀", 200

# ====== LINE Webhook（回你傳來的文字）======
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

# ====== 工具：Yahoo Finance 抓當日變化 ======
def _yahoo_symbol(tw_code: str) -> str:
    tw_code = tw_code.strip().upper()
    if tw_code.endswith(".TW") or tw_code.endswith(".TWO"):
        return tw_code
    return f"{tw_code}.TW"  # 預設當上市

def fetch_change_pct_and_volume(tw_code: str) -> Tuple[float, int]:
    symbol = _yahoo_symbol(tw_code)
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

        # 最後一筆有效收盤/量
        for i in range(len(closes) - 1, -1, -1):
            c = closes[i]
            v = volumes[i] if i < len(volumes) else 0
            if c is not None:
                last_price = c
                last_volume = int(v or 0)
                break
        # 前一筆當作昨收
        for i in range(len(closes) - 2, -1, -1):
            c = closes[i]
            if c is not None:
                last_close = c
                break
        if last_price is not None and last_close is not None:
            break

    if last_price is None or last_close is None or last_close == 0:
        return 0.0, 0

    change_pct = (last_price - last_close) / last_close * 100.0
    return round(change_pct, 2), last_volume

def pick_rising_stocks(
    watchlist: List[str],
    min_change_pct: float = 2.0,
    min_volume: int = 1_000_000,
    top_k: int = 10,
) -> List[str]:
    rows = []
    for code in watchlist:
        try:
            chg, vol = fetch_change_pct_and_volume(code)
            rows.append((code, chg, vol))
        except Exception:
            continue
    rows = [r for r in rows if r[1] >= min_change_pct and r[2] >= min_volume]
    rows.sort(key=lambda x: x[1], reverse=True)

    pretty = [f"{i+1}. {code}  漲幅 {chg:.2f}%  量 {vol:,}"
              for i, (code, chg, vol) in enumerate(rows[:top_k])]
    return pretty

# ====== 測試推播 ======
@app.get("/test-push")
def test_push():
    if not USER_ID:
        return "Missing env: LINE_USER_ID", 500
    msg = request.args.get("msg", "Hello from Bot!")
    try:
        line_bot_api.push_message(USER_ID, TextSendMessage(text=msg))
        return f"已推送訊息: {msg}", 200
    except LineBotApiError as e:
        app.logger.exception(e)
        return f"LINE push 失敗：{e}", 500

# ====== 每日起漲清單（手動觸發端點）======
@app.get("/daily-push")
def daily_push():
    if not USER_ID:
        return "Missing env: LINE_USER_ID", 500

    # 先放一份示範追蹤清單（可自行調整/加上 .TWO）
    watchlist = [
        "2330", "2454", "2317", "2303", "2603", "2882", "2412",
        "1303", "1101", "5871", "1605", "2377", "3481", "3661",
    ]
    try:
        picked = pick_rising_stocks(
            watchlist=watchlist,
            min_change_pct=2.0,
            min_volume=1_000_000,
            top_k=10
        )
        today = dt.datetime.now(tz=dt.timezone(dt.timedelta(hours=8))).strftime("%Y-%m-%d")
        if picked:
            message = f"【{today} 起漲清單】\n" + "\n".join(picked)
        else:
            message = f"【{today} 起漲清單】\n尚無符合條件的個股（或資料未更新）"

        line_bot_api.push_message(USER_ID, TextSendMessage(text=message))
        return "Daily push sent!", 200
    except Exception as e:
        app.logger.exception(e)
        return str(e), 500

if __name__ == "__main__":
    port = int(os.getenv("PORT", "5000"))
    app.run(host="0.0.0.0", port=port)