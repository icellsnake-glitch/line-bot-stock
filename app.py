from flask import Flask, request, abort
from linebot import LineBotApi, WebhookHandler
from linebot.exceptions import InvalidSignatureError
from linebot.models import MessageEvent, TextMessage, TextSendMessage
import os

app = Flask(__name__)

# 從環境變數讀取 LINE Bot 設定
channel_secret = os.getenv("LINE_CHANNEL_SECRET")
channel_access_token = os.getenv("LINE_CHANNEL_ACCESS_TOKEN")
default_user_id = os.getenv("LINE_USER_ID")

if channel_secret is None or channel_access_token is None:
    raise Exception("請先在 Render 環境變數設定 LINE_CHANNEL_SECRET 與 LINE_CHANNEL_ACCESS_TOKEN")

line_bot_api = LineBotApi(channel_access_token)
handler = WebhookHandler(channel_secret)

@app.route("/health", methods=["GET"])
def health():
    return "ok", 200

@app.route("/callback", methods=["POST"])
def callback():
    signature = request.headers["X-Line-Signature"]
    body = request.get_data(as_text=True)
    try:
        handler.handle(body, signature)
    except InvalidSignatureError:
        abort(400)
    return "OK"

@handler.add(MessageEvent, message=TextMessage)
def handle_message(event):
    line_bot_api.reply_message(
        event.reply_token,
        TextSendMessage(text=f"你說了：{event.message.text}")
    )

# 測試推播端點
@app.route("/push", methods=["GET"])
def push_message():
    msg = request.args.get("msg", "Hello from Render!")
    to = request.args.get("to", default_user_id)
    if not to:
        return "缺少目標 userId", 400
    line_bot_api.push_message(to, TextSendMessage(text=msg))
    return f"推播完成：{msg}", 200

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5000)
