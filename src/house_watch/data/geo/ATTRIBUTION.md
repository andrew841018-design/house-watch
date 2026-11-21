# geo 資料的來源與授權

這個資料夾的 GeoJSON 都是 WGS84（EPSG:4326）。程式載入後投影到 TWD97／TM2（EPSG:3826）計算距離與面積。

- 複製進來時沒有改動任何幾何。
- 屬性只做兩件事：
  - 刪掉本機檔案路徑（這一版沒有找到）。
  - 車程檔的 `description`、`confidence_note` 改寫成不指向任何一個門檻的中性說明。
- feature 一律依屬性挑選，不依順序，例如藝文里用 `name` 挑。
- 含 OpenStreetMap 衍生內容的檔案，整檔以 ODbL 1.0 提供，© OpenStreetMap contributors。

## 授權與出處

| 檔案 | 內容 | 來源 | 授權 | 做法 | 可信度 |
|---|---|---|---|---|---|
| `rca_restricted_area.geojson` | RCA 原桃園廠地下水污染管制區（52.3 公頃） | 環境部開放資料「地下水污染管制區範圍圖」GISEPA_P_32，2026 年 10 月版（場址代碼 H00001） | 政府資料開放授權條款－第 1 版 | 官方向量原樣轉出 | high |
| `rca_site.geojson` | ① 官方場址代表點<br>② 場址輪廓 | ① 環境部「土壤及地下水污染場址位置圖」GISEPA_P_24，2026 年 8 月版<br>② OSM way 505528663 | ① 政府資料開放授權條款－第 1 版<br>② ODbL 1.0 | ① 原樣<br>② OSM 社群繪製，原樣 | ① high<br>② medium |
| `zhonglu.geojson` | 中路特區：中路地區整體開發計畫範圍外框 | 細部計畫書（桃園縣政府 2009）的範圍文字與圖 3，加上 OSM 道路中心線 | ODbL 1.0（幾何含 OSM 衍生） | 沿路的邊用 OSM 中心線，其餘邊從計畫書圖數化，6 點仿射校正，RMSE 約 11 m；含半個路寬 | medium |
| `yiwen.geojson` | 藝文特區三個圖層：①核心街廓、②廣義、③藝文里。程式只用 ③ | ① 觀光行銷局頁面（2012 封存）的四至，加 OSM 中心線<br>② 維基百科的四至（次要來源），加 OSM 中心線<br>③ OSM relation 20277432（2026-03-01 新設之里） | ODbL 1.0 | ①② polygonize 道路中心線<br>③ OSM 行政界原樣 | ① medium<br>② low<br>③ medium |
| `zhongli_district.geojson` | 中壢區界 | OSM relation 1526714（admin_level 7，版本 63） | ODbL 1.0 | Nominatim 的 polygon_geojson，原樣 | medium |
| `zhongli_ic_drive.geojson` | 到內壢、中豐、中壢交流道上匝道入口的反向車程範圍：6／8／10／12 分鐘，各交流道與聯集 | Valhalla 3.9.3（FOSSGIS 公開站），路網為 2026-11-20 的 OSM | ODbL 1.0（OSM 衍生） | 反向等時線，封鎖範圍內的國道出口匝道；自由車流；未裁切。程式載入時強制一層包一層（R3-04） | medium |
| `zhongli_ic_points.geojson` | 9 個上匝道入口點 | OSM（Overpass，osm_base 2026-11-20） | ODbL 1.0 | 地面道路 → 匝道 → 國道主線的拓撲搜尋 | medium |
| `taoyuan_district.geojson` | 桃園區界 | OSM relation 2665196（admin_level 7，版本 51），Overpass osm_base 2026-11-21T02:31:18Z | ODbL 1.0 | relation 的 outer 成員 polygonize，座標取到 1e-7 度 | medium |

### `taoyuan_district.geojson` 的用途

- 把 taoyuan_core 的周邊範圍裁到桃園區（R1-15、R3-21、PLAN_v2 §4 第 4 項）：
  周邊 1 公里的選項跨進蘆竹區的那一小塊裁掉，地圖上畫出裁切線並註明。
- 也是定位的多邊形證據之一（離區界 100 公尺以上才算，R3-01）。

取得時的檢查：

- 單一多邊形，面積 34.374 km²；中路特區與藝文里整個在裡面；和中壢區界、蘆竹區與八德區相鄰的里沒有重疊。
- 用 115 年 10 月版門牌檔核對：桃園區 226,994 列門牌有 226,879 列（99.95%）在多邊形內；
  其他區的門牌落在裡面的，每區最多 185 列（OSM 區界和戶政點位在區界附近的小差異）。

## 門牌位置坐標資料（執行時下載，不隨套件散布）

程式會從 data.gov.tw 下載「桃園市門牌位置坐標資料」（資料集 157689），在本機建索引。使用與再散布時，依授權條款附件的格式標示出處，例如：

> 桃園市政府民政局 2026 桃園市門牌位置坐標資料（115年10月版）。此開放資料依政府資料開放授權條款（Open Government Data License）進行公眾釋出，使用者於遵守本條款各項規定之前提下，得利用之。政府資料開放授權條款：https://data.gov.tw/license

年份與版本請換成實際使用的那一期。索引的 `meta` 表記有版本與下載網址。

## OSM 社區名稱表（手動更新，不隨套件散布）

- `community.refresh_osm_names()` 從 Overpass 抓具名的住宅物件，存在本機資料夾的 `osm_names.json`。
- 資料是 ODbL 1.0，© OpenStreetMap contributors。
