import csv
from io import StringIO

def load_universe(max_scan: int = 500) -> list[str]:
    """
    取得全市場代號清單（上市 + 上櫃）。
    來源優先順序：
    1) UNIVERSE_CSV_URL（你自己提供的 CSV/Google Sheet 轉 CSV），第一欄放代號
    2) TWSE/TPEx 開放資料（自動抓）
    3) 若都失敗，回傳空清單
    備註：回傳為「2330」「2454」「8411.TWO」這種格式（不加 .TW 的先當上市）
    """
    # 0) 參數
    url_csv = os.getenv("UNIVERSE_CSV_URL", "").strip()
    max_scan = int(os.getenv("MAX_SCAN", str(max_scan)))

    # 1) 你自己給 CSV（最穩）
    if url_csv:
        try:
            r = requests.get(url_csv, timeout=15)
            r.raise_for_status()
            rows = list(csv.reader(StringIO(r.text)))
            codes = []
            for row in rows:
                if not row or not row[0]:
                    continue
                code = row[0].strip().upper()
                # 允許 .TWO / .TW / 純數字
                if code.endswith(".TW") or code.endswith(".TWO") or code.isdigit():
                    codes.append(code)
            return codes[:max_scan]
        except Exception:
            pass  # 失敗就往下用官方清單

    # 2) 官方清單（自動抓）—— 盡量兼容欄位名稱
    codes = []
    try:
        # TWSE 上市公司清單
        # 開放資料：t187ap03_L（上市公司名錄）
        j = requests.get("https://openapi.twse.com.tw/v1/opendata/t187ap03_L", timeout=15).json()
        for it in j:
            # 常見欄位：'Code' / '公司代號' / '證券代號'
            code = (it.get("Code") or it.get("公司代號") or it.get("證券代號") or "").strip()
            if code and code.isdigit():
                codes.append(code)  # 上市先不加後綴，後面 _yahoo_symbol 會補 .TW
    except Exception:
        pass

    try:
        # TPEx 上櫃公司清單
        # 常見開放 API：/openapi/v1/mopsfin_t187ap07_O（不同平台欄位名會異動，做保守解析）
        j2 = requests.get("https://www.tpex.org.tw/openapi/v1/mopsfin_t187ap07_O", timeout=15).json()
        for it in j2:
            code = (it.get("Code") or it.get("公司代號") or it.get("證券代號") or "").strip()
            if code and code.isdigit():
                codes.append(f"{code}.TWO")  # 上櫃直接標 .TWO
    except Exception:
        pass

    # 去重 & 截斷
    uniq = []
    seen = set()
    for c in codes:
        if c not in seen:
            uniq.append(c)
            seen.add(c)
        if len(uniq) >= max_scan:
            break
    return uniq