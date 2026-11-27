# house_watch（桃園找房雷達）

定時整理桃園指定區域的售屋網站開價物件與實價登錄成交，放在一個靜態網頁上：有地圖、清單、篩選，並標出新上架、降價、下架，附近成交可以拿來比價。

**狀態**：已上線使用。每小時抓實價登錄和 5 家房仲網站的公開物件，產生靜態網頁。

## 隱私設計

- 程式只輸出「事實」：價格、坪數、樓層、屋齡區間、位置精度等。
- 個人條件（預算、屋齡、樓層、坪數、距離等）只存在使用者自己的瀏覽器（localStorage 與分享連結的 `#` 片段），Python 程式完全不讀。
- 公開的抓取範圍刻意比任何個人條件寬，只放寬、不收窄。真的要收窄（例如調低開價上限）時，原本在範圍內、
  現在在範圍外的在架物件，會在兩次有效完整掃描後被標成「已下架」（擁有者 2026-11-21 決定維持這個行為）。
- 本機設定與資料放在 platformdirs 的使用者資料夾，不在 repo 裡。
- 推送前可以跑 `python3 scripts/pre_push_check.py <敏感值清單> origin/main..HEAD`：清單放在 repo 外、
  自己維護（一行一個值）；命中時只列出檔名和行號，不印出值。
- 網站放在 `<帳號>.github.io` 底下時，localStorage 是整個 `<帳號>.github.io` 共用的：同帳號的其他
  Pages 網站讀得到存在這裡的條件。這個帳號不要開別的 Pages 網站，或改用自己的網域。

## 開發

