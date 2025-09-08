import os
import re
import datetime as dt
import requests
from typing import List, Tuple
from flask import Flask, request, abort
from linebot import LineBotApi, WebhookHandler
from linebot.exceptions import InvalidSignatureError
from linebot.models import MessageEvent, TextMessage, TextSendMessage

# -------- Flask 基本設定 --------
app = Flask(__name__)

CHANNEL_ACCESS_TOKEN = os.getenv("LINE_CHANNEL_ACCESS_TOKEN")
CHANNEL_SECRET = os.getenv("LINE_CHANNEL_SECRET")
USER_ID = os.getenv("LINE_USER_ID")

if not CHANNEL_ACCESS_TOKEN or not CHANNEL_SECRET:
    raise RuntimeError("Missing LINE credentials!")

line_bot_api = LineBotApi(CHANNEL_ACCESS_TOKEN)
handler = WebhookHandler(CHANNEL_SECRET)

# -------- Yahoo Finance 工具 --------
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
        try:
            r = requests.get(url, timeout=10)
            r.raise_for_status()
            j = r.json()
        except Exception:
            continue

        result = j.get("chart", {}).get("result", [])
        if not result:
            continue

        quote = (result[0].get("indicators", {}).get("quote") or [{}])[0]
        closes, volumes = quote.get("close") or [], quote.get("volume") or []

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

# -------- 全市場清單工具 --------
def _parse_twse_codes(html: str) -> List[str]:
    html = html.replace("\u3000", " ")
    return re.findall(r'>\s*(\d{4})\s', html)

def load_watchlist() -> List[str]:
    raw = (os.getenv("WATCHLIST") or "").strip()
    if raw.upper() == "ALL":
        codes = []
        try:
            r = requests.get("https://isin.twse.com.tw/isin/C_public.jsp?strMode=2", timeout=15)
            r.encoding = "big5"
            codes += _parse_twse_codes(r.text)
        except Exception:
            pass
        try:
            r = requests.get("https://isin.twse.com.tw/isin/C_public.jsp?strMode=4", timeout=15)
            r.encoding = "big5"
            codes += [c + ".TWO" for c in _parse_twse_codes(r.text)]
        except Exception:
            pass
        uniq, seen = [], set()
        for c in codes:
            if c not in seen:
                seen.add(c)
                uniq.append(c)
        uniq.sort(key=lambda x: (".TWO" in x, x))
        return uniq

    if raw:
        return [t.strip() for t in raw.split(",") if t.strip()]
    return []

# -------- 過濾起漲股 --------
def pick_rising_stocks(watchlist: List[str],
                       min_change_pct: float = 2.0,
                       min_volume: int = 1_000_000,
                       top_k: int = 10) -> List[str]:
    rows = []
    for code in watchlist:
        try:
            chg, vol = fetch_change_pct_and_volume(code)
            rows.append((code, chg, vol))
        except Exception:
            continue
    rows = [r for r in rows if r[1] >= min_change_pct and r[2] >= min_volume]
    rows.sort(key=lambda x: x[1], reverse=True)
    return [f"{i+1}. {c}  漲幅 {chg:.2f}%  量 {vol:,}" for i, (c, chg, vol) in enumerate(rows[:top_k])]

# -------- API 路由 --------
@app.get("/")
def root():
    return "Bot is running!", 200

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

# 每日推播
@app.get("/daily-push")
def daily_push():
    try:
        watchlist = load_watchlist()
        picked = pick_rising_stocks(
            watchlist=watchlist,
            min_change_pct=float(os.getenv("MIN_CHANGE_PCT", 2.0)),
            min_volume=int(os.getenv("MIN_VOLUME", 1_000_000)),
            top_k=int(os.getenv("MAX_STOCKS", 10))
        )
        today = dt.datetime.now(tz=dt.timezone(dt.timedelta(hours=8))).strftime("%Y-%m-%d")
        msg = f"【{today} 起漲清單】\n" + ("\n".join(picked) if picked else "尚無符合條件個股")
        if USER_ID:
            line_bot_api.push_message(USER_ID, TextSendMessage(text=msg))
        return "Push sent!", 200
    except Exception as e:
        app.logger.exception(e)
        return str(e), 500

if __name__ == "__main__":
    port = int(os.getenv("PORT", 5000))
    app.run(host="0.0.0.0", port=port)