import os
import re
import csv
import io
import datetime as dt
from typing import List, Tuple, Iterable, Optional

import requests
from flask import Flask, request, abort

# ======（可選）LINE SDK：若你之前已安裝並使用，保留；否則也可改走 requests 直呼 API ======
from linebot import LineBotApi, WebhookHandler
from linebot.exceptions import InvalidSignatureError
from linebot.models import MessageEvent, TextMessage, TextSendMessage


# -----------------------
# Flask
# -----------------------
app = Flask(__name__)


# -----------------------
# 讀環境變數（含預設值）
# -----------------------
def _getenv_str(key: str, default: str = "") -> str:
    v = os.getenv(key, "")
    return v if v is not None else default

def _getenv_float(key: str, default: float) -> float:
    v = os.getenv(key, "").strip()
    if v == "":
        return default
    try:
        return float(v)
    except Exception:
        return default

def _getenv_int(key: str, default: int) -> int:
    v = os.getenv(key, "").strip()
    if v == "":
        return default
    try:
        return int(v)
    except Exception:
        return default


# ====== 臨界值（給你常用的門檻，若沒設環境變數，就用預設） ======
MIN_CHANGE_PCT   = _getenv_float("MIN_CHANGE_PCT",   2.0)      # 今日漲幅(%) 門檻
MIN_VOLUME       = _getenv_int  ("MIN_VOLUME",       1_000_000)# 今日成交量(股) 門檻
MAX_LINES_PER_MSG= _getenv_int  ("MAX_LINES_PER_MSG",25)       # LINE 單則訊息最多行（避免過長）
MAX_CHARS_PER_MSG= _getenv_int  ("MAX_CHARS_PER_MSG",1900)     # LINE 單則訊息最多字元
MAX_SCAN         = _getenv_int  ("MAX_SCAN",         800)      # 最高掃描上限（ALL 時避免過量）

WATCHLIST_ENV    = _getenv_str  ("WATCHLIST",        "2330,2317,2454,2603,2882")
LIST_SOURCES_ENV = _getenv_str  ("LIST_SOURCES",     "")       # 逗號分隔 CSV 來源(第一欄為代號)

# Emoji 標記（可空白）
EMOJI_LISTED     = _getenv_str  ("EMOJI_LISTED", "🔵")
EMOJI_OTC        = _getenv_str  ("EMOJI_OTC",    "🟣")
EMOJI_ETF        = _getenv_str  ("EMOJI_ETF",    "🟢")

# LINE
LINE_ACCESS_TOKEN = os.getenv("LINE_CHANNEL_ACCESS_TOKEN", "")
LINE_CHANNEL_SECRET = os.getenv("LINE_CHANNEL_SECRET", "")
USER_ID           = os.getenv("LINE_USER_ID", "")  # 你的 User ID（測試推播用）

if LINE_ACCESS_TOKEN and LINE_CHANNEL_SECRET:
    line_bot_api = LineBotApi(LINE_ACCESS_TOKEN)
    handler = WebhookHandler(LINE_CHANNEL_SECRET)
else:
    line_bot_api = None
    handler = None


# -----------------------
# Yahoo 資料（不需金鑰）
# -----------------------
def _yahoo_symbol(tw_code: str) -> str:
    """
    將台股代號轉成 Yahoo 代號：
    - 上市（或已帶 .TW） => 2330.TW
    - 上櫃（或已帶 .TWO）=> 6488.TWO
    - 若無法判斷，預設 .TW
    """
    c = tw_code.strip().upper()
    if c.endswith(".TW") or c.endswith(".TWO"):
        return c
    # 你若有清單標注 'OTC' 可自動判斷，這裡先預設 .TW
    return f"{c}.TW"

