import time
import datetime as dt
from flask import request
from linebot.models import TextSendMessage

def tw_now():
    return dt.datetime.now(dt.timezone(dt.timedelta(hours=8)))

def wait_until(target: dt.datetime):
    """每 15 秒醒來一次，直到到達 target（台北時間）"""
    while True:
        now = tw_now()
        if now >= target:
            return
        # 只睡一小段，避免平台長時間阻塞
        remaining = (target - now).total_seconds()
        time.sleep(min(15, max(1, remaining)))

@app.get("/onejob-push")
def onejob_push():
    # 驗證
    cron_key = request.args.get("key", "")
    if cron_key != os.getenv("CRON_SECRET", ""):
        return "Forbidden", 403
    if not USER_ID:
        return "Missing env: LINE_USER_ID", 500

    # 你的監看清單（用你現有的 pick_rising_stocks 邏輯）
    watchlist = [
        "2330","2454","2317","2303","2603","2882","2412",
        "1303","1101","5871","1605","2377","3481","3661",
    ]

    # 今日三個時間點（台北時間）
    today = tw_now().date()
    t0700  = dt.datetime.combine(today, dt.time(7, 0),  tzinfo=dt.timezone(dt.timedelta(hours=8)))
    t0730  = dt.datetime.combine(today, dt.time(7, 30), tzinfo=dt.timezone(dt.timedelta(hours=8)))
    t0800  = dt.datetime.combine(today, dt.time(8, 0),  tzinfo=dt.timezone(dt.timedelta(hours=8)))
    targets = [(t0700, "07:00"), (t0730, "07:30"), (t0800, "08:00")]

    pushed = []
    try:
        for target_dt, label in targets:
            # 若現在已過時段，就直接略過（例如你晚一點才觸發）
            if tw_now() < target_dt:
                wait_until(target_dt)

            # 產生一次「起漲清單」訊息（你已經有 pick_rising_stocks）
            picked = pick_rising_stocks(
                watchlist=watchlist,
                min_change_pct=2.0,
                min_volume=1_000_000,
                top_k=10
            )
            date_txt = tw_now().strftime("%Y-%m-%d")
            msg = f"【{date_txt} 起漲清單】\n" + ("\n".join(picked) if picked else "尚無符合條件（或資料未更新）")
            msg += f"\n⏰ 預設推送時間 {label}"

            line_bot_api.push_message(USER_ID, TextSendMessage(text=msg))
            pushed.append(label)

        return f"One job done. Pushed at {', '.join(pushed)}", 200

    except Exception as e:
        app.logger.exception(e)
        return str(e), 500