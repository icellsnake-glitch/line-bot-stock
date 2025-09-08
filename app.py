import os
import datetime as dt
from typing import List, Tuple
import requests
from flask import Flask, request

app = Flask(__name__)

# ========= 讀環境變數（安全且防空字串） =========
def _f(name: str, default: float) -> float:
    v = (os.getenv(name) or "").strip()
    try:
        return float(v) if v else default
    except Exception:
        return default

def _i(name: str, default: int) -> int:
    v = (os.getenv(name) or "").strip()
    try:
        return int(v) if v else default
    except Exception:
        return default

LINE_TOKEN = (os.getenv("LINE_CHANNEL_ACCESS_TOKEN") or "").strip()
LINE_USER_ID = (os.getenv("LINE_USER_ID") or "").strip()

MIN_CHANGE_PCT = _f("MIN_CHANGE_PCT", 2.0)     # 漲幅門檻(%)，預設 2%
MIN_VOLUME     = _i("MIN_VOLUME",   1_000_000) # 成交量門檻(股)，預設 100 萬
MAX_LINES_PER_MSG = _i("MAX_LINES_PER_MSG", 25)
MAX_CHARS_PER_MSG = _i("MAX_CHARS_PER_MSG", 1800)

_default_watchlist = "2330,2317,2454,2303,2603,2882,2412,1303,1101,2377,3661,3481"
WATCHLIST = (os.getenv("WATCHLIST") or _default_watchlist).strip()

# ========= 小工具 =========
def _yahoo_symbol(tw_code: str) -> str:
    tw_code = tw_code.strip().upper()
    if tw_code.endswith(".TW") or tw_code.endswith(".TWO"):
        return tw_code
    return f"{tw_code}.TW"

def fetch_change_pct_and_volume(tw_code: str) -> Tuple[float, int]:
    """
    回傳：(當日漲跌幅%, 當日成交量)
    優先取 1d/1m 內盤；若拿不到，退 5d/1d。
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
        result = j.get("chart", {}).get("result", [])
        if not result:
            continue
        indicators = result[0].get("indicators", {})
        quote = (indicators.get("quote") or [{}])[0]
        closes = quote.get("close") or []
        volumes = quote.get("volume") or []

        # 最後一筆有效價格/量
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

    if not last_price or not last_close:
        return 0.0, 0

    change_pct = (last_price - last_close) / last_close * 100.0
    return round(change_pct, 2), last_volume

def chunks_by_limit(lines: List[str], max_lines: int, max_chars: int) -> List[str]:
    """把多行切成多段，避免超過 LINE 訊息限制"""
    pages, buf, chars = [], [], 0
    for s in lines:
        if (len(buf) + 1 > max_lines) or (chars + len(s) + 1 > max_chars):
            pages.append("\n".join(buf))
            buf, chars = [], 0
        buf.append(s); chars += len(s) + 1
    if buf:
        pages.append("\n".join(buf))
    return pages

def send_line_message(user_id: str, text: str) -> Tuple[int, str]:
    """用 requests 直接打 LINE Push API（不需 line-bot-sdk）"""
    if not LINE_TOKEN:
        return 0, "Missing LINE_CHANNEL_ACCESS_TOKEN"
    url = "https://api.line.me/v2/bot/message/push"
    headers = {
        "Authorization": f"Bearer {LINE_TOKEN}",
        "Content-Type": "application/json",
    }
    payload = {"to": user_id, "messages": [{"type": "text", "text": text}]}
    r = requests.post(url, headers=headers, json=payload, timeout=10)
    return r.status_code, r.text

# ========= 路由 =========
@app.get("/")
def root():
    return (
        "✅ LINE Bot 股票推播服務啟動中\n"
        "- 健康檢查 OK\n"
        "- 手動推送請訪問：/daily-push\n"
        "- 單純測試請訪問：/test-push?msg=Hello\n"
    ), 200

@app.get("/test-push")
def test_push():
    if not LINE_USER_ID:
        return "Missing env: LINE_USER_ID", 500
    msg = request.args.get("msg", "測試推播 OK")
    code, text = send_line_message(LINE_USER_ID, msg)
    return (f"Push sent! ({code})" if code == 200 else f"Push failed: {text}"), 200

@app.get("/daily-push")
def daily_push():
    try:
        if not LINE_USER_ID:
            return "Missing env: LINE_USER_ID", 500

        # 解析 watchlist
        codes = [c.strip() for c in WATCHLIST.split(",") if c.strip()]
        rows = []
        for c in codes:
            try:
                chg, vol = fetch_change_pct_and_volume(c)
                rows.append((c, chg, vol))
            except Exception:
                # 單一代號出錯就略過
                continue

        picked = [r for r in rows if r[1] >= MIN_CHANGE_PCT and r[2] >= MIN_VOLUME]
        picked.sort(key=lambda x: x[1], reverse=True)

        tz8 = dt.timezone(dt.timedelta(hours=8))
        today = dt.datetime.now(tz=tz8).strftime("%Y-%m-%d %H:%M")
        header = f"【{today} 起漲清單】(漲幅≥{MIN_CHANGE_PCT}%, 量≥{MIN_VOLUME:,})"
        if not picked:
            pages = [header + "\n尚無符合條件的個股（或資料未更新）"]
        else:
            body_lines = [f"{i+1}. {c}  漲幅 {chg:.2f}%  量 {vol:,}"
                          for i, (c, chg, vol) in enumerate(picked)]
            pages = chunks_by_limit([header, ""] + body_lines,
                                    MAX_LINES_PER_MSG, MAX_CHARS_PER_MSG)

        # 逐頁送出
        ok = 0
        for p in pages:
            code, _ = send_line_message(LINE_USER_ID, p)
            if code == 200:
                ok += 1

        return f"Sent {ok}/{len(pages)} page(s).", 200
    except Exception as e:
        app.logger.exception(e)
        return str(e), 500

if __name__ == "__main__":
    port = int(os.getenv("PORT", "10000"))
    app.run(host="0.0.0.0", port=port)