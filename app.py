import re
import os
import logging
import requests

# ---- 既有工具（保留你原本有的也行） ---------------------------------
CODE_RE = re.compile(r"\b(\d{4})\b")

def _clean_code(token: str) -> str | None:
    if not token:
        return None
    t = token.strip().upper()
    if t.endswith(".TW") or t.endswith(".TWO"):
        m = CODE_RE.search(t)
        return t if m else None
    m = CODE_RE.search(t)
    return m.group(1) if m else None

def _infer_market_suffix(url_or_name: str) -> str:
    s = (url_or_name or "").lower()
    if ("tpex" in s) or ("otc" in s) or ("two" in s):
        return ".TWO"
    return ".TW"

def _load_one_source(url: str) -> list[str]:
    codes: list[str] = []
    try:
        logging.info(f"[LIST] fetching: {url}")
        if url.startswith("http://") or url.startswith("https://"):
            resp = requests.get(url, timeout=12)
            resp.raise_for_status()
            text = resp.text
        else:
            with open(url, "r", encoding="utf-8") as f:
                text = f.read()

        suffix_guess = _infer_market_suffix(url)
        for raw in text.splitlines():
            row = raw.strip()
            if not row:
                continue
            if ("," in row) or ("\t" in row) or (";" in row):
                sep = "," if "," in row else ("\t" if "\t" in row else ";")
                first = row.split(sep)[0]
                code = _clean_code(first)
            else:
                code = _clean_code(row)
            if not code:
                continue
            codes.append(code if code.endswith((".TW", ".TWO")) else code + suffix_guess)
    except Exception as e:
        logging.info(f"[LIST] source error ({url}): {e}")
    return codes

# ---- 新增：官方 ISIN 頁面解析（不用 CSV） -----------------------------
TWSE_ISIN_URL = "https://isin.twse.com.tw/isin/C_public.jsp?strMode=2"  # 上市
TPEX_ISIN_URL = "https://isin.twse.com.tw/isin/C_public.jsp?strMode=4"  # 上櫃

_HTML_TD_RX = re.compile(r"<td[^>]*>(.*?)</td>", re.IGNORECASE | re.DOTALL)

def _fetch_isin_codes(url: str, suffix: str) -> list[str]:
    """
    從 TWSE/TPEx ISIN 公開頁抓代號：
    - 解析每列第一個 <td>，格式通常是「1101 台泥」
    - 抓到 4 碼就加上 .TW / .TWO
    """
    codes: list[str] = []
    try:
        logging.info(f"[ISIN] fetch: {url}")
        resp = requests.get(
            url,
            timeout=15,
            headers={
                "User-Agent": "Mozilla/5.0",
                "Accept-Language": "zh-TW,zh;q=0.9,en;q=0.8",
            },
        )
        resp.raise_for_status()
        html = resp.text

        # 每一列 <tr>…</tr>，取第一個 <td>
        for row in html.split("</tr>"):
            tds = _HTML_TD_RX.findall(row)
            if not tds:
                continue
            cell = tds[0].strip()
            # cell 內容常見：「1101 台泥」或「1101　台泥」
            m = CODE_RE.search(cell.replace("　", " "))  # 全形空白→半形
            if not m:
                continue
            code = m.group(1)
            # 過濾掉「股票類別」等非普通股（cell 內會包含關鍵字）
            # 若你要納入全部，也可移除此判斷
            if any(k in row for k in ["受益憑證", "認購", "認售", "牛證", "熊證", "特別股"]):
                continue
            codes.append(code + suffix)
    except Exception as e:
        logging.info(f"[ISIN] error: {e}")
    return codes

def _load_from_official_isin() -> list[str]:
    twse = _fetch_isin_codes(TWSE_ISIN_URL, ".TW")
    tpex = _fetch_isin_codes(TPEX_ISIN_URL, ".TWO")
    logging.info(f"[ISIN] TWSE: {len(twse)}, TPEX: {len(tpex)}")
    return twse + tpex

# ---- 總載入器：優先用 LIST_SOURCES，其次官方 ISIN，最後備援清單 --------
def load_all_tickers() -> list[str]:
    acc: list[str] = []

    # 1) 使用者提供的來源（CSV/純文字，多個以逗號分隔）
    srcs = [s.strip() for s in os.getenv("LIST_SOURCES", "").split(",") if s.strip()]
    for s in srcs:
        acc.extend(_load_one_source(s))

    # 2) 抓不到就直接爬官方 ISIN 頁（上市 + 上櫃）
    if not acc:
        acc = _load_from_official_isin()

    # 3) 再抓不到就用內建少量備援清單
    if not acc:
        logging.info("[LIST] using builtin fallback list")
        builtin_tw = ["2330", "2317", "2454", "2303", "2882", "2412", "2603", "1303", "2382", "2308"]
        builtin_two = ["6488", "5269", "4736", "3289"]
        acc = [c + ".TW" for c in builtin_tw] + [c + ".TWO" for c in builtin_two]

    # 去重、只留合法 Yahoo 代號
    uniq = []
    seen = set()
    for t in acc:
        tt = t.strip().upper()
        if (tt.endswith(".TW") or tt.endswith(".TWO")) and tt not in seen:
            seen.add(tt)
            uniq.append(tt)

    logging.info(f"[LIST] total tickers loaded: {len(uniq)}")
    return uniq