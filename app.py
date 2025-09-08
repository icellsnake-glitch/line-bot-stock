import os
import re
import datetime as dt
from typing import List, Tuple
import requests

from flask import Flask, request, abort
from linebot import LineBotApi, WebhookHandler
from linebot.exceptions import InvalidSignatureError
from linebot.models import MessageEvent, TextMessage, TextSendMessage

# -------------------------
# 環境變數（必要/可選）
# -------------------------
def _get_str(key: str, default: str = "") -> str:
    v = os.getenv(key, "")
    return v if v is not None else default

def _get_float(key: str, default: float) -> float:
    v = os.getenv(key, "")
    return float(v.strip()) if v and v.strip() != "" else default

def _get_int(key: str, default: int) -> int:
    v = os.getenv(key, "")
    return int(v.strip()) if v and v.strip() != "" else default

LINE_CHANNEL_ACCESS_TOKEN = _get_str("LINE_CHANNEL_ACCESS_TOKEN")
LINE_CHANNEL_SECRET       = _get_str("LINE_CHANNEL_SECRET")
LINE_USER_ID              = _get_str("LINE_USER_ID")  # 你的個人 userId（測試推播用）

# 門檻與分段
MIN_CHANGE_PCT   = _get_float("MIN_CHANGE_PCT",   0.5)      # 例如 0.5 (%)
MIN_VOLUME       = _get_int  ("MIN_VOLUME",       100)      # 例如 100（股）
MAX_LINES_PER_MSG= _get_int  ("MAX_LINES_PER_MSG", 25)
MAX_CHARS_PER_MSG= _get_int  ("MAX_CHARS_PER_MSG", 1900)

# 觀察名單（逗號分隔）。若給 "ALL" 就用內建全市場（示例少量；你可接上自己的 CSV）
WATCHLIST_RAW = _get_str("WATCHLIST", "2330,2317,2454,2303,2412,2882,1303,1101")

# 偵錯模式：半夜/沒資料時也會送「測試清單」
DEBUG_MODE = _get_str("DEBUG_MODE", "0").strip() == "1"

if not LINE_CHANNEL_ACCESS_TOKEN or not LINE_CHANNEL_SECRET:
    raise RuntimeError("Missing env: LINE_CHANNEL_ACCESS_TOKEN or LINE_CHANNEL_SECRET")

# -------------------------
# Flask & LINE init
# -------------------------
app = Flask(__name__)
line_bot_api = LineBotApi(LINE_CHANNEL_ACCESS_TOKEN)
handler = WebhookHandler(LINE_CHANNEL_SECRET)

# -------------------------
# 工具：Yahoo Finance 抓當日漲跌/量
# -------------------------
def _yahoo_symbol(tw_code: str) -> str:
    tw_code = tw_code.strip().upper()
    if tw_code.endswith(".TW") or tw_code.endswith(".TWO"):
        return tw_code
    # 簡化處理：預設視為上市 .TW
    return f"{tw_code}.TW"

