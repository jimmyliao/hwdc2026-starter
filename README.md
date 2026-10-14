# 打造跨模型 AI Agent 的 CI/CD 管線：從本地開發到雲端部署

**Hello World Dev Conference 2026｜體驗工作坊學員專案（Starter）**

- **投影片與學員手冊下載**：<https://jimmyliao.pse.is/hwdc2026>
- **課堂共筆（HackMD）**：<https://hackmd.io/@HWDC/2026/%2FS1UgtE7sGe>
- **環境需求**：Python 3.11 以上及 [uv](https://docs.astral.sh/uv/)（建議備有 `git`；先執行 `git --version` 與 `uv --version` 確認）。L1 與 L2 可全程離線進行，不需自備 API Key。

---

## 1. 取得專案（`git clone` 或免 `git` 抓 tarball）

- **方式 A（推薦，已安裝 `git`）**：
  ```sh
  git clone https://github.com/jimmyliao/hwdc2026-starter.git
  cd hwdc2026-starter
  ```
- **方式 B（免 `git`，直接抓 GitHub tarball 解壓縮）**：
  - macOS／Linux／WSL：
    ```sh
    mkdir -p hwdc2026-starter && curl -fsSL https://github.com/jimmyliao/hwdc2026-starter/archive/refs/heads/main.tar.gz | tar -xz --strip-components=1 -C hwdc2026-starter && cd hwdc2026-starter
    ```
  - Windows PowerShell：
    ```powershell
    mkdir hwdc2026-starter -Force | Out-Null; curl.exe -fsSL https://github.com/jimmyliao/hwdc2026-starter/archive/refs/heads/main.tar.gz | tar -xz --strip-components=1 -C hwdc2026-starter; cd hwdc2026-starter
    ```

---

## 2. 安裝與自我檢查

進入 `hwdc2026-starter/` 後執行（Windows PowerShell 無須安裝 `make`，直接使用 `uv run hw <子指令>`）：

```sh
make setup             # Windows PowerShell: uv sync
cp .env.example .env   # Windows PowerShell: copy .env.example .env
uv run hw doctor       # 環境自我檢查（確認全部 PASS）
make models            # Windows PowerShell: uv run hw models
make check L=L1        # Windows PowerShell: uv run hw check L1（出廠狀態為 0/6）
```

> **注意**：`make models` 輸出格式為 `別名: 實際模型名稱`——**冒號左邊**（`fast`、`mock`、`class`）才是設定裡要填的「**別名（alias）**」，請勿填冒號右邊的模型全名（如 `gemini-3.1-flash-lite`）。

### 常用指令對照表

| 功能 | macOS／Linux／WSL（`make`） | Windows PowerShell（跨平台通用） |
|---|---|---|
| 安裝相依套件 | `make setup` | `uv sync` |
| 環境自我診斷 | `uv run hw doctor` | `uv run hw doctor` |
| 列出可用模型別名 | `make models` | `uv run hw models` |
| 單題問答 | `make ask Q="你好"` | `uv run hw ask "你好"` |
| L1 檢查（x/6） | `make check L=L1` | `uv run hw check L1` |
| L2 檢查（x/3） | `make check L=L2` | `uv run hw check L2` |
| 領取課堂限時 Token | `CLASS_CODE=<課堂代碼> NICKNAME=你的暱稱 make claim` | `$env:CLASS_CODE="<課堂代碼>"; $env:NICKNAME="你的暱稱"; uv run hw claim` |
| 執行 10 題離線批次練習 | `make run` | `uv run hw run` |
| 產生用量與成本報表 | `make report` | `uv run hw report` |
| 預覽提交資料 | `make submit SUBMIT_ARGS=--dry-run` | `uv run hw submit --dry-run` |
| 送出成績至排行榜 | `make submit` | `uv run hw submit` |
| 執行單元測試 | `make test` | `uv run hw test` |

---

## 3. 工作坊實作流程（L1 → 瞬間 1 → L2 → L3）

1. **Part 2｜L1 簡單改（`make check L=L1` 0/6 → 6/6）**：
   - 依 `make check L=L1` 的提示（與手冊 `lab-guide.html` 的參考做法）修復 `agent/config.py`、`agent/main.py`、`.env.example`、`.gitignore` 六個問題。
2. **瞬間 1｜換模型不改程式（`make claim` + `PROVIDER=mock/class`）**：
   - 修完 L1 後，領取課堂限時 token（成功後會自動將 `GATEWAY_TOKEN`、`GATEWAY_URL`、`CLASS_CODE`、`NICKNAME` 寫回 `.env`）：
     ```sh
     CLASS_CODE=<現場公布的課堂代碼> NICKNAME=你的暱稱 make claim
     PROVIDER=mock make ask Q="用一句話介紹台北"
     PROVIDER=class make ask Q="用一句話介紹台北"
     ```
3. **Part 3｜L2 進階（`make check L=L2` 0/3 → 3/3）**：
   - 執行 `make run` 與 `make report` 檢視 `report.html`，在 `scenarios/tasks.py` 完成輸入 token 截斷（L2-①）、多租戶 `tenant` 預算控管（L2-②）與 429 `FALLBACK` 備援鏈（L2-③）。
4. **L3｜送出排行榜（`make submit`）**：
   - 確認 `L1：6/6` 與 `L2：3/3` 後，執行 `make run` 與 `make submit`（全程離線 `mock` 計價，0 次真實呼叫）。

---

## 4. Part 4：CI/CD 管線與 Cloud Run 部署（專案內附完整範例）

本 repo 已內建完整的 GitHub Actions CI/CD 與容器化部署範本，詳見 [`deploy/README.md`](deploy/README.md)：

| 檔案路徑 | 用途 |
|---|---|
| [`.github/workflows/ci.yml`](.github/workflows/ci.yml) | GitHub Actions 離線 `mock` CI 管線（執行 `uv sync`、`hw doctor`、`hw test`、環境預檢與 `hw check L1/L2`） |
| [`.github/workflows/deploy-cloud-run.yml`](.github/workflows/deploy-cloud-run.yml) | Cloud Run CD 部署範本（透過 WIF 驗證並以 Secret Manager `--set-secrets` 注入密鑰） |
| [`deploy/check_env.py`](deploy/check_env.py) | **瞬間 3（失敗會叫）**：容器啟動前檢查必要環境變數與密鑰長度，缺漏立即 `exit 1` |
| [`deploy/Dockerfile.example`](deploy/Dockerfile.example) | 正式環境 Cloud Run Dockerfile（非 root 執行、啟動前預檢） |
| [`deploy/dockerignore.example`](deploy/dockerignore.example) | 防止 `.env`、`.git`、本機紀錄檔被打包進映像或上傳 |
| [`server/app.py`](server/app.py) | 將 `gateway/llm.py` 包裝為 FastAPI 小服務（含 `/health`、`/claim`、`/v1/chat/completions`、`/leaderboard`） |

### 本機快速演練 Part 4（不需 Docker、不需真實 API Key）

```sh
# 1. 瞬間 3（失敗會叫）：故意漏設環境變數，啟動瞬間立即報錯 exit 1
env -u CLASS_CODE -u SERVER_SECRET -u ADMIN_TOKEN \
  uv run --extra server uvicorn server.app:app --host 127.0.0.1 --port 8080

# 2. 補齊測試變數，在本機啟動 Gateway 服務（瀏覽 http://127.0.0.1:8080/health 與 /leaderboard）
SERVER_SECRET=demo-only-secret-0123456789abcdef-xyz \
CLASS_CODE=demo-class-code \
ADMIN_TOKEN=demo-admin-token-0123456789abcdef-xyz \
uv run --extra server uvicorn server.app:app --host 127.0.0.1 --port 8080
```
