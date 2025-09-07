from flask import Flask, request, abort
from linebot import LineBotApi, WebhookHandler
from linebot.exceptions import InvalidSignatureError
from linebot.models import MessageEvent, TextMessage, TextSendMessage
import yfinance as yf
import os

app = Flask(__name__)

# 從環境變數讀取 LINE Token
CHANNEL_ACCESS_TOKEN = os.environ.get("CHANNEL_ACCESS_TOKEN")
CHANNEL_SECRET = os.environ.get("CHANNEL_SECRET")

line_bot_api = LineBotApi(CHANNEL_ACCESS_TOKEN)
handler = WebhookHandler(CHANNEL_SECRET)

@app.route("/callback", methods=['POST'])
def callback():
    signature = request.headers['X-Line-Signature']
    body = request.get_data(as_text=True)

    try:
        handler.handle(body, signature)
    except InvalidSignatureError:
        abort(400)

    return 'OK'

# 處理文字訊息
@handler.add(MessageEvent, message=TextMessage)
def handle_message(event):
    text = event.message.text.strip()

    # ✅ Ping 測試
    if text.lower() == "ping":
        reply_text = "pong ✅"

    # ✅ 股票查詢 (輸入代號，例如: AAPL, TSLA, 2330.TW)
    else:
        ticker = text.upper()
        try:
            stock = yf.Ticker(ticker)
            hist = stock.history(period="5d")
            price = hist['Close'].iloc[-1]
            reply_text = f"{ticker} 收盤價：{price:.2f}"
        except:
            reply_text = "查詢失敗，請輸入正確股票代號"

    line_bot_api.reply_message(
        event.reply_token,
        TextSendMessage(text=reply_text)
    )

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port)
