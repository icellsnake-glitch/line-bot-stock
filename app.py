import datetime as dt
import requests
from typing import List, Tuple

# ---------- 工具：抓 Yahoo Finance 當日變化（不需金鑰） ----------
def _yahoo_symbol(tw_code: str) -> str:
    """把台股代號轉成 Yahoo Finance 代號：2330 -> 2330.TW"""
    tw_code = tw_code.strip().upper()
    if tw_code.endswith(".TW") or tw_code.endswith(".TWO"):
        return tw_code
    # 上市：.TW，上櫃：.TWO（你也可為上櫃個股手動指定）
    return f"{tw_code}.TW"

def fetch_change_pct_and_volume(tw_code: str) -> Tuple[float, int]:
    """
    回傳：(當日漲跌幅%, 當日成交量)
    使用 1d/1m 的 intraday 資料；若市場未開或拿不到，退回最近的日線。
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

        if last_price is not None and last_close is not None:
            break

    if last_price is None or last_close is None or last_close == 0:
        # 拿不到資料時回 0,0，不入選
        return 0.0, 0

    change_pct = (last_price - last_close) / last_close * 100.0
    return round(change_pct, 2), last_volume

# ---------- 簡易「起漲」邏輯 ----------
def pick_rising_stocks(watchlist: List[str],
                       min_change_pct: float = 2.0,
                       min_volume: int = 1_000_000,
                       top_k: int = 10) -> List[str]:
    """
    以「漲幅 >= min_change_pct 且 成交量 >= min_volume」過濾，
    依漲幅排序後取前 top_k。
    """
    rows = []
    for code in watchlist:
        try:
            chg, vol = fetch_change_pct_and_volume(code)
            rows.append((code, chg, vol))
        except Exception:
            # 單一代號失敗時略過；不中斷整體流程
            continue

    rows = [r for r in rows if r[1] >= min_change_pct and r[2] >= min_volume]
    rows.sort(key=lambda x: x[1], reverse=True)

    # 格式化輸出
    pretty = [f"{i+1}. {code}  漲幅 {chg:.2f}%  量 {vol:,}"
              for i, (code, chg, vol) in enumerate(rows[:top_k])]
    return pretty
    
@app.get("/daily-push")
def daily_push():
    try:
        if not USER_ID:
            return "Missing env: LINE_USER_ID", 500

        # 你的追蹤清單（先放常見權值與熱門股；之後你可改成讀檔或資料庫）
        watchlist = [
            "2330", "2454", "2317", "2303", "2603", "2882", "2412",
            "1303", "1101", "5871", "1605", "2377", "3481", "3661",
            # 上櫃請加 .TWO，例如「某些上櫃代號.TWO」
        ]

        picked = pick_rising_stocks(
            watchlist=watchlist,
            min_change_pct=2.0,     # 起漲門檻：漲幅 >= 2%
            min_volume=1_000_000,   # 成交量門檻（股）
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