import os
from flask import Flask, request, abort
from linebot import LineBotApi, WebhookHandler
from linebot.exceptions import InvalidSignatureError
from linebot.models import MessageEvent, TextMessage, TextSendMessage

app = Flask(__name__)

# ✅ 從環境變數讀取（請到 Render > Settings > Environment 先設定）
CHANNEL_ACCESS_TOKEN = os.getenv("CHANNEL_ACCESS_TOKEN")
CHANNEL_SECRET = os.getenv("CHANNEL_SECRET")

if not CHANNEL_ACCESS_TOKEN or not CHANNEL_SECRET:
    # 若沒設好環境變數，啟動時就直接提示，避免之後才噴錯
    raise RuntimeError("Missing env: CHANNEL_ACCESS_TOKEN or CHANNEL_SECRET")

line_bot_api = LineBotApi(CHANNEL_ACCESS_TOKEN)
handler = WebhookHandler(CHANNEL_SECRET)

# 健康檢查（開根網址會看到 OK，方便確認服務有起來）
@app.get("/")
def health():
    return "OK", 200

@app.route("/callback", methods=["POST"])
def callback():
    # 取簽名；若 header 缺失，回 400
    signature = request.headers.get("X-Line-Signature")
    if not signature:
        abort(400, description="Missing X-Line-Signature")

    # 取請求內容（字串）
    body = request.get_data(as_text=True)

    # 驗證與處理事件
    try:
        handler.handle(body, signature)
    except InvalidSignatureError:
        abort(400, description="Invalid signature")

    return "OK", 200

# 回覆文字訊息（echo）
@handler.add(MessageEvent, message=TextMessage)
def handle_message(event):
    line_bot_api.reply_message(
        event.reply_token,
        TextSendMessage(text=event.message.text)
    )

if __name__ == "__main__":
    # Render 會提供 PORT 環境變數
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port)