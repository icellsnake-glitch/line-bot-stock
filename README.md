# LINE 股票清單推播（Render 版，無 line-bot-sdk）

## 必要環境變數
- LINE_CHANNEL_ACCESS_TOKEN  ← LINE Messaging API 的 Channel access token (長字串)
- LINE_USER_ID               ← 你的 User ID（在 LINE Developers > Messaging API 看到）
- WATCHLIST                  ← 以逗號分隔的代號清單，例如：2330,2317,2454
- MIN_CHANGE_PCT             ← 漲幅門檻（預設 2.0）
- MIN_VOLUME                 ← 成交量門檻（預設 1000000）
- MAX_LINES_PER_MSG          ← 每則訊息最多列數（預設 25）
- MAX_CHARS_PER_MSG          ← 每則訊息最多字數（預設 1800）

## 部署後測試
1. 開啟根網址：`/` 看到健康檢查說明。
2. 手動推送清單：`/daily-push`
3. 單純測訊息：`/test-push?msg=Hello`

> 免費方案沒有 Cron Jobs，自動排程可改用第三方（GitHub Actions/IFTTT/Zapier）每天請求 `/daily-push`。