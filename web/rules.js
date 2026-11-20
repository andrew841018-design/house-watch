// house_watch 瀏覽器規則（契約 C4）。ES module，不碰 DOM。
//
// 這個檔由 WP8a 擁有；G0 只放佔位：列舉值、原因代碼與 API 形狀，函式尚未實作。
//
// 契約格式（WP0 定）：
// - ENUMS 與 REASONS 的內容要寫成合法 JSON（雙引號、不放註解、不留尾逗號），
//   夾在 /*<contract:名稱>*/ 與 /*</contract:名稱>*/ 兩個標記之間。
// - Python 的契約測試（tests/test_web_contract.py）用正則取出這兩段：
//   ENUMS 必須和 src/house_watch/models.py 的 RULE_ENUMS 完全相同；
//   案例表（test/rules_cases.json）用到的原因代碼都要在 REASONS 裡。
// - 列舉值只由 WP0 新增；原因代碼由 WP8a 維護（每個欄位都有 <欄位>_missing，
//   另有邊界類、衝突類、位置類）。

function deepFreeze(value) {
  if (value && typeof value === "object") {
    Object.values(value).forEach(deepFreeze);
    Object.freeze(value);
  }
  return value;
}

export const ENUMS = deepFreeze(/*<contract:ENUMS>*/{
  "BType": ["大樓", "華廈", "公寓", "透天", "其他"],
  "Elevator": ["yes", "no", "inferred", "unknown"],
  "MainKind": ["main", "main_plus_aux"],
  "Parking": [
    "ramp_flat",
    "ground_flat",
    "lift_flat",
    "flat_unknown_access",
    "mechanical",
    "mixed_with_flat",
    "mixed_no_flat",
    "none",
    "unknown"
  ],
  "Precision": ["site", "door", "community", "road", "none"],
  "RecordStatus": ["active", "gone"],
  "UpBasis": ["official", "estimated", "none"]
}/*</contract:ENUMS>*/);

export const REASONS = deepFreeze(/*<contract:REASONS>*/[
  "age_boundary",
  "group_conflict",
  "loc_conflict",
  "loc_road_mid",
  "price_missing"
]/*</contract:REASONS>*/);

/**
 * 判定一筆紀錄對一組條件的結果（C4）。
 *
 * 順序：區域不符 → 不評估；任一條不符 → fail；否則任一條待確認 → pending；否則 match。
 * 缺值（null、缺欄位、空字串）一律先檢查，最後才比大小（R3-02）。
 *
 * @param {"listing"|"tx"} kind
 * @param {object} rec 匯出紀錄：listings 檔的一個 group，或 tx 檔的一筆；
 *   另外帶 `area`，也就是它所在資料檔的區域 id。
 * @param {object} profile 一組條件（C3）。
 * @param {string} today 台北日期，YYYY-MM-DD。
 * @returns {{status: "match"|"pending"|"fail", reasons: string[]}}
 */
export function evaluate(kind, rec, profile, today) {
  throw new Error("not implemented: evaluate（WP8a）");
}

/**
 * 解析分享片段或 localStorage 的內容（C3：{v: 1, profiles: [...]}）。
 * 有任何不認得的鍵或值就整份拒絕。
 *
 * @param {string} str
 * @returns {{ok: true, profiles: object[]} | {ok: false, error: string}}
 */
export function parseProfiles(str) {
  throw new Error("not implemented: parseProfiles（WP8a）");
}

/**
 * 把條件寫成分享片段與 localStorage 用的字串（C3）。
 *
 * @param {{v: 1, profiles: object[]}} obj
 * @returns {string}
 */
export function serializeProfiles(obj) {
  throw new Error("not implemented: serializeProfiles（WP8a）");
}
