# 小額模擬交易安全設計

本版只有**模擬引擎**，沒有任何交易所下單 API；已由 Auto Scan 連接訊號日記並進行模擬開平倉，但不會發送真實委託。

預設 `PAPER_TRADING_ENABLED=false`、`TRADING_KILL_SWITCH=true`；兩者必須明確設定才允許模擬開倉。真實下單請求永遠被拒絕。

預設測試資金 50 USDT、每筆風險預算 0.5%、每日損失停機線 2%、最多一筆模擬持倉、每天最多兩筆新單、1 倍槓桿及 50 USDT 名目上限。只接受等回測策略且品質分數至少 50 的訊號。交易成本以往返約 0.22% 估算，不含實際 funding；不保證成交或最大損失。

`python -m unittest discover -s tests -v` 執行測試。

## 尚未完成（不可直接用於實盤）

1. 連續行情與成交狀態機仍不完整；目前使用間歇式已收盤 K 線模擬。
2. 雖然 Auto Scan 單獨發布 cloud-journal，仍缺乏真正原子性的跨流程交易狀態與通知 outbox。不可依賴 runner 本機檔案或 cache 保證風控。
3. Bitunix API 簽名、最小委託量／精度、交易所止損止盈確認、持倉對帳、取消與錯誤恢復。
4. 真實下單必須另外取得明確確認，且使用無提幣權限的專用 API Key（存放 GitHub Secrets，不可貼在聊天中）。
5. 至少 30 筆模擬成交及異常測試通過，才評估真實小額交易。

**安全警告：** 此模組的 `try_open` 僅修改傳入的記憶體狀態；呼叫者必須在可靠鎖下讀取並原子保存，否則多個排程仍可能突破限制。

## 自動掃描與 Telegram（新）

`auto-scan.yml` 先從 `cloud-journal` 取回 `cloud/paper_state.json`（首次以主分支明確種子檔初始化），模擬處理最近已收盤 15m K 線，並在 journal snapshot 一併發布持倉狀態。成功發布後才送 Telegram 開倉／平倉通知。

預設仍停用：Repository Variables `PAPER_TRADING_ENABLED=true` 且 `TRADING_KILL_SWITCH=false` 才會模擬交易。永不發送真實交易委託。請先驗證 GitHub Actions 成功與 cloud snapshot 更新，再考慮切換兩個變數。

限制：GitHub Actions 掃描為間歇性，僅取最近已收盤 K 線；不能保證捕捉所有盤中波動，模擬成交價非交易所實際成交。Telegram 通知可能因網路故障遺失，不能作為唯一的持倉紀錄。可靠紀錄以 cloud/paper_state.json 為準。若 snapshot 推送失敗，workflow 不會發出該輪通知。
\n## 維運限制（2026-11）\n\n- 手動 Scan 不再寫入 cloud-journal，避免清除 paper_state；只保留短期 workflow artifact。\n- 模擬成交按可取得的已收盤 K 線順序補算，歷史資料仍受交易所 API 取樣上限影響；無法保證完整盤中路徑。\n- 每日摘要以 close_at (UTC) 歸屬；舊事件缺乏 close_at 不納入每日報表，但仍計入累積績效。\n- 訊號品質分數是規則評分，不代表實際勝率；不要用此分數直接估算報酬。\n