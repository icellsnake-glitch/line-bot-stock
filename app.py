import os, re, csv, io, time, math, datetime as dt
from typing import List, Tuple, Dict

import requests
import pytz
from flask import Flask, request, abort, Response
from linebot import LineBotApi, WebhookHandler
from linebot.exceptions import InvalidSignatureError
from linebot.models import MessageEvent, TextMessage, TextSendMessage

# ---------------- Base ----------------
app = Flask(__name__)
TZ = pytz.timezone("Asia/Taipei")
TZ8 = dt.timezone(dt.timedelta(hours=8))

CHANNEL_ACCESS_TOKEN = os.getenv("LINE_CHANNEL_ACCESS_TOKEN", "").strip()
CHANNEL_SECRET       = os.getenv("LINE_CHANNEL_SECRET", "").strip()
USER_ID              = os.getenv("LINE_USER_ID", "").strip()
if not CHANNEL_ACCESS_TOKEN or not CHANNEL_SECRET:
    raise RuntimeError("Missing env: LINE_CHANNEL_ACCESS_TOKEN or LINE_CHANNEL_SECRET")

line_bot_api = LineBotApi(CHANNEL_ACCESS_TOKEN)
handler      = WebhookHandler(CHANNEL_SECRET)

MAX_LINES_PER_MSG = int(os.getenv("MAX_LINES_PER_MSG", "25"))
MAX_CHARS_PER_MSG = int(os.getenv("MAX_CHARS_PER_MSG", "1900"))
EMOJI_LISTED = os.getenv("EMOJI_LISTED", "📈")
EMOJI_OTC    = os.getenv("EMOJI_OTC", "✨")
EMOJI_ETF    = os.getenv("EMOJI_ETF", "📊")

# ---------------- Utils ----------------
_UA = {"User-Agent": "Mozilla/5.0 (StockBot)", "Referer": "https://mis.twse.com.tw/stock/index.jsp"}

def _clean_text(x: str) -> str:
    x = re.sub(r"\s+", " ", x or "").strip()
    x = re.sub(r"（.*?）|\(.*?\)", "", x).strip()
    return x

# ---------------- ① 全市場清單（下載路由） ----------------
def _fetch_twse_listed() -> List[Tuple[str, str]]:
    url = "https://isin.twse.com.tw/isin/C_public.jsp?strMode=2"
    r = requests.get(url, timeout=20, headers=_UA); r.raise_for_status()
    html = r.text
    rows = []
    for tr in re.findall(r"<tr[^>]*>(.*?)</tr>", html, flags=re.S|re.I):
        tds = re.findall(r"<td[^>]*>(.*?)</td>", tr, flags=re.S|re.I)
        if len(tds) < 5: continue
        raw = re.sub(r"<.*?>", "", tds[0]).strip()     # "2330 台積電"
        m = re.match(r"^(\d{4})\s+(.+)$", raw)
        if not m: continue
        code, name = m.group(1), _clean_text(m.group(2))
        cat = _clean_text(re.sub(r"<.*?>","",tds[3]))
        if "股票" not in cat: continue
        rows.append((f"{code}.TW", name))
    return rows

def _fetch_tpex_otc() -> List[Tuple[str, str]]:
    url = "https://isin.twse.com.tw/isin/C_public.jsp?strMode=4"
    r = requests.get(url, timeout=20, headers=_UA); r.raise_for_status()
    html = r.text
    rows = []
    for tr in re.findall(r"<tr[^>]*>(.*?)</tr>", html, flags=re.S|re.I):
        tds = re.findall(r"<td[^>]*>(.*?)</td>", tr, flags=re.S|re.I)
        if len(tds) < 5: continue
        raw = re.sub(r"<.*?>", "", tds[0]).strip()     # "6488 環球晶"
        m = re.match(r"^(\d{4})\s+(.+)$", raw)
        if not m: continue
        code, name = m.group(1), _clean_text(m.group(2))
        cat = _clean_text(re.sub(r"<.*?>","",tds[3]))
        if "股票" not in cat: continue
        rows.append((f"{code}.TWO", name))
    return rows

