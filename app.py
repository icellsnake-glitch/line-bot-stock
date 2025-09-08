# === NEW: 自動抓全市場股票代號（上市/上櫃） ==========================
import re
import requests
from bs4 import BeautifulSoup

ISIN_URL = "https://isin.twse.com.tw/isin/C_public.jsp?strMode=2"

def fetch_all_tw_symbols() -> list[str]:
    """
    從證交所 ISIN 列表頁抓取所有『股票』代號。
    - 市場：上市、上櫃
    - 排除：ETF、受益憑證、權證、債券、存託憑證…等非『股票』
    回傳：像 ["2330", "2317", "2454", ...]，不帶 .TW / .TWO，後續由你既有 _yahoo_symbol() 轉接。
    """
    r = requests.get(ISIN_URL, timeout=20)
    # 頁面是 BIG5 編碼
    r.encoding = "big5"

    soup = BeautifulSoup(r.text, "html.parser")
    table = soup.find("table")
    if not table:
        return []

    symbols = []
    # 第一列是表頭，略過
    rows = table.find_all("tr")[1:]
    for tr in rows:
        tds = [td.get_text(strip=True) for td in tr.find_all("td")]
        if len(tds) < 7:
            continue

        code_and_name = tds[0]       # 例如 "2330　台積電"
        market = tds[3]              # 例如 "上市" / "上櫃"
        category = tds[4]            # 例如 "股票" / "ETF" / "受益憑證"...

        # 只收『股票』，市場要「上市」或「上櫃」
        if category != "股票":
            continue
        if market not in ("上市", "上櫃"):
            continue

        m = re.match(r"^([0-9A-Z]+)", code_and_name)
        if not m:
            continue
        code = m.group(1)
        # 代號必須都是數字（台股普通股），像 2330、2317；排除像 TDR 特殊代號
        if not re.match(r"^\d{3,4}$", code):
            continue

        symbols.append(code)

    # 去重
    symbols = sorted(set(symbols))
    return symbols

def get_watchlist_from_env() -> list[str]:
    """
    - WATCHLIST 省略或空：回 []
    - WATCHLIST=ALL：自動抓全市場股票（上市/上櫃）。
    - WATCHLIST=逗號清單：例如 '2330,2317,2454'
    """
    raw = os.getenv("WATCHLIST", "").strip()
    if not raw:
        return []

    if raw.upper() == "ALL":
        try:
            return fetch_all_tw_symbols()
        except Exception:
            # 抓全市場失敗時，不要讓服務掛掉；回空清單即可
            return []

    # 逗號清單
    items = [x.strip() for x in raw.split(",") if x.strip()]
    # 允許你手動放 .TWO 後綴；如果純數字就先保留，後續 _yahoo_symbol()會接手
    return items
# =====================================================================

# === 修改：使用 get_watchlist_from_env() 取得追蹤清單 ==================
@app.get("/daily-push")
def daily_push():
    try:
        if not USER_ID:
            return "Missing env: LINE_USER_ID", 500

        # <--- 原本這裡如果寫死清單或讀 WATCHLIST，請改成：
        watchlist = get_watchlist_from_env()

        # 如果 watchlist 是 ALL 但抓失敗，給個保底清單避免全空
        if not watchlist:
            watchlist = ["2330", "2317", "2454", "2303", "2882", "2603"]

        picked = pick_rising_stocks(
            watchlist=watchlist,
            min_change_pct=MIN_CHANGE,      # 你原本設定的門檻
            min_volume=MIN_VOLUME,          # 你原本設定的門檻
            top_k=MAX_SCORE                 # 你原本用來限制清單長度的變數名（若不同請對應）
        )

        today = dt.datetime.now(tz=dt.timezone(dt.timedelta(hours=8))).strftime("%Y-%m-%d")
        if picked:
            message = f"【{today} 起漲清單（共 {len(picked)} 檔）】\n" + "\n".join(picked)
        else:
            message = f"【{today} 起漲清單】\n尚無符合條件的個股（或資料未更新）"

        line_bot_api.push_message(USER_ID, TextSendMessage(text=message))
        return "Daily push sent!", 200
    except Exception as e:
        app.logger.exception(e)
        return str(e), 500