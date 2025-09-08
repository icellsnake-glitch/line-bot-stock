import os
from datetime import datetime, timezone, timedelta

from flask import Flask, request, abort
from linebot import LineBotApi, WebhookHandler
from linebot.exceptions import InvalidSignatureError
from linebot.models import MessageEvent, TextMessage, TextSendMessage

app = Flask(__name__)

# === 環境變數 ===
CHANNEL_ACCESS_TOKEN = os.getenv("LINE_CHANNEL_ACCESS_TOKEN", "").strip()
CHANNEL_SECRET       = os.getenv("LINE_CHANNEL_SECRET", "").strip()
USER_ID              = os.getenv("LINE_USER_ID", "").strip()   # 你的個人 userId

# 若沒設好金鑰，首頁顯示提示，但不讓程序當掉
@app.get("/")
def root():
    ok = bool(CHANNEL_ACCESS_TOKEN and CHANNEL_SECRET)
    msg = "OK" if ok else "Missing env: LINE_CHANNEL_ACCESS_TOKEN / LINE_CHANNEL_SECRET"
    now = datetime.now(timezone(timedelta(hours=8))).strftime("%Y-%m-%d %H:%M:%S")
    return f"Bot is running. {msg} | {now}", 200

# === LINE SDK 物件（只有在金鑰齊全時才建立） ===
line_bot_api = LineBotApi(CHANNEL_ACCESS_TOKEN) if CHANNEL_ACCESS_TOKEN else None
handler      = WebhookHandler(CHANNEL_SECRET)   if CHANNEL_SECRET       else None

# === Webhook ===
@app.post("/callback")
def callback():
    if not (handler and CHANNEL_SECRET):
        abort(500, "LINE handler not ready (missing secrets).")
    signature = request.headers.get("X-Line-Signature", "")
    body = request.get_data(as_text=True)
    try:
        handler.handle(body, signature)
    except InvalidSignatureError:
        abort(400)
    return "OK"

# 簡單 Echo
@handler.add(MessageEvent, message=TextMessage)
def handle_message(event):
    if not line_bot_api:
        return
    line_bot_api.reply_message(event.reply_token, TextSendMessage(text=event.message.text))

# === 立即測試推播 ===
@app.get("/test-push")
def test_push():
    if not (line_bot_api and USER_ID):
        missing = []
        if not CHANNEL_ACCESS_TOKEN: missing.append("LINE_CHANNEL_ACCESS_TOKEN")
        if not CHANNEL_SECRET:       missing.append("LINE_CHANNEL_SECRET")
        if not USER_ID:              missing.append("LINE_USER_ID")
        return "Missing env: " + ", ".join(missing), 500
    now = datetime.now(timezone(timedelta(hours=8))).strftime("%Y-%m-%d %H:%M")
    line_bot_api.push_message(USER_ID, TextSendMessage(text=f"測試推播 OK：{now}"))
    return "Push sent!", 200

if __name__ == "__main__":
    port = int(os.getenv("PORT", "5000"))
    app.run(host="0.0.0.0", port=port)