def _fetch_tw_etf() -> List[Tuple[str, str]]:
    def parse(url: str, suffix: str) -> List[Tuple[str, str]]:
        rr = requests.get(url, timeout=20, headers=_UA); rr.raise_for_status()
        h = rr.text
        out = []
        for tr in re.findall(r"<tr[^>]*>(.*?)</tr>", h, flags=re.S|re.I):
            tds = re.findall(r"<td[^>]*>(.*?)</td>", tr, flags=re.S|re.I)
            if len(tds) < 5: continue
            raw = re.sub(r"<.*?>", "", tds[0]).strip()  # 例如 "0050 元大台灣50"
            m = re.match(r"^(\w+)\s+(.+)$", raw)
            if not m: continue
            code, name = m.group(1), _clean_text(m.group(2))
            cat = _clean_text(re.sub(r"<.*?>","",tds[3]))
            if "ETF" not in cat: continue
            out.append((f"{code}.{suffix}", name))
        return out
    return parse("https://isin.twse.com.tw/isin/C_public.jsp?strMode=2","TW") + \
           parse("https://isin.twse.com.tw/isin/C_public.jsp?strMode=4","TWO")

def _to_csv_response(pairs: List[Tuple[str, str]], filename: str) -> Response:
    buf = io.StringIO(); w = csv.writer(buf)
    w.writerow(["代號","名稱"])
    for code, name in pairs: w.writerow([code, name])
    data = buf.getvalue().encode("utf-8-sig")
    return Response(data, headers={
        "Content-Type":"text/csv; charset=utf-8",
        "Content-Disposition": f'attachment; filename="{filename}"'
    })

@app.get("/symbols/listed.csv")
def symbols_listed(): return _to_csv_response(_fetch_twse_listed(), "listed.csv")

@app.get("/symbols/otc.csv")
def symbols_otc():    return _to_csv_response(_fetch_tpex_otc(), "otc.csv")

@app.get("/symbols/etf.csv")
def symbols_etf():    return _to_csv_response(_fetch_tw_etf(),   "etf.csv")

@app.get("/symbols/all_symbols.csv")
def symbols_all():
    rows = _fetch_twse_listed() + _fetch_tpex_otc() + _fetch_tw_etf()
    seen, uniq = set(), []
    for c,n in rows:
        if c not in seen: seen.add(c); uniq.append((c,n))
    return _to_csv_response(uniq, "all_symbols.csv")

# ---------------- ② 全市場載入（10 分鐘快取） ----------------
_SYMBOL_CACHE: Dict[str, Tuple[float, List[str]]] = {}
_ETF_SET_CACHE = None

def _cached(key: str, build_func, ttl_sec=600):
    now = time.time()
    rec = _SYMBOL_CACHE.get(key)
    if rec and now - rec[0] < ttl_sec: return rec[1]
    rows = build_func(); _SYMBOL_CACHE[key] = (now, rows); return rows

def get_etf_set() -> set[str]:
    global _ETF_SET_CACHE
    if _ETF_SET_CACHE is not None: return _ETF_SET_CACHE
    etf = _fetch_tw_etf()
    # 存 base 代號（不含 .TW/.TWO）
    _ETF_SET_CACHE = {c.split(".")[0] for c,_ in etf}
    return _ETF_SET_CACHE

def load_all_symbols() -> list[str]:
    def _build():
        listed = _fetch_twse_listed()
        otc    = _fetch_tpex_otc()
        etf    = _fetch_tw_etf()
        seen, out = set(), []
        for c,_ in listed + otc + etf:
            if c not in seen:
                seen.add(c); out.append(c)
        return out
    return _cached("ALL_SYMBOLS", _build, ttl_sec=600)

