"""幾何、門牌索引與定位（工作包 WP1、WP2；契約 C7）。

- `proj`：TWD97／TM2（EPSG:3826）與 WGS84 互換，純 Python。
- `shapes`：GeoJSON 讀取、依屬性挑 feature、投影、一層包一層的車程檔。
- `areas`：區域的幾何與範圍指標（`GeoContext.metrics`、`road_frac`）。
- `doorindex`：桃園市門牌位置坐標資料的下載、檢查、建索引與比對。
- `locate`：一筆物件或成交的區與位置（R3-01 決策表、R1-14／R3-07 精度降級）。
- `community`：社區名定位（OSM 具名建物、他站同社區座標）。
"""
