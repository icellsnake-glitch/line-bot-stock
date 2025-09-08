import os
import re
import csv
import io
import requests
import datetime as dt
from typing import List, Tuple

from flask import Flask, request, abort
from linebot import LineBotApi, WebhookHandler
from linebot.exceptions import InvalidSignatureError
from linebot.models import MessageEvent, TextMessage, TextSendMessage


# ========= Flask & LINE 基本 =========
app = Flask(__name__)

CHANNEL_ACCESS_TOKEN = os.getenv("LINE_CHANNEL_ACCESS_TOKEN")
CHANNEL_SECRET = os.getenv("LINE_CHANNEL_SECRET")
USER_ID = os.getenv("LINE_USER_ID")                 # 你的個人 User ID（測試推播用）
CRON_SECRET = os.getenv("CRON_SECRET", "s3cr3t")    # 排程用簡易密鑰

if not CHANNEL_ACCESS_TOKEN or not CHANNEL_SECRET:
    raise RuntimeError("Missing LINE_CHANNEL_ACCESS_TOKEN or LINE_CHANNEL_SECRET")

line_bot_api = LineBotApi(CHANNEL_ACCESS_TOKEN)
handler = WebhookHandler(CHANNEL_SECRET)


# ========= 門檻參數（可用環境變數覆蓋）=========
def _get_float(name: str, default: float) -> float:
    val = os.getenv(name, str(default)).strip()
    return float(val) if val else default

def _get_int(name: str, default: int) -> int:
    val = os.getenv(name, str(default)).strip()
    return int(val) if val else default

MIN_CHANGE_PCT    = _get_float("MIN_CHANGE_PCT",    2.0)       # 今日漲幅(%) 下限
MIN_VOLUME        = _get_int(  "MIN_VOLUME",        1_000_000) # 今日量 下限(股)
MAX_LINES_PER_MSG = _get_int(  "MAX_LINES_PER_MSG", 25)        # 每則訊息最多行數
MAX_CHARS_PER_MSG = _get_int(  "MAX_CHARS_PER_MSG", 1900)      # 每則訊息最多字數(留點緩衝)

WATCHLIST_MODE    = os.getenv("WATCHLIST", "2330,2317,2454,2603,2882").strip().upper()
# 建議自己準備清單 CSV，第一欄是代號；多個來源用逗號分隔
# 例如：LIST_SOURCES=https://your.site/listed.csv,https://your.site/otc.csv
LIST_SOURCES      = [u.strip() for u in os.getenv("LIST_SOURCES", "").split(",") if u.strip()]

# ========= 小工具 =========
def _tw_symbol(code: str) -> str:
    """2330 -> 2330.TW（上市），若已包含 .TW/.TWO 就原樣回傳"""
    code = code.strip().upper()
    if code.endswith(".TW") or code.endswith(".TWO"):
        return code
    # 不知道上市/上櫃時，先預設 .TW；拿不到就會過濾掉，不影響穩定性
    return f"{code}.TW"

def _is_code(token: str) -> bool:
    """是否像 4 位數台股代號（允許 ETF 4碼），過濾奇怪欄位"""
    return bool(re.fullmatch(r"\d{4}", token.strip()))

def split_messages(blocks: List[str]) -> List[str]:
    """把多行文字分裝成多則，避免超過 LINE 限制"""
    packs, cur = [], ""
    for line in blocks:
        # 先試試看併進去
        candidate = (cur + ("\n" if cur else "") + line) if cur else line
        if candidate.count("\n") + 1 > MAX_LINES_PER_MSG or len(candidate) > MAX_CHARS_PER_MSG:
            # 目前這則已滿，先收
            if cur:
                packs.append(cur)
            cur = line  # 另起一則
        else:
            cur = candidate
    if cur:
        packs.append(cur)
    return packs


# ========= 來源清單（全市場代號）=========
def fetch_codes_from_csv_url(url: str) -> List[str]:
    """從遠端 CSV 下載代號（第一欄為代號）"""
    try:
        r = requests.get(url, timeout=15)
        r.raise_for_status()
        txt = r.text
        f = io.StringIO(txt)
        reader = csv.reader(f)
        codes = []
        for row in reader:
            if not row:
                continue
            c0 = row[0].strip()
            if _is_code(c0):
                codes.append(c0)
        return codes
    except Exception:
        return []

def fetch_all_market_codes() -> List[str]:
    """
    取得「全市場」代號的三種方式（由易到難）：
    1) 你提供 LIST_SOURCES（建議）：CSV 第一欄放代號
    2) 嘗試台/櫃公開資料（失敗就跳過，不中斷）
    3) 最後退回小型示範清單
    """
    # (1) 你給的 CSV 來源（最可靠）
    codes: List[str] = []
    for u in LIST_SOURCES:
        codes += fetch_codes_from_csv_url(u)
    codes = list(dict.fromkeys([c for c in codes if _is_code(c)]))  # 去重

    # (2) 嘗試公開來源（抓不到就算了；盡量溫和）
    if not codes:
        try:
            # TWSE 開放資料：上市公司基本資料（欄位含 "公司代號"）
            # 來源說明：openapi.twse.com.tw v1
            u1 = "https://openapi.twse.com.tw/v1/opendata/t187ap03_L"
            r1 = requests.get(u1, timeout=15)
            if r1.ok:
                j1 = r1.json()
                for it in j1:
                    c = (it.get("公司代號") or "").strip()
                    if _is_code(c):
                        codes.append(c)
        except Exception:
            pass
        try:
            # TPEX（上櫃）簡單名單，若來源失敗則略過
            u2 = "https://www.tpex.org.tw/openapi/v1/mopsfin_t187ap03_O"  # 同結構（公司代號）
            r2 = requests.get(u2, timeout=15)
            if r2.ok:
                j2 = r2.json()
                for it in j2:
                    c = (it.get("公司代號") or "").strip()
                    if _is_code(c):
                        codes.append(c)
        except Exception:
            pass

        codes = list(dict.fromkeys(codes))

    # (3) 最小退回清單，避免整體失敗
    if not codes:
        codes = ["2330", "2317", "2454", "2303", "2412", "2882", "2603", "1216", "1101", "1301"]

    return codes


