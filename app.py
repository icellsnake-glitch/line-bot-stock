import os, re, math, io, csv, time, random, threading, datetime as dt
from typing import List, Tuple, Optional, Dict

import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

import pytz
import schedule
from flask import Flask, request, abort, jsonify

from linebot import LineBotApi, WebhookHandler
from linebot.exceptions import InvalidSignatureError
from linebot.models import MessageEvent, TextMessage, TextSendMessage

# ------------------ 基本設定 ------------------
app = Flask(__name__)
TZ  = pytz.timezone("Asia/Taipei")
TZ8 = dt.timezone(dt.timedelta(hours=8))

# LINE 金鑰
CHANNEL_ACCESS_TOKEN = os.getenv("LINE_CHANNEL_ACCESS_TOKEN", "").strip()
CHANNEL_SECRET       = os.getenv("LINE_CHANNEL_SECRET", "").strip()
USER_ID              = os.getenv("LINE_USER_ID", "").strip()   # 個人 userId（測推播）

if not CHANNEL_ACCESS_TOKEN or not CHANNEL_SECRET:
    raise RuntimeError("Missing env: LINE_CHANNEL_ACCESS_TOKEN or LINE_CHANNEL_SECRET")

line_bot_api = LineBotApi(CHANNEL_ACCESS_TOKEN)
handler      = WebhookHandler(CHANNEL_SECRET)

# 訊息切頁限制
MAX_LINES_PER_MSG = int(os.getenv("MAX_LINES_PER_MSG", "25"))
MAX_CHARS_PER_MSG = int(os.getenv("MAX_CHARS_PER_MSG", "1800"))

# emoji（可改）
EMOJI_LISTED = os.getenv("EMOJI_LISTED", "📈")
EMOJI_OTC    = os.getenv("EMOJI_OTC",    "✨")
EMOJI_ETF    = os.getenv("EMOJI_ETF",    "📦")

WATCHLIST_ENV = os.getenv("WATCHLIST", "2330,2317,2454,2303,2412").strip()
LIST_SOURCES  = os.getenv("LIST_SOURCES", "").strip()   # 逗號分隔 CSV/JSON（第一欄/欄位名=代號）

# ------------------ HTTP Session with retry ------------------
_session = None
def _get_session():
    global _session
    if _session is None:
        s = requests.Session()
        retries = Retry(total=3, backoff_factor=0.6, status_forcelist=[429, 500, 502, 503, 504])
        s.mount("https://", HTTPAdapter(max_retries=retries))
        s.headers.update({
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124 Safari/537.36"
        })
        _session = s
    return _session

def http_get(url, timeout=12):
    s = _get_session()
    r = s.get(url, timeout=timeout)
    r.raise_for_status()
    if r.headers.get("Content-Type","").startswith("application/json") and not r.content:
        time.sleep(0.5 + random.random()*0.5)
        r = s.get(url, timeout=timeout)
        r.raise_for_status()
    return r

# ------------------ 代號/市場 ------------------
def normalize_code(token: str) -> Optional[str]:
    t = token.strip().upper()
    if not t: return None
    if t.endswith(".TW") or t.endswith(".TWO"):
        core = t.split(".")[0]
        return core if re.fullmatch(r"\d{4}", core) else None
    return t if re.fullmatch(r"\d{4}", t) else None

def _yahoo_symbol(code: str) -> str:
    u = code.upper()
    if u.endswith(".TW") or u.endswith(".TWO"):
        return u
    return u + ".TW"

def classify_code(code: str, etf_set: set) -> str:
    u = code.upper()
    if u in etf_set: return "ETF"
    if u.endswith(".TWO"): return "OTC"
    return "LISTED"

