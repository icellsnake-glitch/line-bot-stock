## 台股清單自動更新（代號、名稱、上市/上櫃/ETF）

### 手動更新
- 打開：`/refresh-symbols`
- 成功會回應：`OK, refreshed symbols: <筆數>`
- 下載檔案：`/symbols.csv` 或直接讀取專案根目錄的 `all_taiwan_stocks.csv`

### 自動更新（免費方案建議）
- 用 cron-job.org 建一個 GET 任務：
  - URL：`https://<你的域名>/refresh-symbols`
  - 時間：每天 08:20（開盤前）或你要的時間
- 若你升級 Render Starter，也可改用 Render Jobs 定時呼叫同一路由。

### 程式使用
- 你的選股程式可直接讀檔：`all_taiwan_stocks.csv`
- 欄位：`code,name,board` 其中 `board ∈ {LISTED,OTC,ETF}`