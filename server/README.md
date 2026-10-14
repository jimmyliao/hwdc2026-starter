# 講師共用 gateway

從 `starter/` 啟動；需 Python 3.11 以上及 uv：

```sh
uv run --extra server uvicorn server.app:app --host 127.0.0.1 --port 8080
```

設定 `SERVER_SECRET`（HMAC 簽章密鑰）、`CLASS_CODE`（領取代碼）、`ADMIN_TOKEN`（管理端密鑰）；正式服務應使用 Secret Manager 注入，勿寫入程式庫、命令歷史或日誌。講師使用真實模型時可選配 `GEMINI_API_KEY` 與 `gemini` extra，沿用 gateway 的 gemini provider；Vertex／ADC 選項【待驗證】，未實作。預設別名 `fast,mock`，其中 `fast` 使用 Gemini；純離線測試使用 `mock`。部署時請限制 Cloud Run 呼叫來源／設定適當存取政策，公開端點僅靠限時 token 與班級代碼保護。

`POST /claim` 傳入 `{"class_code":"…","nickname":"…"}`，回傳 `token`、台北時間 ISO 8601 `expires_at`、`expires_on`（YYYYMMDD）。格式與防外洩流程見 [token 規格](../../ops/token-format.md)；舊版 token 須重新 `make claim`。後續 `/v1/chat/completions`、`/questions`、`/submit` 需 `Authorization: Bearer <token>`；聊天傳入 `{"model":"mock","messages":[{"role":"user","content":"你好"}]}`，回傳 OpenAI 相容的 `choices`、`usage` 與 `x_cost_usd`。`/submit` 接受 `level`、`passed`（布林）、`tokens`（非負整數）、`cost`（非負美元）、`details`（物件）。`/leaderboard` 是每個暱稱最新提交的 HTML 排行榜，每 5 秒自動重整；`?format=json` 回傳 JSON。`/healthz` 無需驗證。`/admin/stats`、`POST /admin/kill`、`POST /admin/revive` 使用 `Authorization: Bearer <ADMIN_TOKEN>`。

`TOKEN_EXPIRES_ON` 預設 `20261016`（台北時間當天 23:59:59），`TOKEN_TTL_S` 選配正整數秒、`TOKEN_RPM` 預設 6 次/分鐘、`TOKEN_TPM` 預設 4000 估算 token/分鐘、`MAX_INPUT_TOKENS` 預設 1500、`GLOBAL_CONCURRENCY` 預設 16、`GLOBAL_QUEUE_TIMEOUT_S` 預設 20 秒、`TOKEN_BUDGET_USD` 預設 0.05、`GLOBAL_BUDGET_USD` 預設 10；上述數值皆可透過環境變數覆蓋（需正數）。`ALLOWED_ALIASES` 預設 `fast,mock`（即使設為 `smart`，學員仍不可用；僅講師設 `INSTRUCTOR=1` 才可開放），`QUESTIONS_FILE` 預設 `questions.sample.json`。claim 依識別 IP 每分鐘最多 `CLAIM_RPM`（預設 20）次；單次請求 64 KiB（串流計數，超過立即 413）、最多 20 則訊息及共 4000 字元。輸入上限包含系統提示；訊息內容以保守方式估算（CJK 1；ASCII 字母／空白 1/4；ASCII 數字與符號 1/2；emoji、符號、非拉丁字母等 1 字元 = 1 token）；超過時 HTTP 413 提示先截斷 context，不會替使用者自動裁切。TPM 以同一估算輸入加固定 256 輸出 token 計算每次成功請求；429 會附 `Retry-After`。同一 token 同時只能有一個請求，第二個立即回 429（請等上一個請求完成）；不同 token 共用全域 slot，等候逾時回 503 並附 `Retry-After`。排隊逾時不扣 RPM／TPM／預算；上游失敗仍計入已放行的 RPM，但釋放 TPM 與預算預留。所有限速及併發狀態僅限單一程序。
server 呼叫上游時不使用學員 gateway 的本機 `RPM_LIMIT=5` 節流；改由上述每 token RPM/TPM、全域併發與費用預算實施護欄。學員端單題仍遵守本機節流。

