import os
from flask import Flask, request, jsonify
from linebot import LineBotApi
from linebot.models import TextSendMessage

app = Flask(__name__)

# 必填的三個環境變數（都是一行、不能有換行）
LINE_CHANNEL_ACCESS_TOKEN = os.getenv("LINE_CHANNEL_ACCESS_TOKEN", "").replace("\n", "").strip()
LINE_USER_ID = os.getenv("LINE_USER_ID", "").strip()  # 你的 User ID（U 開頭）
CRON_TOKEN = os.getenv("CRON_TOKEN", "change-me").strip()

line_bot_api = LineBotApi(LINE_CHANNEL_ACCESS_TOKEN)

@app.get("/")
def root():
    return "Bot is running! 🚀", 200

# 手動測試推播：瀏覽 https://你的域名/test-push?msg=hi
@app.get("/test-push")
def test_push():
    msg = request.args.get("msg", "👋 測試推播成功！")
    line_bot_api.push_message(LINE_USER_ID, TextSendMessage(text=msg))
    return jsonify(ok=True, msg=msg), 200

# 排程用的網址：Render 每天打這個網址就會推播
@app.get("/cron")
def cron():
    if request.args.get("token") != CRON_TOKEN:
        return jsonify(error="unauthorized"), 401
    text = "🌅 早安！我是你的股市小幫手，之後這裡會放起漲清單～"
    line_bot_api.push_message(LINE_USER_ID, TextSendMessage(text=text))
    return jsonify(ok=True, pushed=True), 200

if __name__ == "__main__":
    port = int(os.getenv("PORT", "5000"))
    app.run(host="0.0.0.0", port=port)