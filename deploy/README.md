# Part 4：從本機到雲端（CI/CD 與 Cloud Run 部署指南）

本目錄與 `.github/workflows/` 提供工作坊 **Part 4（從本機到雲端）** 的完整範例檔：

- `.github/workflows/ci.yml`：離線 `mock` CI 管線（不花錢、不需 API Key，驗證 `hw doctor`、`hw check L1/L2`、`hw test` 與啟動前預檢）。
- `.github/workflows/deploy-cloud-run.yml`：透過 Workload Identity Federation (WIF) 部署至 Google Cloud Run，並由 Secret Manager 注入密鑰。
- `deploy/check_env.py`：啟動前環境變數檢查腳本（缺少必要變數或密鑰長度不足立即 `exit 1`，只印長度、不洩漏密鑰內容）。
- `deploy/Dockerfile.example`：正式上雲用 Dockerfile（非 root 執行、啟動前先跑 `check_env.py`）。
- `deploy/dockerignore.example`：防止 `.env`、`.git`、本機紀錄檔被打包進映像或上傳至 Cloud Build。

---

## 1. 本機演練「瞬間 3：失敗會叫（Fail Loud）」

### 1.1 故意少給環境變數 → 啟動瞬間 `exit 1` 攔截

```bash
# 方式 A：直接執行啟動預檢腳本（故意漏掉 GEMINI_API_KEY）
SERVER_SECRET=demo-only-secret-0123456789abcdef-xyz \
CLASS_CODE=demo-class-code \
ADMIN_TOKEN=demo-admin-token-0123456789abcdef-xyz \
uv run python deploy/check_env.py SERVER_SECRET CLASS_CODE ADMIN_TOKEN GEMINI_API_KEY
```

預期輸出（非零結束碼 `1`）：
```text
環境預檢失敗：缺少 GEMINI_API_KEY
```

或直接啟動 FastAPI 服務（故意移除密鑰變數）：
```bash
env -u CLASS_CODE -u SERVER_SECRET -u ADMIN_TOKEN \
  uv run --extra server uvicorn server.app:app --host 127.0.0.1 --port 8080
```

### 1.2 補齊變數通過預檢（只印長度、不印密鑰內容）

```bash
SERVER_SECRET=demo-only-secret-0123456789abcdef-xyz \
CLASS_CODE=demo-class-code \
ADMIN_TOKEN=demo-admin-token-0123456789abcdef-xyz \
GEMINI_API_KEY=demo-only-key \
uv run python deploy/check_env.py SERVER_SECRET CLASS_CODE ADMIN_TOKEN GEMINI_API_KEY
```

---

## 2. 在本機啟動 Gateway 服務

```bash
SERVER_SECRET=demo-only-secret-0123456789abcdef-xyz \
CLASS_CODE=demo-class-code \
ADMIN_TOKEN=demo-admin-token-0123456789abcdef-xyz \
uv run --extra server uvicorn server.app:app --host 127.0.0.1 --port 8080
```

- 健康檢查：<http://127.0.0.1:8080/health>
- 本機排行榜：<http://127.0.0.1:8080/leaderboard>

---

## 3. 部署到 Google Cloud Run（搭配 Secret Manager）

```bash
gcloud run deploy hwdc-class-gateway --source . --region asia-east1 \
  --allow-unauthenticated --min-instances=1 --max-instances=1 --concurrency=80 --cpu=2 --memory=1Gi \
  --set-secrets=GEMINI_API_KEY=gemini-api-key:latest,CLASS_CODE=hwdc-class-code:latest,SERVER_SECRET=hwdc-server-secret:latest,ADMIN_TOKEN=hwdc-admin-token:latest
```
