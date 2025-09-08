import os
import re
import datetime as dt
from typing import List, Tuple
import requests
from flask import Flask, request, abort

# -------------------------
# Flask
# -------------------------
app = Flask(__name__)

# -------------------------
# 小工具：安全讀 env（延後到用到再讀）
# -------------------------
def env_str(key: str, default: str = "") -> str:
    v = os.getenv(key, "").strip()
    return v if v != "" else default

def env_float(key: str, default: float) -> float:
    v = os.getenv(key)
    return float(v.strip()) if v and v.strip() != "" else default

def env_int(key: str, default: int) -> int:
    v = os.getenv(key)
    return int(v.strip()) if v and v.strip() != "" else default

# -------------------------
# 參數（可用 Render Environment 覆蓋）
# -------------------------
MIN_CHANGE_PCT     = env_float("MIN_CHANGE_PCT", 2.0)        # 今日漲幅門檻（%）
MIN_VOLUME         = env_int("MIN_VOLUME", 1_000_000)        # 今日量門檻（股）
MAX_LINES_PER_MSG  = env_int("MAX_LINES_PER_MSG", 25)        # 每則訊息最多幾行
MAX_CHARS_PER_MSG  = env_int("MAX_CHARS_PER_MSG", 1900)      # 每則訊息最多字數

WATCHLIST_ENV      = env_str("WATCHLIST", "2330,2317,2454,2603,2882")
LIST_SOURCES_ENV   = env_str("LIST_SOURCES", "")             # ALL 模式用，CSV/多來源以逗號分隔

EMOJI_LISTED = env_str("EMOJI_LISTED", "📈")
EMOJI_OTC    = env_str("EMOJI_OTC", "🚀")
EMOJI_ETF    = env_str("EMOJI_ETF", "🧺")

# -------------------------
# Yahoo Finance 抓價量
# -------------------------
def _yahoo_symbol(tw_code: str) -> str:
    code = tw_code.strip().upper()
    if code.endswith(".TW") or code.endswith(".TWO"):
        return code
    # 簡化：預設上市 .TW；若要上櫃可在 watchlist 直接寫 .TWO
    return f"{code}.TW"

