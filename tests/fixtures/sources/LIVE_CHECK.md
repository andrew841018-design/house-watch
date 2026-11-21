# 實際連線驗證（WP5a／5b／5d）

- 日期：2026-11-21（台北）。
- 方法：用本 repo 的 `Fetcher` 與轉接器直接連線（不是瀏覽器）。狀態存在暫存資料庫，跑完即刪。
- User-Agent：`house-watch/0.1.0`，沒有帳號或聯絡方式。
- 禮貌規則：同一主機間隔 ≥ 4 秒；每站最多 5 個請求；robots.txt 由抓取層自動先讀。
- 只記錄數量、排序與是否被擋，不記錄物件標題、經紀人或任何個人資料。
- 查詢範圍：桃園區，開價 ≤ 公開抓取範圍的上限（default.toml `[scope.listing]`），不加其他伺服器端條件。

## 結果

| 站 | 請求數 | 誠實 UA 被擋？ | 排序語意 |
|---|---|---|---|
| 中信 cthouse | 5（robots、前端 JS、列表第 1、2 頁、詳細頁 1） | 否，全部 200 | 預設排序＝`created_date` 新到舊 |
| 住商 hbhousing | 4（robots、列表第 1、2 頁、詳細頁 1） | 否，全部 200 | `sort=8`（上架新到舊）＝刊登日新到舊 |
| 東森 etwarm | 5（robots、CSRF 頁、列表第 1 頁、詳細頁 2 次） | 否，全部 200 | `sort=NEW`＝編號遞減 |

### 中信

- 列表第 1、2 頁：`created_date` 在兩頁內都是 19/19 遞減；第 1 頁最後一筆 ≥ 第 2 頁第一筆；兩頁沒有重複編號。
  編號本身不是遞減（15/19、12/19）。
- 同一個 session 內先讀第 1 頁再讀第 2 頁可以正常翻頁（搜尋狀態在第 1 頁回應設的 cookie）。
- 其他排序（URL 的 `<n>-ord`）的代碼沒有查到：選單定義所在的前端檔回了 50 bytes 的空殼。
  所以完整掃描與探測都先用預設排序。新刊登一定在最前面，探測看得到；掃描途中被刷新的物件
  可能在該次完整掃描漏看，但下架要連續兩次有效完整掃描都沒看到。
- 總數 2,158 筆（所有型態）→ 每頁 20 筆，一次完整掃描 108 頁。
- 詳細頁：「屋齡」X年Y個月解析成功，完工日區間從一年縮到一個月。

### 住商

- `sort=8`、`pageRows=50`：兩頁都滿 50 筆，編號整體由新到舊（同一天的物件之間順序不固定）。
- 總數 2,650 筆（所有型態）→ 一次完整掃描 53 頁。
- 詳細頁：`__NUXT_DATA__`（devalue）解碼成功，讀到完工日（精確到日）、車位型式（坡道平面）、
  電梯（有）、頁面上的「含車位價」。

### 東森

- 不送型態條件（`type=null`）可以用：總數 108 筆（含公寓、土地等）→ 一次完整掃描 11 頁。
- `sort=NEW` 第 1 頁 10 筆編號嚴格遞減。
- CSRF：先讀 `/houses?searchKind=buy` 取 `meta[name=csrf-token]`，同一個 session 內 POST 成功。
- 詳細頁：座標取自物件地圖（`#house-life-tab1`），不是門市地圖。
  有一筆詳細頁的「樓層」「屋齡」「車位」都是「－」（未填），但批次 API 有「20F / 20F」：
  解析結果是未知（正確），樓層要靠批次 API 補。

## robots.txt 內容雜湊（給 `[robots] accepted_hash`）

雜湊算法見 `src/house_watch/sources/robots.py` 的 `robots_digest()`（換行統一、去頭尾空白後的 sha256）。

| 來源 | robots.txt | 雜湊 |
|---|---|---|
| cthouse | `https://buy.cthouse.com.tw/robots.txt` | `16ceb5ee3e0dc13aa9adf31a3ebbe45a1d965b8c2b9f72eaf84e5911e140ed95` |
| hbhousing | `https://www.hbhousing.com.tw/robots.txt` | `16ceb5ee3e0dc13aa9adf31a3ebbe45a1d965b8c2b9f72eaf84e5911e140ed95` |
| etwarm | `https://www.etwarm.com.tw/robots.txt` | `0b3f076e693fb4e22f8189d1ec1fb97c5ada4434472e20c9dac5a42bd285d7b9` |

中信與住商的內容都是 `User-agent: *` / `Allow: /`，所以雜湊相同。

## 對請求預算的影響（只算桃園區）

- 中信：完整掃描 108 頁，超過 default.toml 目前的 `max_requests_full = 60`（佔位值），
  照現在的設定完整掃描會判成 budget_exhausted。
- 住商：53 頁。東森：11 頁。
- 中壢區的頁數這次沒有量。
