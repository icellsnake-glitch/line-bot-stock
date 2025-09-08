# ===================== 參數（可用 Render 環境變數覆寫） =====================
import os
import re
import datetime as dt
import requests
from typing import List, Tuple, Dict
from bs4 import BeautifulSoup
from linebot.models import TextSendMessage

# ----- 門檻（預設值你可改） -----
MIN_CHANGE          = float(os.getenv("MIN_CHANGE_PCT", "2.0"))         # 起漲門檻：今漲幅 %
MIN_VOLUME          = int(os.getenv("MIN_VOLUME", "1000000"))           # 今量（股）

# 可選：前一日條件（預設不開）
USE_YDAY_FILTER     = os.getenv("USE_YDAY_FILTER", "0") == "1"
MIN_CHANGE_PRE      = float(os.getenv("MIN_CHANGE_PCT_PRE", "-1000"))   # 前一日漲幅下限
MIN_VOLUME_PRE      = int(os.getenv("MIN_VOLUME_PRE", "0"))             # 前一日量下限

# ----- 推播分段上限 -----
MAX_LINES_PER_MSG   = int(os.getenv("MAX_LINES_PER_MSG", "18"))
MAX_CHARS_PER_MSG   = int(os.getenv("MAX_CHARS_PER_MSG", "4500"))

# ----- 其他 -----
TOP_K               = int(os.getenv("TOP_K", "50"))                     # 每一群最多取幾檔
CRON_SECRET         = os.getenv("CRON_SECRET", "").strip()              # 若設了就要帶 ?secret= 才能觸發
WATCHLIST_RAW       = os.getenv("WATCHLIST", "ALL").strip()             # ALL = 全市場

# Emoji（可換）
EMOJI_LISTED        = os.getenv("EMOJI_LISTED", "🏦")
EMOJI_OTC           = os.getenv("EMOJI_OTC", "🏬")
EMOJI_ETF           = os.getenv("EMOJI_ETF", "📈")

# ===================== 你已經有的工具：Yahoo 取價量 =====================
def _yahoo_symbol(tw_code: str, market: str | None = None) -> str:
    """
    2330 + 市場 => 2330.TW / 2330.TWO
    若 market 未提供，僅數字則預設 .TW
    """
    tw_code = tw_code.strip().upper()
    if tw_code.endswith(".TW") or tw_code.endswith(".TWO"):
        return tw_code
    if market == "上櫃":
        return f"{tw_code}.TWO"
    return f"{tw_code}.TW"

def fetch_change_pct_and_volume(symbol_or_code: str) -> Tuple[float, int, float, int]:
    """
    回傳：(今日漲跌幅%, 今日量, 昨日漲跌幅%, 昨日量)
    用 1d/1m 拿不到就退 5d/1d。
    """
    # 若傳進來已含 .TW/.TWO 就直接用；否則預設 .TW
    s = symbol_or_code if symbol_or_code.endswith((".TW", ".TWO")) else f"{symbol_or_code}.TW"
    urls = [
        f"https://query1.finance.yahoo.com/v8/finance/chart/{s}?range=1d&interval=1m",
        f"https://query1.finance.yahoo.com/v8/finance/chart/{s}?range=5d&interval=1d",
    ]
    last_close = last_price = None
    last_vol = y_close = y_price = y_vol = 0

    for url in urls:
        r = requests.get(url, timeout=12)
        r.raise_for_status()
        j = r.json()
        result = j.get("chart", {}).get("result", [])
        if not result:
            continue
        quote = (result[0].get("indicators", {}).get("quote") or [{}])[0]
        closes = quote.get("close") or []
        vols   = quote.get("volume") or []

        # 末筆當作今日
        for i in range(len(closes)-1, -1, -1):
            c = closes[i]
            v = vols[i] if i < len(vols) else 0
            if c is not None:
                last_price = c
                last_vol = int(v or 0)
                # 前一筆當作昨收
                for j2 in range(i-1, -1, -1):
                    if closes[j2] is not None:
                        last_close = closes[j2]
                        y_price = closes[j2]
                        y_vol   = int(vols[j2] or 0) if j2 < len(vols) else 0
                        # 再往前一筆作為「前一日的昨收」用來算昨日漲跌
                        for k in range(j2-1, -1, -1):
                            if closes[k] is not None:
                                y_close = closes[k]
                                break
                        break
                break
        if last_price is not None and last_close is not None:
            break

    if not last_price or not last_close:
        return 0.0, 0, 0.0, 0

    chg_today = round((last_price - last_close) / last_close * 100.0, 2)
    chg_yday  = round(((y_price - y_close) / y_close * 100.0), 2) if (y_price and y_close) else 0.0
    return chg_today, last_vol, chg_yday, y_vol

# ===================== 抓全市場代號 + 分群 =====================
ISIN_URL = "https://isin.twse.com.tw/isin/C_public.jsp?strMode=2"