每次放行前預留「CJK 感知估算輸入 token × 輸入單價 + 固定 256 輸出 token × 輸出單價」的最壞成本（含備援模型及系統提示）；進行中的預留與已花費一併計入 token／全場預算，失敗後釋放，成功後改記實際費用。HTTP 402 的 `detail` 會回報剩餘額及所需預留額。`x_truncated` 與 `finish_reason=length` 表示答案截斷。預留額與 TPM 是估算值，非上游 tokenizer 的硬性保證；上游超出估算時仍可能超過硬上限，應另設上游帳務防線。gateway 原有磁碟快取維持不變；快取命中仍算一次成功請求的 RPM／TPM，但成本仍以 gateway 回報的零費用計。`KILL=1` 永遠停用學員端點，解除環境變數後才能由管理端重啟；沒有設定 `KILL` 時管理端可 kill/revive。請勿設定白名單以外的 `FALLBACK`。

容器映像（從 `starter/` 作為 build context；**僅供本機測試，正式部署只用 [`deploy/Dockerfile.example`](../../deploy/Dockerfile.example)**）：

```sh
docker build -f server/Dockerfile -t classroom-gateway .
```

Cloud Run 來源部署範例（**僅供參考，不在本專案執行**；`--source .` 採 Buildpacks 而非 `server/Dockerfile`，部署前需確認目標環境已配置 uv、server extra 與入口點；**務必先排除本機 `.env`、key 與其他私密檔案，不可直接上傳含機密的工作目錄**）：

```sh
gcloud run deploy classroom-gateway --source . --region YOUR_REGION \
  --max-instances=1 --min-instances=1 --concurrency=80 --cpu=2 --memory=1Gi
```

以上僅為 [本機負載測試](../../ops/loadtest-report.md) 建議的起始規格，尚未於 Cloud Run 驗證；維持單一 worker，實測後再調整資源。`--min-instances=1` 產生閒置費用。

服務狀態只存在單一程序的記憶體，每 30 秒及正常關閉時傾印到 `/tmp/hwdc2026-server-state.json` **僅供診斷，不會在啟動時讀回**；重啟或多 worker 會重置／分裂限速、預算、排行榜與停用狀態。Cloud Run 的 `/tmp` 並非持久儲存；生產使用需外部持久化與多實例協調。

## Token 設定與撤銷

| 環境變數 | 用途／預設 |
| --- | --- |
| `SERVER_SECRET` | 必填；HMAC 簽章密鑰，輪換後全部既有 token 失效 |
| `CLASS_CODE` | 必填；領取時的班級代碼 |
| `ADMIN_TOKEN` | 必填；管理員端點驗證 |
| `TOKEN_EXPIRES_ON` | `20261016`；台北時間當日 23:59:59 絕對到期，格式 YYYYMMDD |
| `TOKEN_TTL_S` | 選配；相對秒數上限，單程序記憶體，重啟後不再有效 |
| `TOKEN_BUDGET_USD` | `0.05`；每張 token 預算 |

管理端點均需 `Authorization: Bearer <ADMIN_TOKEN>`：`POST /admin/revoke {"nickname":"…"}`、`POST /admin/unrevoke {"nickname":"…"}`、`GET /admin/revoked`。撤銷後該暱稱的 token 回 401；**重啟會清空撤銷名單**，多實例互不共享。全場失效仍須換 `SERVER_SECRET` 或設 `KILL=1`。紙條簽發 `SERVER_SECRET=… TOKEN_EXPIRES_ON=… python tools/issue_slips.py 10 --prefix slip`（暱稱自動加 6 字元隨機尾碼如 `slip01-k3x9ab`，輸出目錄 0700、檔案 0600；`--no-suffix` 僅供測試）（在 repo 根目錄執行；勿在命令列寫真密鑰，應安全注入環境）；`ops/slips/` 的 CSV／HTML **含完整有效 token，絕不能 commit 或公開**。