def fetch_change_pct_and_volume(tw_code: str) -> Tuple[float, int]:
    """
    回傳：(當日漲跌幅%, 當日成交量)
    先抓 1d/1m；若拿不到就退回 5d/1d。
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

        # 最後有效價
        for i in range(len(closes) - 1, -1, -1):
            c = closes[i]
            v = volumes[i] if i < len(volumes) else 0
            if c is not None:
                last_price = c
                last_volume = int(v or 0)
                break

        # 昨收（上一筆）
        for i in range(len(closes) - 2, -1, -1):
            c = closes[i]
            if c is not None:
                last_close = c
                break

        if last_price is not None and last_close is not None:
            break

    if not last_price or not last_close or last_close == 0:
        return 0.0, 0

    chg = (last_price - last_close) / last_close * 100.0
    return round(chg, 2), last_volume


# -----------------------
# 代號清單（ALL / CSV / 手動）
# -----------------------
CODE_4DIGIT = re.compile(r"^\d{4}$")

def _normalize_code(token: str) -> Optional[str]:
    """
    僅保留「4碼數字」或已帶尾碼 .TW/.TWO 的代號，其他丟棄。
    """
    t = token.strip().upper()
    if not t:
        return None
    if t.endswith(".TW") or t.endswith(".TWO"):
        # 移除奇怪空白/全形
        return t.replace(" ", "")
    if CODE_4DIGIT.match(t):
        return t
    return None

def _iter_csv_codes(url: str) -> Iterable[str]:
    """
    讀取「第一欄為代號」的 CSV，回傳代號迭代器。
    """
    try:
        resp = requests.get(url, timeout=15)
        resp.raise_for_status()
        content = resp.content.decode("utf-8", errors="ignore")
        f = io.StringIO(content)
        reader = csv.reader(f)
        for row in reader:
            if not row:
                continue
            code = _normalize_code(row[0])
            if code:
                yield code
    except Exception:
        # 某個來源失敗就跳過
        return []

def get_watchlist() -> List[str]:
    """
    取得掃描名單：
    - WATCHLIST = ALL => 從 LIST_SOURCES 指定的多個 CSV 收集代號
    - 否則 WATCHLIST 逗號分隔
    """
    if WATCHLIST_ENV.strip().upper() == "ALL":
        urls = [u.strip() for u in LIST_SOURCES_ENV.split(",") if u.strip()]
        codes: List[str] = []
        for u in urls:
            for c in _iter_csv_codes(u):
                codes.append(c)
        # 去重、最多 MAX_SCAN
        seen = set()
        uniq: List[str] = []
        for c in codes:
            if c not in seen:
                seen.add(c)
                uniq.append(c)
                if len(uniq) >= MAX_SCAN:
                    break
        return uniq
    else:
        tokens = [t for t in WATCHLIST_ENV.split(",")]
        out = []
        for t in tokens:
            c = _normalize_code(t)
            if c:
                out.append(c)
        return out[:MAX_SCAN]


# -----------------------
# 過濾 & 排序
# -----------------------
def pick_rising_stocks(
    watchlist: List[str],
    min_change_pct: float = MIN_CHANGE_PCT,
    min_volume: int = MIN_VOLUME,
    top_k: int = 200,
) -> List[str]:
    rows = []
    for code in watchlist:
        try:
            chg, vol = fetch_change_pct_and_volume(code)
            if chg >= min_change_pct and vol >= min_volume:
                rows.append((code, chg, vol))
        except Exception:
            continue

    rows.sort(key=lambda x: x[1], reverse=True)

    # 排版（簡易加入市場 emoji）
    def _emoji_for(code: str) -> str:
        if code.endswith(".TWO"):
            return EMOJI_OTC
        if code.startswith("00") or code.startswith("10") and code.endswith(".TW"):
            # 不準確的 ETF 判斷示意；若你在 CSV 有標記 ETF，更精準
            return EMOJI_ETF
        return EMOJI_LISTED

    pretty = [
        f"{i+1}. {_emoji_for(code)} {code}  漲幅 {chg:.2f}%  量 {vol:,}"
        for i, (code, chg, vol) in enumerate(rows[:top_k])
    ]
    return pretty


# -----------------------
# 組訊息 & 推播
# -----------------------
def build_today_message() -> str:
    watch = get_watchlist()
    items = pick_rising_stocks(
        watchlist=watch,
        min_change_pct=MIN_CHANGE_PCT,
        min_volume=MIN_VOLUME,
        top_k=MAX_SCAN
    )
    today = dt.datetime.now(dt.timezone(dt.timedelta(hours=8))).strftime("%Y-%m-%d")
    if items:
        return f"【{today} 起漲清單】\n" + "\n".join(items)
    return f"【{today} 起漲清單】\n尚無符合條件的個股（或資料未更新）"


# -----------------------
# Web：首頁顯示，?push=1 可同時推播
# -----------------------
@app.get("/")
def home():
    text = build_today_message()
    if request.args.get("push") == "1" and USER_ID and line_bot_api:
        try:
            line_bot_api.push_message(USER_ID, TextSendMessage(text=text))
        except Exception as e:
            app.logger.exception(e)
            text += f"\n\n(推播失敗：{e})"
    return text, 200, {"Content-Type": "text/plain; charset=utf-8"}


# -----------------------
# 手動推播 API
# -----------------------
@app.get("/daily-push")
def daily_push():
    if not USER_ID:
        return "Missing env: LINE_USER_ID", 500
    if not line_bot_api:
        return "LINE SDK not ready", 500
    text = build_today_message()
    line_bot_api.push_message(USER_ID, TextSendMessage(text=text))
    return "Push sent!", 200


# -----------------------
# LINE Webhook（可選）
# -----------------------
if handler and line_bot_api:
    @app.post("/callback")
    def callback():
        sig = request.headers.get("X-Line-Signature", "")
        body = request.get_data(as_text=True)
        try:
            handler.handle(body, sig)
        except InvalidSignatureError:
            abort(400)
        return "OK"

    @handler.add(MessageEvent, message=TextMessage)
    def handle_message(event):
        # Echo（可改自訂功能）
        line_bot_api.reply_message(
            event.reply_token,
            TextSendMessage(text=event.message.text)
        )


# -----------------------
# 健康檢查
# -----------------------
@app.get("/healthz")
def healthz():
    return "ok", 200


# -----------------------
# 本地啟動
# -----------------------
if __name__ == "__main__":
    port = int(os.getenv("PORT", "5000"))
    app.run(host="0.0.0.0", port=port)