# ------------------ 全市場清單 ------------------
def load_watchlist() -> list[str]:
    """
    WATCHLIST=ALL：抓上市/上櫃（並可合併 LIST_SOURCES）
    ELSE：逗號清單（可含 .TW/.TWO）
    """
    wl = os.getenv("WATCHLIST", "").strip()
    if wl.upper() != "ALL":
        out = []
        for c in wl.split(","):
            c = c.strip().upper()
            if not c: continue
            nc = normalize_code(c)
            if nc:
                out.append(nc if nc.endswith((".TW",".TWO")) else (nc + ".TW"))
        return out or ["2330.TW"]

    codes = []

    # TWSE 上市
    try:
        twse_url = "https://openapi.twse.com.tw/v1/opendata/t187ap03_L"
        r = http_get(twse_url); arr = r.json()
        for row in arr:
            code = (row.get("公司代號") or "").strip()
            if code.isdigit():
                codes.append(code + ".TW")
    except Exception as e:
        app.logger.warning(f"TWSE list fetch fail: {e}")

    # TPEx 上櫃（端點偶爾變動，失敗時請改自備清單）
    try:
        tpex_url = "https://www.tpex.org.tw/webapi/api/v1/tpex_main_spec/otc_company_info"
        r = http_get(tpex_url); arr = r.json().get("data", [])
        for row in arr:
            code = str(row.get("SecuritiesCompanyCode") or row.get("股票代號") or "").strip()
            if code.isdigit():
                codes.append(code + ".TWO")
    except Exception as e:
        app.logger.warning(f"TPEx list fetch fail: {e}")

    # 合併自定清單
    if LIST_SOURCES:
        for url in [u.strip() for u in LIST_SOURCES.split(",") if u.strip()]:
            try:
                rr = http_get(url)
                ctype = (rr.headers.get("Content-Type") or "").lower()
                if "json" in ctype:
                    arr = rr.json()
                    for row in arr:
                        c = str(row.get("code") or row.get("代號") or row.get("公司代號") or "").strip().upper()
                        c = normalize_code(c)
                        if c: codes.append(c if c.endswith((".TW",".TWO")) else (c+".TW"))
                else:
                    for line in rr.text.splitlines():
                        first = line.split(",")[0].strip().upper()
                        c = normalize_code(first)
                        if c: codes.append(c if c.endswith((".TW",".TWO")) else (c+".TW"))
            except Exception as e:
                app.logger.warning(f"LIST_SOURCES fetch fail: {url} -> {e}")

    # 去重
    uniq, seen = [], set()
    for c in codes:
        u = c.upper()
        if (u.endswith(".TW") or u.endswith(".TWO")) and u not in seen:
            seen.add(u); uniq.append(u)

    return uniq or ["2330.TW", "2317.TW", "2454.TW", "2303.TW", "2412.TW"]

# ------------------ ETF 清單 ------------------
def load_etf_set() -> set[str]:
    s = set()
    codes = os.getenv("ETF_CODES","").strip()
    if codes:
        for c in codes.split(","):
            c = c.strip().upper()
            if c and c.isdigit(): s.add(c + ".TW")

    url = os.getenv("ETF_LIST_URL","").strip()
    if url:
        try:
            rr = http_get(url)
            if "json" in (rr.headers.get("Content-Type") or "").lower():
                arr = rr.json()
                for row in arr:
                    c = str(row.get("code") or row.get("代號") or "").strip()
                    if c.isdigit(): s.add(c + ".TW")
            else:
                for line in rr.text.splitlines():
                    c = line.split(",")[0].strip()
                    if c.isdigit(): s.add(c + ".TW")
        except Exception as e:
            app.logger.warning(f"ETF list fetch fail: {e}")
    return s

