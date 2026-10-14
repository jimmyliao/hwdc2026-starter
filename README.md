# LLM Gateway 工作坊：學員快速開始

需求：Python 3.11 以上及 [uv](https://docs.astral.sh/uv/)（建議同時備有 `git`；先跑 `git --version` 與 `uv --version` 確認）。L1 和 L2 可全程離線進行，不需真實 API key。

### 1. 取得專案（`git clone` 或免 `git` 抓 tarball）

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

### 2. 安裝與自我檢查

進入 `hwdc2026-starter/` 後（Windows PowerShell 無須安裝 make）：

```sh
uv sync
cp .env.example .env   # Windows PowerShell: copy .env.example .env
uv run hw doctor
uv run hw models
uv run hw check L1
```

macOS／Linux 亦可使用 `make setup`、`cp .env.example .env`、`make models`、`make check L=L1`。

發放版故意留下 L1 六項練習；基本①②③以 15 分鐘為目標，加強④⑤⑥可選做。依 `uv run hw check L1`（或 `make check L=L1`）的提示，並用 `uv run hw models`（或 `make models`）比對設定，逐題修復。`uv run hw ask "你好"`（或 `make ask Q="你好"`）應顯示回答、provider、token 與美元成本。今天請先用 `mock`；領取令牌後（`uv run hw claim` 或 `make claim`）可使用 `class`。`fast` 需要個人的真實上游 key，課堂不需使用；`smart` 只供講師設 `INSTRUCTOR=1` 使用；`class` 的 `GATEWAY_MODEL` 僅支援 `fast` 或 `mock`。

接著可使用 `uv run hw check L2`（或 `make check L=L2`）練習上下文長度、tenant 預算與 fallback，目標 15 分鐘。兩關全過後，執行 `uv run hw run`（或 `make run`）用離線 mock 跑十題、產生 `answers.json`，並查看總 token／成本；未設 `INSTRUCTOR=1` 時直接執行十題 runner 也只允許 mock，避免意外連續真實呼叫。每位學員的 class 真實請求使用本機鎖，一次只能送一筆（可用 `REMOTE_LOCK_TIMEOUT_S` 調整等候秒數），429 自動 full jitter 退避；請等上一個請求完成。`uv run hw submit --dry-run`（或 `make submit SUBMIT_ARGS=--dry-run`）可離線預覽提交資料，`uv run hw submit`（或 `make submit`）正式提交。其他指令：`uv run hw report`（`make report`）產生用量報告、`uv run hw test`（`make test`）執行測試、`uv run hw doctor` 診斷環境。回答固定要求不超過三句、最多 256 輸出 token；`truncated` 表示輸出被截斷。只有選擇真實模型時才需要自行設定對應的 API key；不要把 `.env` 或 key 提交到版控。

`uv run hw claim`（或 `make claim`，例如 `CLASS_CODE=<現場公布的課堂代碼> NICKNAME=你的暱稱 make claim`）成功後會自動將 `GATEWAY_TOKEN`、`GATEWAY_URL`、`CLASS_CODE`、`NICKNAME` 寫回專案目錄的 `.env`（同時備份於使用者家目錄 `.hwdc26/token`，POSIX 權限 600），terminal 僅顯示遮罩及台北時間到期日；切勿把 `.env` 或 token 貼到 GitHub 或截圖。過期或被撤銷請洽講師／重新領取。講師備用紙條的 CSV／HTML 含完整 token，**不得 commit**。

可選 `uv run --extra examples python examples/openai_sdk_example.py` 示範 OpenAI SDK 呼叫 `mock` 與 `fast`；`fast` 使用真實上游，請只在講師同意及明確設定後執行。`openai` 非預設依賴。可選 `make install-hooks` 啟用提交前掃描（不會由 `make setup` 自動安裝）；因 L1⑥ 刻意保留假 key，啟用後也會攔截它。`make uninstall-hooks` 移除 hook。詳細格式、限制與外洩流程見 [token 規格](../ops/token-format.md)。

跨平台等價指令：`uv run hw ask "你好"`、`models`、`check L1|L2`、`run`、`report`、`claim`、`submit --dry-run`、`test`、`doctor`。未安裝選配 server 依賴時，`hw test` 自動略過 server 測試；講師驗證使用 `uv run --extra server pytest`。