# ========= 抓 Yahoo 當日漲幅 / 量 =========
def fetch_change_pct_and_volume(tw_code: str) -> Tuple[float, int]:
    """
    回傳：(當日漲跌幅%, 當日成交量)
    先用 1d/1m intraday；失敗再用 5d/1d 日線。
    """
    symbol = _tw_symbol(tw_code)
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

        # 取最後一筆有效值
        for i in range(len(closes) - 1, -1, -1):
            c = closes[i]
            v = volumes[i] if i < len(volumes) else 0
            if c is not None:
                last_price = c
                last_volume = int(v or 0)
                break

        # 取上一筆作為昨收
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


def pick_rising_stocks(codes: List[str],
                       min_change_pct: float,
                       min_volume: int,
                       top_k: int = 50) -> List[Tuple[str, float, int]]:
    rows = []
    for code in codes:
        try:
            chg, vol = fetch_change_pct_and_volume(code)
            rows.append((code, chg, vol))
        except Exception:
            continue

    rows = [r for r in rows if r[1] >= min_change_pct and r[2] >= min_volume]
    rows.sort(key=lambda x: x[1], reverse=True)
    return rows[:top_k]


def format_blocks(rows: List[Tuple[str, float, int]], title: str) -> List[str]:
    if not rows:
        return [f"{title}\n尚無符合條件個股（或資料未更新）"]

    lines = [f"{i+1}. {c}  ↑{chg:.2f}%  量 {vol:,}" for i, (c, chg, vol) in enumerate(rows)]
    first_line = title
    packs = split_messages([first_line] + lines)
    return packs


# ========= Web 路由 =========
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
    line_bot_api.reply_message(
        event.reply_token,
        TextSendMessage(text=event.message.text)
    )

@app.get("/test-push")
def test_push():
    try:
        if not USER_ID:
            return "Missing env: LINE_USER_ID", 500
        now = dt.datetime.now(tz=dt.timezone(dt.timedelta(hours=8))).strftime("%Y-%m-%d %H:%M")
        line_bot_api.push_message(USER_ID, TextSendMessage(text=f"測試推播 OK ：{now}"))
        return "Push sent!", 200
    except Exception as e:
        app.logger.exception(e)
        return str(e), 500


# ---- 觸發掃描（手動）----
@app.get("/run-scan")
def run_scan():
    # 可加 ?secret=xxx 簡單保護；若沒設就略過
    sec = request.args.get("secret", "")
    if CRON_SECRET and sec != CRON_SECRET:
        return "Forbidden", 403
    return _do_scan_and_push()


# ---- 觸發掃描（排程會打這個）----
@app.get("/daily-push")
def daily_push():
    # 也支援 ?secret=xxx
    sec = request.args.get("secret", "")
    if CRON_SECRET and sec != CRON_SECRET:
        return "Forbidden", 403
    return _do_scan_and_push()


def _do_scan_and_push():
    try:
        if not USER_ID:
            return "Missing env: LINE_USER_ID", 500

        # 取得待掃清單
        if WATCHLIST_MODE == "ALL":
            watchlist = fetch_all_market_codes()
        else:
            # WATCHLIST="2330,2317,2454" 或含 .TW/.TWO
            watchlist = [c.strip().upper() for c in WATCHLIST_MODE.split(",") if c.strip()]

        picked = pick_rising_stocks(
            codes=watchlist,
            min_change_pct=MIN_CHANGE_PCT,
            min_volume=MIN_VOLUME,
            top_k=200,            # 先挑最多200，再分段送出
        )

        today = dt.datetime.now(tz=dt.timezone(dt.timedelta(hours=8))).strftime("%Y-%m-%d")
        title = f"【{today} 起漲清單 (≥{MIN_CHANGE_PCT:.2f}%, 量≥{MIN_VOLUME:,})】"
        packs = format_blocks(picked, title)

        # 分段推送
        for p in packs:
            line_bot_api.push_message(USER_ID, TextSendMessage(text=p))

        return f"OK, sent {len(packs)} message(s).", 200

    except Exception as e:
        app.logger.exception(e)
        return str(e), 500


# （可選）預留一個 Richmenu 設定路由，避免之後再加出現 app 未定義
@app.get("/setup-richmenu")
def setup_richmenu():
    return "Richmenu setup endpoint", 200


# ========= 入口 =========
if __name__ == "__main__":
    port = int(os.getenv("PORT", "5000"))
    app.run(host="0.0.0.0", port=port)