# ------------------ Yahoo 取價量（日/分） ------------------
def fetch_change_pct_and_volume(tw_code: str) -> Tuple[float, int]:
    """
    回傳：(當日漲跌幅%, 當日成交量)
    1d/1m 取不到→退 5d/1d；失敗回 (0,0)
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
        r = http_get(url); j = r.json()
        result = (j.get("chart", {}) or {}).get("result") or []
        if not result: continue
        node = result[0]
        quote = (node.get("indicators", {}) or {}).get("quote") or [{}]
        q = quote[0]
        closes = q.get("close") or []
        volumes = q.get("volume") or []

        for i in range(len(closes)-1, -1, -1):
            c = closes[i]; v = volumes[i] if i < len(volumes) else 0
            if c is not None:
                last_price = float(c); last_volume = int(v or 0); break
        for i in range(len(closes)-2, -1, -1):
            c = closes[i]
            if c is not None:
                last_close = float(c); break

        if last_price is not None and last_close not in (None, 0):
            break

    if last_price is None or last_close in (None, 0):
        return 0.0, 0

    chg = (last_price - last_close) / last_close * 100.0
    return round(chg, 2), last_volume

def fetch_daily_bars(symbol: str, days: int = 60):
    """取最近日K(<=days)：list[{o,h,l,c,v}]"""
    url = f"https://query1.finance.yahoo.com/v8/finance/chart/{symbol}?range={days}d&interval=1d"
    r = http_get(url); j = r.json().get("chart",{}).get("result",[])
    if not j: return []
    r0 = j[0]; q = (r0.get("indicators",{}).get("quote") or [{}])[0]
    highs, lows, opens, closes, vols = q.get("high") or [], q.get("low") or [], q.get("open") or [], q.get("close") or [], q.get("volume") or []
    bars = []
    for i in range(min(len(closes), len(vols))):
        if closes[i] is None: continue
        bars.append(dict(
            o = opens[i]  or math.nan,
            h = highs[i]  or math.nan,
            l = lows[i]   or math.nan,
            c = closes[i] or math.nan,
            v = int(vols[i] or 0),
        ))
    return bars

def compute_volume_features(bars: list, n: int):
    """
    回傳：(today_close, today_vol, vol_ma_n(不含今天), yday_vol, today_high, yday_high)
    """
    if not bars: return math.nan,0,0,0,math.nan,math.nan
    today = bars[-1]
    yday  = bars[-2] if len(bars)>=2 else dict(v=0,h=math.nan,c=math.nan)
    hist = bars[-(n+1):-1] if len(bars) > 1 else []
    vols = [b["v"] for b in hist if b["v"]]
    vol_ma_n = int(sum(vols)/len(vols)) if vols else 0
    return (today["c"], today["v"], vol_ma_n, int(yday.get("v",0)),
            today.get("h", math.nan), yday.get("h", math.nan))

# ========= Technicals（不依賴 pandas/TA-Lib） =========
def sma(arr, n):
    out, s, q = [], 0.0, []
    for x in arr:
        q.append(x); s += x
        if len(q) > n: s -= q.pop(0)
        out.append(s/len(q) if q else math.nan)
    return out

def ema(arr, n):
    out, k, prev = [], 2/(n+1), None
    for x in arr:
        prev = x if prev is None else (x - prev)*k + prev
        out.append(prev)
    return out

def rsi(closes, period=14):
    gains, losses, out, prev = 0.0, 0.0, [], None
    for i, c in enumerate(closes):
        if prev is None:
            out.append(math.nan); prev = c; continue
        change = c - prev
        up = max(change, 0.0); down = -min(change, 0.0)
        if i <= period:
            gains += up; losses += down
            rs = (gains/period)/(losses/period) if i == period and losses>0 else None
            out.append(100 - 100/(1+rs) if rs is not None else math.nan)
        else:
            gains = (gains*(period-1) + up)/period
            losses = (losses*(period-1) + down)/period
            rs = (gains/losses) if losses>0 else math.inf
            out.append(100 - 100/(1+rs))
        prev = c
    return out

def macd(closes, fast=12, slow=26, signal=9):
    ema_fast = ema(closes, fast)
    ema_slow = ema(closes, slow)
    macd_line = [ (f - s) if (f is not None and s is not None) else math.nan
                  for f, s in zip(ema_fast, ema_slow)]
    signal_line = ema([x if not math.isnan(x) else 0.0 for x in macd_line], signal)
    hist = [ (m - s) for m, s in zip(macd_line, signal_line)]
    return macd_line, signal_line, hist

def stochastic(highs, lows, closes, k_period=9, d_period=3):
    k_list = []
    for i in range(len(closes)):
        left = max(0, i-k_period+1)
        hh = max(highs[left:i+1])
        ll = min(lows[left:i+1])
        k_list.append(50.0 if hh==ll else (closes[i]-ll)/(hh-ll)*100.0)
    d_list = sma(k_list, d_period)
    return k_list, d_list

def bollinger(closes, n=20, mult=2.0):
    ma = sma(closes, n)
    stds, window = [], []
    for c in closes:
        window.append(c)
        if len(window) > n: window.pop(0)
        if len(window) < 2:
            stds.append(math.nan)
        else:
            m = sum(window)/len(window)
            var = sum((x-m)*(x-m) for x in window)/len(window)
            stds.append(var**0.5)
    upper = [m + mult*s if (not math.isnan(m) and not math.isnan(s)) else math.nan
             for m, s in zip(ma, stds)]
    lower = [m - mult*s if (not math.isnan(m) and not math.isnan(s)) else math.nan
             for m, s in zip(ma, stds)]
    return ma, upper, lower

def atr(highs, lows, closes, period=14):
    trs, prev_close = [], None
    for i in range(len(closes)):
        h, l, c = highs[i], lows[i], closes[i]
        tr = (h - l) if prev_close is None else max(h-l, abs(h-prev_close), abs(l-prev_close))
        trs.append(tr); prev_close = c
    return sma(trs, period)

def donchian(highs, lows, n=20):
    up, dn = [], []
    for i in range(len(highs)):
        left = max(0, i-n+1)
        up.append(max(highs[left:i+1]))
        dn.append(min(lows[left:i+1]))
    return up, dn

def bars_to_arrays(bars):
    o = [b["o"] for b in bars]
    h = [b["h"] for b in bars]
    l = [b["l"] for b in bars]
    c = [b["c"] for b in bars]
    v = [b["v"] for b in bars]
    return o,h,l,c,v

# —— 基本門檻（pre/live）＋ TA 參數 —— 
def _get_bool(name: str, default: bool) -> bool:
    v = os.getenv(name, "")
    return default if v=="" else v.strip().lower() in ("1","true","yes","y","on")

def _profile_from_query_or_now(req) -> str:
    p = (req.args.get("profile") or "").strip().lower()
    if p in ("pre", "live"): return p
    now = dt.datetime.now(TZ8).time()
    nine, nine30 = dt.time(9,0), dt.time(9,30)
    return "pre" if (now >= nine and now < nine30) else "live"

def load_thresholds(profile: str) -> Dict:
    prefix = "PRE_" if profile == "pre" else "LIVE_"
    return dict(
        MIN_CHANGE_PCT        = float(os.getenv(prefix+"MIN_CHANGE_PCT", "0.5")),
        MIN_VOLUME            = int(os.getenv(prefix+"MIN_VOLUME", "100000")),
        USE_TODAY_VOL_SPIKE   = _get_bool(prefix+"USE_TODAY_VOL_SPIKE", False),
        VOL_MA_N              = int(os.getenv(prefix+"VOL_MA_N", "5")),
        VOL_SPIKE_RATIO       = float(os.getenv(prefix+"VOL_SPIKE_RATIO", "2.0")),
        USE_YDAY_VOL_BREAKOUT = _get_bool(prefix+"USE_YDAY_VOL_BREAKOUT", False),
        YDAY_BREAKOUT_RATIO   = float(os.getenv(prefix+"YDAY_BREAKOUT_RATIO", "1.5")),
    )

def build_ta_profile(profile:str) -> dict:
    th = load_thresholds(profile)
    def _f(name, default): return float(os.getenv(name, str(default)))
    def _i(name, default): return int(os.getenv(name, str(default)))

    ta = {
        "USE_PRICE_ABOVE_SMA": _get_bool("USE_PRICE_ABOVE_SMA", False),
        "SMA_N": _i("SMA_N", 20),
        "USE_PRICE_ABOVE_EMA": _get_bool("USE_PRICE_ABOVE_EMA", False),
        "EMA_N": _i("EMA_N", 20),

        "USE_RSI_RANGE": _get_bool("USE_RSI_RANGE", False),
        "RSI_N": _i("RSI_N", 14),
        "RSI_MIN": _f("RSI_MIN", 50.0),
        "RSI_MAX": _f("RSI_MAX", 80.0),

        "USE_MACD_POSITIVE": _get_bool("USE_MACD_POSITIVE", False),
        "USE_MACD_GOLDEN":   _get_bool("USE_MACD_GOLDEN",   False),

        "USE_STOCH": _get_bool("USE_STOCH", False),
        "STO_K": _i("STO_K", 9),
        "STO_D": _i("STO_D", 3),

        "USE_BBANDS_BREAK": _get_bool("USE_BBANDS_BREAK", False),
        "BB_N": _i("BB_N", 20),
        "BB_MULT": _f("BB_MULT", 2.0),

        "USE_ATR_PCT": _get_bool("USE_ATR_PCT", False),
        "ATR_N": _i("ATR_N", 14),
        "ATR_MIN_PCT": _f("ATR_MIN_PCT", 1.0),

        "USE_DONCHIAN": _get_bool("USE_DONCHIAN", False),
        "DON_N": _i("DON_N", 20),
    }
    ta.update(th)
    return ta

def ta_pass(bars, ta:dict) -> bool:
    if not bars or len(bars)<3: 
        return False
    o,h,l,c,v = bars_to_arrays(bars)

    # SMA / EMA
    if ta["USE_PRICE_ABOVE_SMA"]:
        sm = sma(c, ta["SMA_N"])
        if math.isnan(sm[-1]) or c[-1] <= sm[-1]: return False
    if ta["USE_PRICE_ABOVE_EMA"]:
        em = ema(c, ta["EMA_N"])
        if math.isnan(em[-1]) or c[-1] <= em[-1]: return False

    # RSI
    if ta["USE_RSI_RANGE"]:
        r = rsi(c, ta["RSI_N"]); rv = r[-1]
        if math.isnan(rv) or rv < ta["RSI_MIN"] or rv > ta["RSI_MAX"]: return False

    # MACD
    if ta["USE_MACD_POSITIVE"] or ta["USE_MACD_GOLDEN"]:
        m, s, hist = macd(c)
        mv, sv, hv = m[-1], s[-1], hist[-1]
        if ta["USE_MACD_POSITIVE"] and not (mv > 0 and hv > 0): return False
        if ta["USE_MACD_GOLDEN"]   and not (mv > sv):           return False

    # KD
    if ta["USE_STOCH"]:
        k, d_ = stochastic(h, l, c, ta["STO_K"], ta["STO_D"])
        kv, dv = k[-1], d_[-1]
        if math.isnan(kv) or math.isnan(dv) or not (kv > dv and kv > 50): return False

    # 布林
    if ta["USE_BBANDS_BREAK"]:
        mid, up, lo = bollinger(c, ta["BB_N"], ta["BB_MULT"])
        if math.isnan(mid[-1]) or math.isnan(up[-1]): return False
        if not (c[-1] >= mid[-1] and h[-1] >= up[-1]): return False

    # ATR%
    if ta["USE_ATR_PCT"]:
        a = atr(h, l, c, ta["ATR_N"])
        if math.isnan(a[-1]) or c[-1]==0: return False
        if (a[-1]/c[-1]*100.0) < ta["ATR_MIN_PCT"]: return False

    # Donchian 突破
    if ta["USE_DONCHIAN"]:
        up, dn = donchian(h,l, ta["DON_N"])
        if h[-1] < up[-1]: return False

    return True

# ------------------ 選股 v3（含量能/突破 + TA，並分群） ------------------
def pick_rising_stocks_v3(
    watchlist: list[str],
    th: dict,
    etf_set: set,
) -> dict:
    out = {"LISTED": [], "OTC": [], "ETF": []}
    for code in watchlist:
        try:
            symbol = _yahoo_symbol(code)
            bars = fetch_daily_bars(symbol, days=max(th["VOL_MA_N"]+30, 60))
            if not bars: continue

            today_c, today_v, vol_ma_n, yday_v, today_h, yday_h = compute_volume_features(bars, th["VOL_MA_N"])
            if math.isnan(today_c): continue
            prev_c = bars[-2]["c"] if len(bars)>=2 and not math.isnan(bars[-2]["c"]) else None
            if not prev_c or prev_c==0: continue
            chg = round((today_c - prev_c) / prev_c * 100.0, 2)

            # 基本價量
            if chg < th["MIN_CHANGE_PCT"] or today_v < th["MIN_VOLUME"]: continue
            if th["USE_TODAY_VOL_SPIKE"] and vol_ma_n>0 and (today_v < th["VOL_SPIKE_RATIO"]*vol_ma_n): continue
            if th["USE_YDAY_VOL_BREAKOUT"] and vol_ma_n>0:
                y_break = yday_v >= th["YDAY_BREAKOUT_RATIO"]*vol_ma_n
                h_break = (not math.isnan(today_h) and not math.isnan(yday_h) and today_h > yday_h)
                if not (y_break or h_break): continue

            # TA 關卡
            if not ta_pass(bars, th): continue

            bucket = classify_code(code, etf_set)
            out[bucket].append((code, chg, today_v))
        except Exception:
            continue

    for k in out:
        out[k].sort(key=lambda x: x[1], reverse=True)
    return out

def format_grouped_messages(grouped: dict, title_date: str,
                            max_lines: int, max_chars: int,
                            emoji_listed: str, emoji_otc: str, emoji_etf: str) -> List[str]:
    def fmt_rows(rows):
        return [f"{i+1}. {c}  漲幅 {chg:.2f}%  量 {vol:,}" for i,(c,chg,vol) in enumerate(rows)]
    pages: List[str] = []
    for tag, emoji, label in (("LISTED",emoji_listed,"上市"), ("OTC",emoji_otc,"上櫃"), ("ETF",emoji_etf,"ETF")):
        rows = fmt_rows(grouped.get(tag,[]))
        if not rows: continue
        header = f"【{title_date} 起漲清單】{emoji} {label}"
        page, count = header + "\n", 0
        for line in rows:
            if (count >= max_lines) or (len(page) + len(line) + 1 > max_chars):
                pages.append(page.rstrip())
                page, count = header + "\n", 0
            page += line + "\n"; count += 1
        pages.append(page.rstrip())
    return pages

# ------------------ 推播主流程（整合 TA） ------------------
def do_scan_and_push(profile: str) -> str:
    th = build_ta_profile(profile)        # 基本 + TA 參數
    watchlist = load_watchlist()
    etf_set   = load_etf_set()

    grouped = pick_rising_stocks_v3(watchlist, th, etf_set)
    now_s = dt.datetime.now(TZ).strftime("%Y-%m-%d %H:%M")

    msgs = format_grouped_messages(
        grouped, now_s,
        max_lines=MAX_LINES_PER_MSG, max_chars=MAX_CHARS_PER_MSG,
        emoji_listed=EMOJI_LISTED, emoji_otc=EMOJI_OTC, emoji_etf=EMOJI_ETF
    )

    if not USER_ID:
        return "Missing LINE_USER_ID"

    if not msgs:
        line_bot_api.push_message(USER_ID, TextSendMessage(text=f"【{now_s} 起漲清單】\n尚無符合條件的個股（或資料未更新）"))
        return "no-picks"

    for m in msgs:
        line_bot_api.push_message(USER_ID, TextSendMessage(text=m))
    return f"sent {len(msgs)} pages"

# ------------------ 盤中每5分鐘排程（平日 09:10–13:30） ------------------
def is_trading_now() -> bool:
    now = dt.datetime.now(TZ)
    if now.weekday() > 4: return False
    hhmm = now.hour * 100 + now.minute
    return 910 <= hhmm <= 1330

def job_every_5min():
    if is_trading_now():
        try:
            do_scan_and_push("live")
        except Exception as e:
            app.logger.exception(e)

def scheduler_thread():
    schedule.every(5).minutes.do(job_every_5min)
    while True:
        schedule.run_pending()
        time.sleep(1)

threading.Thread(target=scheduler_thread, daemon=True).start()

# ------------------ 路由 ------------------
@app.get("/")
def root():
    now = dt.datetime.now(TZ8).strftime("%Y-%m-%d %H:%M:%S")
    return f"Bot is running. {now}", 200

@app.get("/ping")
def ping():
    return "pong", 200

@app.get("/wake")
def wake():
    return "awake", 200

@app.get("/daily-push")
def daily_push_route():
    try:
        profile = _profile_from_query_or_now(request)  # ?profile=pre|live；否則依時間
        status  = do_scan_and_push(profile)
        return f"OK: {status}", 200
    except Exception as e:
        app.logger.exception(e)
        return str(e), 500

@app.get("/debug")
def debug_one():
    code = (request.args.get("code") or "").strip()
    if not code:
        return "usage: /debug?code=2330", 400
    try:
        chg, vol = fetch_change_pct_and_volume(code)
        return f"{code} -> change={chg}%, volume={vol}", 200
    except Exception as e:
        app.logger.exception(e)
        return f"error: {e}", 500

@app.get("/debug/watchlist")
def debug_watchlist():
    try:
        codes = load_watchlist()
        return {"count": len(codes), "sample": codes[:20]}, 200
    except Exception as e:
        app.logger.exception(e)
        return {"error": str(e)}, 500

@app.get("/debug/etf")
def debug_etf():
    try:
        etfs = sorted(list(load_etf_set()))
        return {"count": len(etfs), "sample": etfs[:20]}, 200
    except Exception as e:
        app.logger.exception(e)
        return {"error": str(e)}, 500

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
    if text in ("測試推播", "test"):
        status = do_scan_and_push("live")
        reply = f"測試推播：{status}"
    else:
        reply = text
    line_bot_api.reply_message(event.reply_token, TextSendMessage(text=reply))

if __name__ == "__main__":
    port = int(os.getenv("PORT", "5000"))
    app.run(host="0.0.0.0", port=port)