def fetch_universe() -> List[Dict]:
    """
    讀證交所 ISIN 頁（BIG5），回：
    [{code:'2330', name:'台積電', market:'上市', category:'股票'},
     {code:'0050', name:'元大台灣50', market:'上市', category:'ETF'}, ...]
    僅回「上市/上櫃」，其他市場忽略。
    """
    r = requests.get(ISIN_URL, timeout=20)
    r.encoding = "big5"
    soup = BeautifulSoup(r.text, "html.parser")
    table = soup.find("table")
    if not table:
        return []
    rows = table.find_all("tr")[1:]  # 去掉表頭
    out = []
    for tr in rows:
        cols = [td.get_text(strip=True) for td in tr.find_all("td")]
        if len(cols) < 6:
            continue
        code_name, _, _, market, category, _ = cols[:6]
        if market not in ("上市", "上櫃"):
            continue
        m = re.match(r"^([0-9A-Z]+)", code_name)
        if not m:
            continue
        code = m.group(1)
        # 只收數字代號（一般股/ETF）；排除權證、債券等
        if not re.match(r"^\d{3,5}$", code):
            continue
        out.append({"code": code, "market": market, "category": category})
    return out

def split_groups(universe: List[Dict]) -> Dict[str, List[Dict]]:
    """依 類別 → 股票(上市)、股票(上櫃)、ETF 分群"""
    listed = [x for x in universe if x["category"] == "股票" and x["market"] == "上市"]
    otc    = [x for x in universe if x["category"] == "股票" and x["market"] == "上櫃"]
    etf    = [x for x in universe if x["category"] == "ETF"]
    return {"listed": listed, "otc": otc, "etf": etf}

# ===================== 篩選 + 格式化 + 分頁 =====================
def pick_rising(block: List[Dict]) -> List[tuple]:
    """
    對某一群（上市/上櫃/ETF）的清單做篩選。
    回 [(code, chg, vol, market), ...] 依漲幅大到小排序。
    """
    rows = []
    for it in block:
        code, market = it["code"], it["market"]
        sym = _yahoo_symbol(code, market)
        try:
            chg, vol, chg_pre, vol_pre = fetch_change_pct_and_volume(sym)
        except Exception:
            continue

        if chg < MIN_CHANGE or vol < MIN_VOLUME:
            continue
        if USE_YDAY_FILTER and not (chg_pre >= MIN_CHANGE_PRE and vol_pre >= MIN_VOLUME_PRE):
            continue

        rows.append((code, chg, vol, market))
    rows.sort(key=lambda x: x[1], reverse=True)
    return rows[:TOP_K]

def format_lines(rows: List[tuple]) -> List[str]:
    """把 (code, chg, vol, market) 轉成可讀字串"""
    pretty = []
    for i, (code, chg, vol, market) in enumerate(rows, 1):
        pretty.append(f"{i:>2}. {code:<5} 漲幅 {chg:>6.2f}%  量 {vol:,}")
    return pretty

def chunk_messages(title: str, lines: List[str]) -> List[str]:
    """
    依照 MAX_LINES_PER_MSG 與 MAX_CHARS_PER_MSG 把內容切成多段訊息。
    """
    pages = []
    buf = title
    cnt = 0
    for ln in lines:
        add = ("\n" if buf else "") + ln
        if (cnt + 1 > MAX_LINES_PER_MSG) or (len(buf) + len(add) > MAX_CHARS_PER_MSG):
            pages.append(buf)
            buf = ln
            cnt = 1
        else:
            buf += add
            cnt += 1
    if buf:
        pages.append(buf)
    return pages

# ===================== 入口：/daily-push =====================
@app.get("/daily-push")
def daily_push():
    try:
        # 可選：簡單保護
        if CRON_SECRET and request.args.get("secret") != CRON_SECRET:
            return "Forbidden", 403
        if not USER_ID:
            return "Missing env: LINE_USER_ID", 500

        # 1) 準備清單
        universe: List[Dict]
        manual_codes: List[str] = []
        if WATCHLIST_RAW.upper() == "ALL":
            universe = fetch_universe()
        else:
            # 逗號清單（可混合 .TWO），以「上市」預設；這樣仍會分群成「手動上市」
            manual_codes = [x.strip().upper() for x in WATCHLIST_RAW.split(",") if x.strip()]
            universe = [{"code": c.replace(".TW","").replace(".TWO",""), "market": "上市", "category": "股票"}
                        for c in manual_codes]

        groups = split_groups(universe)

        # 2) 各群篩選
        picked_listed = pick_rising(groups["listed"])
        picked_otc    = pick_rising(groups["otc"])
        picked_etf    = pick_rising(groups["etf"])

        # 3) 各群組裝 + 分頁
        today = dt.datetime.now(tz=dt.timezone(dt.timedelta(hours=8))).strftime("%Y-%m-%d")
        msgs: List[str] = []

        if picked_listed:
            title = f"【{today} 起漲清單】{EMOJI_LISTED} 上市（{len(picked_listed)} 檔）"
            msgs += chunk_messages(title, format_lines(picked_listed))
        if picked_otc:
            title = f"【{today} 起漲清單】{EMOJI_OTC} 上櫃（{len(picked_otc)} 檔）"
            msgs += chunk_messages(title, format_lines(picked_otc))
        if picked_etf:
            title = f"【{today} 起漲清單】{EMOJI_ETF} ETF（{len(picked_etf)} 檔）"
            msgs += chunk_messages(title, format_lines(picked_etf))

        if not msgs:
            msgs = [f"【{today} 起漲清單】目前無符合條件（或資料未更新）\n"
                    f"門檻：漲幅≥{MIN_CHANGE}%，量≥{MIN_VOLUME:,}"]

        # 4) 逐段推播（LINE 每則訊息上限 5000 字，這裡保守用 4500）
        for m in msgs:
            line_bot_api.push_message(USER_ID, TextSendMessage(text=m))

        return f"Sent {len(msgs)} message(s).", 200
    except Exception as e:
        app.logger.exception(e)
        return str(e), 500