def parse_watchlist_from_env() -> list[str]:
    raw = (os.getenv("WATCHLIST") or "").strip()
    if not raw: return []
    if raw.upper() == "ALL": return load_all_symbols()
    out = []
    for tok in re.split(r"[,\s]+", raw):
        tok = tok.strip().upper()
        if not tok: continue
        if tok.endswith(".TW") or tok.endswith(".TWO"): out.append(tok)
        elif re.fullmatch(r"\d{4}", tok): out.append(f"{tok}.TW")
    return out

# ---------------- ③ MIS 批次即時（可切換 Yahoo） ----------------
def _to_exch(code: str) -> str:
    c = code.upper()
    if c.endswith(".TW"):  return f"tse_{c.replace('.TW','.tw')}"
    if c.endswith(".TWO"): return f"otc_{c.replace('.TWO','.tw')}"
    return f"tse_{c}.tw"

def fetch_mis_batch(codes: list[str]) -> dict[str, tuple[float, int]]:
    if not codes: return {}
    out: Dict[str, tuple[float, int]] = {}
    BATCH = 50
    for i in range(0, len(codes), BATCH):
        part = codes[i:i+BATCH]
        url  = "https://mis.twse.com.tw/stock/api/getStockInfo.jsp"
        params = {"ex_ch": "|".join(_to_exch(c) for c in part), "json":"1","delay":"0"}
        try:
            r = requests.get(url, params=params, headers=_UA, timeout=8)
            r.raise_for_status()
            j = r.json() or {}
            for d in j.get("msgArray", []):
                z = d.get("z") or d.get("pz"); y = d.get("y"); v = d.get("v"); c = d.get("c")
                if not (z and y and c): continue
                try:
                    zf, yf = float(z), float(y)
                    if yf <= 0: continue
                    chg = round((zf - yf) / yf * 100.0, 2)
                    vol = int(float(v or "0") * 1000)  # 千股→股
                except Exception:
                    continue
                suffix = ".TW" if d.get("ex") == "tse" else ".TWO"
                key = f"{c}{suffix}"
                out[key] = (chg, vol)
        except Exception:
            continue
        time.sleep(0.2)
    return out

# （簡化）Yahoo 後備來源：僅用日線估算（若需可保留）
def fetch_change_pct_and_volume_yahoo(code: str) -> tuple[float,int]:
    sym = code.upper()
    if not sym.endswith(".TW") and not sym.endswith(".TWO"):
        sym += ".TW"
    url = f"https://query1.finance.yahoo.com/v8/finance/chart/{sym}?range=5d&interval=1d"
    try:
        r = requests.get(url, timeout=8); r.raise_for_status()
        j = r.json().get("chart",{}).get("result",[])
        if not j: return 0.0, 0
        q = (j[0].get("indicators",{}).get("quote") or [{}])[0]
        closes = q.get("close") or []; vols = q.get("volume") or []
        if len(closes) < 2: return 0.0, 0
        last, prev = closes[-1], closes[-2]
        if last is None or prev in (None,0): return 0.0, 0
        chg = round((last - prev)/prev*100.0, 2); vol = int(vols[-1] or 0)
        return chg, vol
    except Exception:
        return 0.0, 0

# ---------------- ④ 掃描 & 推播 ----------------
def _format_rows(items: List[tuple[str,float,int]], top_k: int) -> List[str]:
    rows = []
    for i, (code, chg, vol) in enumerate(items[:top_k]):
        rows.append(f"{i+1}. {code}  漲幅 {chg:.2f}%  量 {vol:,}")
    return rows

def scan_block(codes: list[str], min_chg: float, min_vol: int, top_k: int, use_mis: bool) -> List[str]:
    if not codes: return []
    items = []
    if use_mis:
        data = fetch_mis_batch(codes)  # {'2330.TW': (chg, vol)}
        for c,(chg,vol) in data.items():
            if chg >= min_chg and vol >= min_vol:
                items.append((c, chg, vol))
    else:
        for c in codes:
            chg, vol = fetch_change_pct_and_volume_yahoo(c)
            if chg >= min_chg and vol >= min_vol:
                items.append((c, chg, vol))
    items.sort(key=lambda x: x[1], reverse=True)
    return _format_rows(items, top_k)

