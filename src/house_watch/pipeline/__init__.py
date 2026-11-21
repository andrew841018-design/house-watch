"""開價物件的流程（WP6）：掃描、版本、下架、詳細頁佇列、跨站合併。

- `scan`：每日完整掃描與每 3 小時的探測（r4 §3.4），update 的 sources 步驟入口 `run(ctx)`，
  以及 `--accept-scan` 的入口 `accept_scans(ctx)`。
- `versions`：刊登狀態表（listings）與 append-only 的版本表（listing_versions），價格歷史。
- `delist`：下架＝同一（來源, 區域, 範圍版本）連續兩次有效完整掃描都沒看到（R1-10）。
- `detail_queue`：詳細頁依首次出現的先後排隊，只看公開範圍（R1-21）。
- `dedupe`：跨站合併（complete linkage，R1-07、R1-08）與匯出用的 `export_groups`。
- `locating`：匯出時的定位與範圍指標（物件與成交共用）。
"""
