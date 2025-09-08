import os
from flask import Flask, request, abort
from linebot import LineBotApi, WebhookHandler
from linebot.exceptions import InvalidSignatureError
from linebot.models import MessageEvent, TextMessage, TextSendMessage

app = Flask(__name__)

# ==== 環境變數（在 Render 的 Environment 介面設定）====
CHANNEL_ACCESS_TOKEN = os.getenv("LINE_CHANNEL_ACCESS_TOKEN")
CHANNEL_SECRET       = os.getenv("LINE_CHANNEL_SECRET")
USER_ID              = os.getenv("LINE_USER_ID")  # 你的個人 userId，用來 push

if not CHANNEL_ACCESS_TOKEN or not CHANNEL_SECRET:
    raise RuntimeError("Missing env: LINE_CHANNEL_ACCESS_TOKEN or LINE_CHANNEL_SECRET")
if not USER_ID:
    app.logger.warning("WARN: Missing LINE_USER_ID (push 相關功能會失敗)")

line_bot_api = LineBotApi(CHANNEL_ACCESS_TOKEN)
handler = WebhookHandler(CHANNEL_SECRET)

# ---- 健康檢查 / 首頁 ----
@app.get("/")
def root():
    return "Bot is running! 🚀", 200

# ---- Webhook 入口（LINE 平台會以 POST 打這個路由）----
@app.post("/callback")
def callback():
    signature = request.headers.get("X-Line-Signature", "")
    body = request.get_data(as_text=True)

    try:
        handler.handle(body, signature)
    except InvalidSignatureError:
        abort(400)  # 簽名錯誤 -> 400
    return "OK"

# ---- Echo：把使用者文字原樣回覆 ----
@handler.add(MessageEvent, message=TextMessage)
def handle_message(event):
    line_bot_api.reply_message(
        event.reply_token,
        TextSendMessage(text=event.message.text)
    )

# ---- 手動測試推播：GET /test-push?msg=Hello ----
@app.get("/test-push")
def test_push():
    msg = request.args.get("msg", "Hello from Bot!")
    try:
        if not USER_ID:
            return "Missing env: LINE_USER_ID", 500
        line_bot_api.push_message(USER_ID, TextSendMessage(text=msg))
        return f"Sent: {msg}", 200
    except Exception as e:
        app.logger.exception(e)
        return str(e), 500

# ---- 每日清單推播：GET /daily-push（給外部排程打）----
@app.get("/daily-push")
def daily_push():
    try:
        if not USER_ID:
            return "Missing env: LINE_USER_ID", 500

        # 這裡放你的選股邏輯；先用假資料示範
        rising_list = ["2330 台積電", "2454 聯發科", "2317 鴻海"]
        message = "今日起漲清單：\n" + "\n".join(f"{i+1}. {s}" for i, s in enumerate(rising_list))

        line_bot_api.push_message(USER_ID, TextSendMessage(text=message))
        return "Daily push sent!", 200
    except Exception as e:
        app.logger.exception(e)
        return str(e), 500

if __name__ == "__main__":
    port = int(os.getenv("PORT", "5000"))
    app.run(host="0.0.0.0", port=port)