def send_chunks(title: str, rows: list[str], icon: str):
    if not rows: return
    page, count = f"{title}\n", 0
    for line in rows:
        if count >= MAX_LINES_PER_MSG or len(page)+len(line)+1 > MAX_CHARS_PER_MSG:
            line_bot_api.push_message(USER_ID, TextSendMessage(text=page.rstrip()))
            page, count = f"{title}\n", 0
        page += line + "\n"; count += 1
    line_bot_api.push_message(USER_ID, TextSendMessage(text=page.rstrip()))

# ---------------- ⑤ Webhook / Routes ----------------
@app.get("/")
def root(): return f"Bot running {dt.datetime.now(TZ8).strftime('%Y-%m-%d %H:%M:%S')}", 200

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
    text = (event.message.text or "").strip()
    if text in ("test","測試","測試推播"):
        status = daily_push_internal()
        line_bot_api.reply_message(event.reply_token, TextSendMessage(text=f"測試：{status}"))
    else:
        line_bot_api.reply_message(event.reply_token, TextSendMessage(text=text))

def daily_push_internal() -> str:
    if not USER_ID: return "Missing LINE_USER_ID"
    watchlist = parse_watchlist_from_env()
    if not watchlist:
        watchlist = ["2330.TW","2454.TW","2317.TW","2303.TW"]
    etf_set = get_etf_set()

    listed = [c for c in watchlist if c.endswith(".TW")  and (c.split(".")[0] not in etf_set)]
    otc    = [c for c in watchlist if c.endswith(".TWO") and (c.split(".")[0] not in etf_set)]
    etf    = []
    for c in watchlist:
        base = c.split(".")[0]
        if base in etf_set:
            etf.append(c if c.endswith((".TW",".TWO")) else f"{base}.TW")

    MIN_CHG = float(os.getenv("MIN_CHANGE_PCT", "0.5"))
    MIN_VOL = int(os.getenv("MIN_VOLUME", "100000"))
    TOP_K   = int(os.getenv("TOP_K", "30"))
    USE_MIS = os.getenv("USE_MIS", "1") in ("1","true","TRUE")

    now_s = dt.datetime.now(TZ8).strftime("%Y-%m-%d %H:%M")
    sent = False
    r1 = scan_block(listed, MIN_CHG, MIN_VOL, TOP_K, USE_MIS)
    if r1: send_chunks(f"【{now_s} 起漲清單】{EMOJI_LISTED} 上市", r1, EMOJI_LISTED); sent = True
    r2 = scan_block(otc,    MIN_CHG, MIN_VOL, TOP_K, USE_MIS)
    if r2: send_chunks(f"【{now_s} 起漲清單】{EMOJI_OTC} 上櫃",   r2, EMOJI_OTC);    sent = True
    r3 = scan_block(etf,    MIN_CHG, max(50000,MIN_VOL//2), TOP_K, USE_MIS)  # ETF 量門檻稍鬆
    if r3: send_chunks(f"【{now_s} 起漲清單】{EMOJI_ETF} ETF",     r3, EMOJI_ETF);    sent = True

    if not sent:
        line_bot_api.push_message(USER_ID, TextSendMessage(text=f"【{now_s} 起漲清單】\n尚無符合條件的個股（或資料未更新）"))
        return "no-picks"
    return "sent"

@app.get("/daily-push")
def daily_push_route():
    try:
        return ("OK: " + daily_push_internal(), 200)
    except Exception as e:
        app.logger.exception(e)
        return str(e), 500

# 本地啟動
if __name__ == "__main__":
    port = int(os.getenv("PORT","5000"))
    app.run(host="0.0.0.0", port=port)