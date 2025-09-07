import os
from flask import Flask, request, abort
from linebot import LineBotApi, WebhookHandler
from linebot.exceptions import InvalidSignatureError
from linebot.models import MessageEvent, TextMessage, TextSendMessage

app = Flask(__name__)

# 從 Render 環境變數讀取
CHANNEL_ACCESS_TOKEN = os.getenv("IWMbWOthRWcoHk/PXDf8V9
Op48XFk7UaB0BsXuFUdiMwh
SJh75ULj4dreQY2hpJOSVCRS
+wj34MUZnw9WbX9qVhMz6
D5lovXCUbNigGEOEJz3rd/A/v
NkWjECvnvf8Ftrh/U9SQKc3Xb
G44ZLNDtKQdB04t89/10/w1c
DnyilFU=")
CHANNEL_SECRET = os.getenv("2320caa4040a38e3c405d9c72d27eafc")

line_bot_api = LineBotApi(CHANNEL_ACCESS_TOKEN)
handler = WebhookHandler(CHANNEL_SECRET)

@app.route("/callback", methods=['POST'])
def callback():
    # 獲取簽名
    signature = request.headers['X-Line-Signature']

    # 獲取請求內容
    body = request.get_data(as_text=True)

    # 驗證簽名
    try:
        handler.handle(body, signature)
    except InvalidSignatureError:
        abort(400)

    return 'OK'

# 回覆訊息
@handler.add(MessageEvent, message=TextMessage)
def handle_message(event):
    line_bot_api.reply_message(
        event.reply_token,
        TextSendMessage(text=event.message.text)
    )

if __name__ == "__main__":
    # Render 預設會給 PORT 環境變數
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port)