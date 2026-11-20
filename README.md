# house_watch（桃園找房雷達）

定時整理桃園指定區域的售屋網站開價物件與實價登錄成交，放在一個靜態網頁上：有地圖、清單、篩選，並標出新上架、降價、下架，附近成交可以拿來比價。

**狀態**：骨架階段（G0），只有資料契約、設定、資料庫結構與測試，還不能抓資料。

## 隱私設計

- 程式只輸出「事實」：價格、坪數、樓層、屋齡區間、位置精度等。
- 個人條件（預算、屋齡、樓層、坪數、距離等）只存在使用者自己的瀏覽器（localStorage 與分享連結的 `#` 片段），Python 程式完全不讀。
- 公開的抓取範圍刻意比任何個人條件寬，只放寬、不收窄。
- 本機設定與資料放在 platformdirs 的使用者資料夾，不在 repo 裡。

## 開發

需要 [uv](https://docs.astral.sh/uv/) 與 Node.js 20 以上。

```sh
uv sync --locked
uv run pytest
uv run ruff check
node --test web/test/*.test.mjs
```

資料夾位置可以用環境變數改：

- `HOUSE_WATCH_DATA_DIR`：資料夾（資料庫、門牌索引、log、備份）。
- `HOUSE_WATCH_CONFIG`：本機設定檔 `settings.toml` 的完整路徑。

匯出 JSON 的 schema 在 `schemas/export.schema.json`，由 `src/house_watch/models.py` 產生：

```sh
uv run python -m house_watch.models > schemas/export.schema.json
```