def fetch_change_pct_and_volume(tw_code: str) -> Tuple[float, int]:
    """
    回傳 (當日漲跌幅%, 當日成交量)
    先嘗試 1d/1m，失敗退回 5d/1d。
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

        # 取最後一筆有效
        for i in range(len(closes) - 1, -1, -1):
            c = closes[i]
            v = volumes[i] if i < len(volumes) else 0
            if c is not None:
                last_price = c
                last_volume = int(v or 0)
                break

        # 前一筆當昨收
        for i in range(len(closes) - 2, -1, -1):
            c = closes[i]
            if c is not None:
                last_close = c
                break

        if last_price is not None and last_close is not None:
            break

    if last_price is None or last_close is None or last_close == 0:
        return 0.0, 0

    chg = (last_price - last_close) / last_close * 100.0
    return round(chg, 2), last_volume

# -------------------------
# 讀清單：支援 WATCHLIST=逗號 或 ALL+LIST_SOURCES
# -------------------------
CODE_RE = re.compile(r"^\s*([0-9]{4})(?:\.(TW|TWO))?\s*$")

def parse_code(token: str) -> str | None:
    """
    合法代號（4碼，可選 .TW/.TWO），回傳規範化字串；否則 None
    """
    m = CODE_RE.match(token)
    if not m:
        return None
    code, suffix = m.group(1), m.group(2)
    if suffix:
        return f"{code}.{suffix}"
    return code  # 無尾碼者，掃描時會自動補 .TW

def read_watchlist() -> List[str]:
    wl = WATCHLIST_ENV.strip()
    if wl.upper() != "ALL":
        out = []
        for t in wl.split(","):
            norm = parse_code(t)
            if norm:
                out.append(norm)
        return list(dict.fromkeys(out))  # 去重

    # ALL 模式：從 LIST_SOURCES 蒐集（CSV / 純文字），多來源以逗號分隔
    sources = [u.strip() for u in LIST_SOURCES_ENV.split(",") if u.strip()]
    if not sources:
        return []

    codes: List[str] = []
    for src in sources:
        try:
            if src.startswith("http"):
                resp = requests.get(src, timeout=10)
                resp.raise_for_status()
                text = resp.text
            else:
                # 允許把清單直接貼到 env（多行文字）
                text = src
            # 抓每行第一欄（逗號/分隔），或直接掃 4 碼
            for line in text.splitlines():
                first = line.split(",")[0].strip()
                token = parse_code(first) or parse_code(line.strip())
                if token:
                    codes.append(token)
        except Exception:
            continue

    # 去重
    codes = list(dict.fromkeys(codes))
    return codes

# -------------------------
# 過濾＋排版
# -------------------------
def pick_rising_stocks(watchlist: List[str],
                       min_change_pct: float,
                       min_volume: int,
                       top_k: int | None = None) -> List[tuple[str, float, int]]:
    rows: List[tuple[str, float, int]] = []
    for code in watchlist:
        try:
            chg, vol = fetch_change_pct_and_volume(code)
            if chg >= min_change_pct and vol >= min_volume:
                rows.append((code, chg, vol))
        except Exception:
            continue
    rows.sort(key=lambda x: x[1], reverse=True)
    return rows[:top_k] if top_k else rows

def pretty_lines(rows: List[tuple[str, float, int]]) -> List[str]:
    out = []
    for i, (code, chg, vol) in enumerate(rows, 1):
        # 判斷上市/上櫃/ETF（超簡化：用尾碼／代號 00 開頭等，你也可改成更嚴謹）
        emoji = EMOJI_LISTED
        u = code.upper()
        if u.endswith(".TWO"):
            emoji = EMOJI_OTC
        if u.startswith("00") or u.startswith("008") or u.startswith("009"):
            emoji = EMOJI_ETF
        out.append(f"{i:>2}. {emoji} {code.replace('.TW','').replace('.TWO','')}  +{chg:.2f}%  量 {vol:,}")
    return out

def split_messages(lines: List[str]) -> List[str]:
    msgs, cur = [], ""
    for ln in lines:
        # 超過字數或行數就換訊息
        if (cur and (len(cur) + 1 + len(ln) > MAX_CHARS_PER_MSG)) or (cur.count("\n") + 1 >= MAX_LINES_PER_MSG):
            msgs.append(cur)
            cur = ""
        cur = ln if not cur else (cur + "\n" + ln)
    if cur:
        msgs.append(cur)
    return msgs

# -------------------------
# LINE 推播（延後載入，避免啟動時卡住）
# -------------------------
def get_line_clients():
    access_token = env_str("LINE_CHANNEL_ACCESS_TOKEN")
    channel_secret = env_str("LINE_CHANNEL_SECRET")
    user_id = env_str("LINE_USER_ID")
    if not access_token or not channel_secret or not user_id:
        return None, None, None
    try:
        from linebot import LineBotApi, WebhookHandler
        return LineBotApi(access_token), WebhookHandler(channel_secret), user_id
    except Exception as e:
        app.logger.exception(e)
        return None, None, None

def push_lines(msgs: List[str]) -> str:
    line_bot_api, _, user_id = get_line_clients()
    if not line_bot_api or not user_id:
        return "LINE 環境變數未設定（LINE_CHANNEL_ACCESS_TOKEN / LINE_CHANNEL_SECRET / LINE_USER_ID）"
    from linebot.models import TextSendMessage
    for m in msgs:
        line_bot_api.push_message(user_id, TextSendMessage(text=m))
    return "OK"

# -------------------------
# 路由
# -------------------------
@app.get("/healthz")
def healthz():
    return "ok", 200

@app.get("/")
def home():
    return "Hello, I'm alive", 200

@app.get("/daily-push")
def daily_push():
    try:
        wl = read_watchlist()
        if not wl:
            return "清單為空：請設定 WATCHLIST（逗號清單），或 WATCHLIST=ALL 並提供 LIST_SOURCES", 200

        picked = pick_rising_stocks(
            watchlist=wl,
            min_change_pct=MIN_CHANGE_PCT,
            min_volume=MIN_VOLUME
        )
        today = dt.datetime.now(tz=dt.timezone(dt.timedelta(hours=8))).strftime("%Y-%m-%d")
        if not picked:
            msgs = [f"【{today} 起漲清單】\n尚無符合條件（或市場未開/資料未更新）"]
        else:
            lines = pretty_lines(picked)
            header = f"【{today} 起漲清單】（門檻：漲≥{MIN_CHANGE_PCT}%、量≥{MIN_VOLUME:,}）"
            lines = [header, ""] + lines
            msgs = split_messages(lines)

        status = push_lines(msgs)
        return (f"Push sent! ({status})", 200)
    except Exception as e:
        app.logger.exception(e)
        return (f"Error: {e}", 500)

# （若你有 LINE webhook，可在下方加上 /callback，不影響上面三個路由）
# @app.post("/callback")
# def callback():
#     ...

if __name__ == "__main__":
    port = int(os.getenv("PORT", "5000"))
    app.run(host="0.0.0.0", port=port)