def fetch_change_pct_and_volume(tw_code: str) -> Tuple[float, int]:
    """
    回傳：(當日漲跌幅%, 當日成交量)
    先抓 1d/1m，抓不到退回 5d/1d 的最後一筆。
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
        r = requests.get(url, timeout=10)
        r.raise_for_status()
        j = r.json()
        result = (j.get("chart", {}) or {}).get("result", []) or []
        if not result:
            continue

        indicators = result[0].get("indicators", {}) or {}
        quote = (indicators.get("quote") or [{}])[0]
        closes = quote.get("close") or []
        volumes = quote.get("volume") or []

        # 最後一筆有效價/量
        for i in range(len(closes) - 1, -1, -1):
            c = closes[i]
            v = volumes[i] if i < len(volumes) else 0
            if c is not None:
                last_price = c
                last_volume = int(v or 0)
                break

        # 上一筆作為昨收
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

# -------------------------
# 名單來源
# -------------------------
def resolve_watchlist(text: str) -> List[str]:
    t = (text or "").strip().upper()
    if t == "ALL":
        # 這裡只放示例（常見權值股 + 幾檔熱門）；
        # 你要全市場可改成讀 CSV / 你的 API（回傳第一欄代號）
        return [
            # 上市（TW）
            "2330","2317","2454","2303","2412","2882","2881","2884",
            "1303","1101","2603","2609","2615","2002","2885","2891",
            "2357","2377","2382","3481","3008","2308","3045","3711",
            # 上櫃（TWO）例子
            "6415.TWO","3491.TWO","6182.TWO",
            # ETF 例子
            "0050.TW","0056.TW","006208.TW"
        ]
    # 逗號/空白都允許
    tokens = re.split(r"[,\s]+", t)
    return [x for x in (tok.strip() for tok in tokens) if x]

# -------------------------
# 起漲篩選
# -------------------------
def pick_rising_stocks(
    watchlist: List[str],
    min_change_pct: float,
    min_volume: int,
    top_k: int = 200
) -> List[str]:
    rows = []
    for code in watchlist:
        try:
            chg, vol = fetch_change_pct_and_volume(code)
            rows.append((code, chg, vol))
        except Exception:
            # 單一代號失敗略過
            continue

    rows = [r for r in rows if r[1] >= min_change_pct and r[2] >= min_volume]
    rows.sort(key=lambda x: (x[1], x[2]), reverse=True)

    pretty = [f"{i+1}. {code}  漲幅 {chg:.2f}%  量 {vol:,}"
              for i, (code, chg, vol) in enumerate(rows[:top_k])]
    return pretty

def split_messages(lines: List[str]) -> List[str]:
    """依行數與字數限制切段，回傳多則訊息。"""
    if not lines:
        return []
    msgs, buf = [], []
    for ln in lines:
        if len("\n".join(buf + [ln])) > MAX_CHARS_PER_MSG or len(buf) >= MAX_LINES_PER_MSG:
            msgs.append("\n".join(buf))
            buf = []
        buf.append(ln)
    if buf:
        msgs.append("\n".join(buf))
    return msgs

# -------------------------
# Flask 路由
# -------------------------
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
    # 簡單 Echo
    line_bot_api.reply_message(event.reply_token, TextSendMessage(text=event.message.text))

@app.get("/test-push")
def test_push():
    """手動測試推送：/test-push?msg=Hello"""
    msg = request.args.get("msg", "測試推播 OK")
    if not LINE_USER_ID:
        return "Missing env: LINE_USER_ID", 500
    line_bot_api.push_message(LINE_USER_ID, TextSendMessage(text=msg))
    return "Sent!", 200

@app.get("/daily-push")
def daily_push():
    """起漲清單推送（可掛 CRON，或手動點）"""
    if not LINE_USER_ID:
        return "Missing env: LINE_USER_ID", 500

    watchlist = resolve_watchlist(WATCHLIST_RAW)
    picked = pick_rising_stocks(
        watchlist=watchlist,
        min_change_pct=MIN_CHANGE_PCT,
        min_volume=MIN_VOLUME,
        top_k=999
    )

    # 若沒資料且開 DEBUG_MODE，就產生一份「測試清單」
    if not picked and DEBUG_MODE:
        test_lines = [
            f"{i+1}. {code}  測試漲幅 {0.7 + i*0.1:.2f}%  量 {1000 + i*200:,}"
            for i, code in enumerate(watchlist[:10])
        ]
        picked = test_lines

    now = dt.datetime.now(tz=dt.timezone(dt.timedelta(hours=8))).strftime("%Y-%m-%d %H:%M")
    title = f"【{now} 起漲清單】(漲幅≥{MIN_CHANGE_PCT}%, 量≥{MIN_VOLUME})"

    if picked:
        chunks = split_messages(picked)
        # 先送標題
        out = [title] + [f"第{i+1}頁\n{m}" for i, m in enumerate(chunks)]
    else:
        out = [title, "尚無符合條件的個股（或資料未更新）"]

    # 推送（多則逐則送）
    for m in out:
        line_bot_api.push_message(LINE_USER_ID, TextSendMessage(text=m))

    return "Push sent!", 200

# healthcheck（可給 Render）
@app.get("/health")
def health():
    return "ok", 200

# -------------------------
# 啟動（本機）
# -------------------------
if __name__ == "__main__":
    port = int(os.getenv("PORT", "5000"))
    app.run(host="0.0.0.0", port=port)