## 資安強化設定（R1 修復）

| 環境變數 | 用途／預設 |
| --- | --- |
| `CLASS_CODE` / `SERVER_SECRET` / `ADMIN_TOKEN` | 啟動檢查：至少 10／32／32 字元，不合格即 exit 1 並說明；`ALLOW_WEAK_SECRETS=1` 僅供本機測試 |
| `TRUSTED_PROXY_HOPS` | `1`；取 `X-Forwarded-For` 由右往左第 N 個位址（`0` 表示不信任 XFF）；標頭缺少、跳數不足或內容不是 IP 時退回 `client.host`。用 `GET /admin/debug-client` 實機驗證 |
| `CLAIM_RPM` / `CLAIM_GLOBAL_RPM` | `20` / `120`；每識別 IP 與全域每分鐘 claim 上限（會場可能共用公網 IP，故不做「每 IP 暱稱數」限制） |
| `CLAIM_FAIL_LIMIT` / `CLAIM_LOCK_S` | `10` / `60`；每識別 IP 課堂代碼錯誤達次數即鎖定秒數 |
| `CLAIM_GLOBAL_FAIL_LIMIT` / `CLAIM_GLOBAL_COOLDOWN_S` | `60` / `60`；全域一分鐘內錯誤過多時，新領取進入全域冷卻；**已持有有效 token 的學員重領（reclaim）與既有 token 的所有請求不受影響** |
| `ADMIN_FAIL_LIMIT` / `ADMIN_LOCK_S` | `10` / `60`；`/admin/*` 驗證失敗鎖定（鎖定期間連正確 token 也回 429，講師請稍候或換網路） |
| `MAX_TOKENS_ISSUED` | `80`；全場簽發上限（以暱稱計；重領不占額度） |
| `UPSTREAM_TIMEOUT_S` / `MAX_WAITING` | `30` / `16`；上游逾時回 504（slot 待上游真正結束才釋放）；排隊等候數超過回 503＋`Retry-After` |
| `MAX_CACHE_FILES` / `MAX_USAGE_LOG_BYTES` | `500` / `5242880`；每 30 秒清理 gateway 快取目錄與 usage 記錄；server 預設把兩者放在 `/tmp` |

- **暱稱策略**：暱稱先做 NFKC 正規化、去除控制／格式字元、收斂空白並 casefold，撤銷、綁定、排行榜都用正規化後的值（`Ａlice`、`ALICE`、` alice ` 視為同一人）。暱稱首次簽發後綁定：前一把 token 未到期時，只有在 `Authorization: Bearer <舊 token>` 帶舊 token 的重新領取（reclaim）才被接受（回傳同一把 token，`reclaimed: true`），否則回 409；遺失 token 的學員請講師執行 `POST /admin/release {"nickname":"…"}` 解除綁定。
- **公開資料**：`/submit` 的 `details` 只保留白名單（`l1`/`l2` 的 `passed`/`total`、`question_count`、`quality_passed`），其餘全部丟棄；`/leaderboard`（HTML 與 JSON）只回 `nickname`、`level`、`passed`、`tokens`、`cost`、`updated_at`。學員自報的數值只是趣味排行，不具可信度。
- **重啟偵測**：`/healthz` 與 `/admin/stats` 回報 `boot_id`（每次啟動隨機）與 `uptime_s`。**記憶體狀態（撤銷名單、已花費、全場預算、綁定、kill、限速、排行榜）重啟即歸零**；對策是單實例（`--max-instances=1`）＋上游專案配額／預算警示＋人工監控 `boot_id`，重啟後立刻重新套用 `POST /admin/revoke`、`/admin/kill` 等狀態。
- `/docs`、`/redoc`、`/openapi.json` 已關閉；未處理例外一律回 `{"detail":"內部錯誤"}`，不含堆疊。