需要 [uv](https://docs.astral.sh/uv/) 與 Node.js 20 以上。

```sh
uv sync --locked
uv run pytest
uv run ruff check
node --test web/test/*.test.mjs
```

資料夾位置可以用環境變數改：

- `HOUSE_WATCH_DATA_DIR`：資料夾（資料庫、門牌索引、備份、匯出的網站）。
- `HOUSE_WATCH_CONFIG`：本機設定檔 `settings.toml` 的完整路徑。
- `HOUSE_WATCH_LOG_DIR`：排程的 log（預設是 platformdirs 的 log 資料夾，Mac 是 `~/Library/Logs/house_watch`）。
- `HOUSE_WATCH_RUNTIME_DIR`：排程用的固定版本（預設 Mac 是 `~/Library/Application Support/house_watch_runtime`）。
- `HOUSE_WATCH_LEDGER`：只給測試用。抓取帳本（每站每日請求數、每主機最後請求時間、暫停）是這台電腦
  所有資料夾共用的一份（`/tmp/house_watch-<uid>/`），設了這個變數就不能連網；連網的試跑一定要共用同一份帳本。

匯出 JSON 的 schema 在 `schemas/export.schema.json`，由 `src/house_watch/models.py` 產生：

```sh
uv run python -m house_watch.models > schemas/export.schema.json
```

## 排程（Mac：launchd）

排程只有一個入口 `house-watch tick`。每次執行：

- 先拿執行鎖（和 update、export、publish 同一把），拿不到就記一筆 skipped、結束碼 0。
- 依資料庫紀錄決定到期的工作：門牌每週、實價登錄每天、每個（來源, 區域）每天一次完整掃描、
  之後每 3 小時探測、詳細頁佇列、每日備份（`<資料夾>/backups`，每日 7 份、每週 4 份）、
  每週 integrity_check。日期一律用台北日期，一輪算開始的那一天。
- 大的完整掃描（例如樂屋兩區約 35 分）一輪最多一個，而且先跑；其他放不進這一輪時間的延到下一輪。
- 每一步有自己的截止時間；中途睡著（牆上時鐘跳太多）就安全結束，讓下一輪重來。
- 最後匯出，再依心跳規則發布（見下一節）。`[publish]` 還沒設定時略過發布。
- log 在 `~/Library/Logs/house_watch/tick.log`（每檔 5 MB、保留 5 份）。`launchd.out.log`、
  `launchd.err.log` 只會有 Python 還沒啟動就失敗的錯誤。

排程跑的是**固定版本**，不是開發資料夾（改到一半的程式不會被排程執行）。安裝與升級：

```sh
uv run pytest                                  # 1. 先確認測試通過
git tag v0.1.0                                 # 2. 要安裝的版本打 tag
uv run house-watch deploy-local v0.1.0         # 3. 從 tag 建 wheel，裝進執行區
RT=~/Library/Application\ Support/house_watch_runtime/bin/house-watch
"$RT" doctor                                   # 4. 用固定版本檢查
"$RT" publish --dry-run                        # 5. 看會發布什麼（不寫入、不推送）
"$RT" schedule install                         # 6. 寫 ~/Library/LaunchAgents/com.house-watch.tick.plist
```

- `deploy-local` 用 `git archive` 取出那個 ref（不讀工作區），套件版本照 `uv.lock`，
  Python 用 uv 管理、裝在執行區裡（Homebrew 升級 Python 不會弄壞排程）。
- `deploy-local` 換版時會拿資料夾的執行鎖（和 tick 同一把）：排程正在跑就拒絕，等它跑完再執行。
  要用平常的 `HOUSE_WATCH_*` 環境變數執行它，才找得到正式的資料夾。
- `schedule install` 只寫檔，不載入也不啟動；它會印出要執行的 `launchctl bootstrap gui/<uid> …`。
- 排程不會帶著你終端機的環境變數。`schedule install` 會把安裝當下設了的
  `HOUSE_WATCH_CONFIG`、`HOUSE_WATCH_DATA_DIR`、`HOUSE_WATCH_LOG_DIR`、`HOUSE_WATCH_RUNTIME_DIR`
  換成絕對路徑寫進 plist，所以要用和平常一樣的環境變數執行它。改了這些路徑，要重新執行一次。
- 執行區、log、設定檔、資料夾都不能放在 `~/Desktop`、`~/Documents`、`~/Downloads` 或外接磁碟：
  macOS 的隱私保護會擋住背景程式，`schedule install` 會直接拒絕。
  時段是公開設定的 `[schedule] window_hours`（台北時間），預設每小時一次（08–23 點，一天 16 次；
  探測仍是每 3 小時，由程式依紀錄決定），
  可以用 `--every-hours N` 改；安裝時換成這台電腦的本機時間。
- Mac 睡眠時錯過的那一輪，醒來後 launchd 會補跑一次；關機時錯過的不補，下一個時段自然補上。
- 停用：`house-watch schedule remove` 會刪掉 plist，並印出要執行的 `launchctl bootout` 指令。

### 其他系統

- cron：`0 8-23 * * * /絕對路徑/house-watch tick`（cron 用本機時間；資料夾與設定照 platformdirs，
  或用上面的環境變數指定）。
- Windows 工作排程器：動作是 `house-watch.exe tick`，每天 08:00 觸發、每 1 小時重複一次、持續 15 小時。

## 發布（獨立的網站 repo）

網站放在另一個公開 repo（不是這個程式 repo），由排程用只綁那個 repo 的 deploy key 推送。
設定一次（M4，擁有者同意公開清單之後才做）：

1. 在 GitHub 建網站 repo，開 Pages（main 分支、根目錄）。
2. 產生專用的 deploy key（不設密碼），只加到網站 repo 的 Deploy keys，勾選可寫入：
   `ssh-keygen -t ed25519 -N "" -f <資料夾>/deploy_key`，權限 600。
3. 準備專案自己的 known_hosts：`ssh-keyscan -t ed25519 github.com > <資料夾>/known_hosts`，
   用 `ssh-keygen -lf <資料夾>/known_hosts` 和 GitHub 官方公布的 SSH 指紋逐字核對後才用。
4. 用這把 key clone 網站 repo（不要放進還沒 init 的資料夾）：
   `GIT_SSH_COMMAND='ssh -F /dev/null -i <deploy_key> -o IdentitiesOnly=yes -o UserKnownHostsFile=<known_hosts> -o StrictHostKeyChecking=yes' git clone git@github.com:<帳號>/<網站 repo>.git <clone>`
5. 在 `settings.toml` 寫：

   ```toml
   [publish]
   site_repo_dir = "/絕對路徑/<clone>"
   remote = "git@github.com:<帳號>/<網站 repo>.git"
   deploy_key = "/絕對路徑/deploy_key"
   known_hosts = "/絕對路徑/known_hosts"
   ```

6. `house-watch publish --dry-run`，確認後 `house-watch publish`。之後交給排程。

發布的規則：

- 只發布匯出清單（`export/manifest.json`）上的檔，逐檔核對 sha256；清單外的檔從 clone 刪掉，不用 `git add -A`。
- 資料有變（meta.json 以外的檔，或 meta 的狀態、錯誤代碼等非時間欄位）才 commit；只有時間戳變了時，
  某個來源已公開的「最後成功時間」超過過期門檻的一半、而且這次比較新，才 commit 一次（心跳），
  讓網頁的過期提示可信，又不會在來源停擺時每一輪都 commit。
- commit 的作者固定是 `house-watch <noreply@house-watch.invalid>`，訊息只有時間與 build id。
  git 不讀你的全域設定、hook 與 ssh 設定，只用 deploy key 與上面的 known_hosts。
- 一般 push，絕不 force push。被拒（`push_rejected`）或連不上（`publish_failed`）時，
  commit 留在 clone 裡，下一輪自動再推。
- deploy key 推得到網站 repo 的所有檔（包含網頁程式），這是設計上接受的事；它碰不到這個程式 repo。

### 出事時

1. 先停排程（`house-watch schedule remove`，並執行它印出的 `launchctl bootout` 指令）；
   到 GitHub 刪掉網站 repo 的 deploy key，再刪本機的金鑰檔。
2. 再評估：刪掉網站 repo 重建，必要時請 GitHub 清除快取。
3. 已經被 fork、clone 或快取的副本收不回來。
