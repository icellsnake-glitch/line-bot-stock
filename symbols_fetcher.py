import io
import re
import time
import json
import pandas as pd
import requests
from typing import Tuple

# ---- 共用：簡單、安全重試 + UA 標頭（避免被擋） ----
SESSION = requests.Session()
SESSION.headers.update({
    "User-Agent": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
                  "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"
})

def _get(url: str, *, as_text=False, verify=False, retry=3, timeout=20) -> requests.Response:
    last = None
    for _ in range(retry):
        try:
            r = SESSION.get(url, timeout=timeout, verify=verify)
            r.raise_for_status()
            return r.text if as_text else r
        except Exception as e:
            last = e
            time.sleep(1.2)
    raise last

# ---- 來源 A：TWSE 列表（含 ETF、上市）— 由 ISIN 頁面讀表 ----
# 備註：有些環境對 TWSE/TPEx 的 TLS 會驗證失敗，因此預設 verify=False
TWSE_ISIN_URL = "https://isin.twse.com.tw/isin/C_public.jsp?strMode=2"

# ---- 來源 B：TPEx（上櫃） ----
TPEX_ISIN_URL = "https://isin.tpex.org.tw/isin/C_public.jsp?strMode=4"

def _parse_isin_html_table(html: str) -> pd.DataFrame:
    # 直接讓 pandas 幫我們把表格抓出來
    tables = pd.read_html(io.StringIO(html), header=0)
    if not tables:
        return pd.DataFrame(columns=["有價證券代號及名稱","市場別","產業別"])
    # 第一張表就是我們要的
    df = tables[0].copy()
    # 欄位在不同日子可能有些差異，這裡做一次「常見欄位名」對齊
    # 只保留我們需要的三欄
    for col in list(df.columns):
        if "代號" in col and "名稱" in col:
            code_name_col = col
            break
    else:
        # 找不到就給空表
        return pd.DataFrame(columns=["有價證券代號及名稱","市場別","產業別"])

    if "市場別" in df.columns:
        mkt_col = "市場別"
    elif "上市/上櫃" in df.columns:
        mkt_col = "上市/上櫃"
    else:
        mkt_col = None

    ind_col = "產業別" if "產業別" in df.columns else None

    keep = [code_name_col]
    if mkt_col: keep.append(mkt_col)
    if ind_col: keep.append(ind_col)

    return df[keep].rename(columns={code_name_col: "code_name",
                                    mkt_col if mkt_col else "市場別": "market",
                                    ind_col if ind_col else "產業別": "industry"})


def _split_code_name(val: str) -> Tuple[str, str]:
    # 例：「2330　臺積電」或「0050　元大台灣50」
    if not isinstance(val, str):
        return "", ""
    s = re.sub(r"\s+", " ", val).strip()
    m = re.match(r"^([A-Z0-9]{3,6})\s+(.+)$", s)
    if m:
        return m.group(1), m.group(2)
    return "", s


def fetch_twse_listed_and_etf() -> pd.DataFrame:
    html = _get(TWSE_ISIN_URL, as_text=True, verify=False)
    df = _parse_isin_html_table(html)
    if df.empty:
        return df

    df[["code", "name"]] = df["code_name"].apply(lambda x: pd.Series(_split_code_name(x)))
    df.drop(columns=["code_name"], inplace=True)

    # 判斷 ETF：市場別或產業別常會含有「ETF」字樣，或名稱含 ETF
    def _classify(row):
        txt = " ".join([str(row.get("market","")), str(row.get("industry","")), str(row.get("name",""))])
        if "ETF" in txt.upper():
            return "ETF"
        return "LISTED"

    df["board"] = df.apply(_classify, axis=1)
    df = df[["code","name","board"]].dropna().drop_duplicates()
    df = df[df["code"].str.len() > 0]
    return df


def fetch_tpex_otc() -> pd.DataFrame:
    html = _get(TPEX_ISIN_URL, as_text=True, verify=False)
    df = _parse_isin_html_table(html)
    if df.empty:
        return df
    df[["code", "name"]] = df["code_name"].apply(lambda x: pd.Series(_split_code_name(x)))
    df.drop(columns=["code_name"], inplace=True)
    df["board"] = "OTC"
    df = df[["code","name","board"]].dropna().drop_duplicates()
    df = df[df["code"].str.len() > 0]
    # 也把上櫃 ETF 轉標籤（少數）
    df.loc[df["name"].str.upper().str.contains("ETF", na=False), "board"] = "ETF"
    return df


def build_all_symbols() -> pd.DataFrame:
    a = fetch_twse_listed_and_etf()
    b = fetch_tpex_otc()

    all_df = pd.concat([a, b], ignore_index=True)
    # 統一代號格式（.TW / .TWO 交給行情層處理，清單只放純代碼）
    all_df["code"] = all_df["code"].str.upper().str.strip()
    # 排序：上市、上櫃、ETF；其內再依 code 排
    cat_order = {"LISTED":0, "OTC":1, "ETF":2}
    all_df["__k"] = all_df["board"].map(cat_order).fillna(9)
    all_df.sort_values(["__k","code"], inplace=True)
    all_df.drop(columns="__k", inplace=True)

    # 去除常見雜訊（如「*」或備註）
    all_df["name"] = all_df["name"].str.replace(r"\s*\(.*?\)\s*$", "", regex=True).str.strip()
    return all_df


def save_all_symbols_csv(path: str = "all_taiwan_stocks.csv") -> int:
    df = build_all_symbols()
    df.to_csv(path, index=False, encoding="utf-8-sig")
    return len(df)
