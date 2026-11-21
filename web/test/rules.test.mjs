// WP8a／WP8b 的測試：規則案例表、條件讀寫、分享片段、搜尋連結、新鮮度、比價、徽章，
// 以及網頁的靜態安全檢查（R2-01）、Leaflet 檔案的完整性、開發用示意資料。
// 執行：node --test web/test/*.test.mjs。test/selftest.html 在瀏覽器裡跑同一份案例表。
// 門檻一律用示意值，不放任何人的條件。
import { test } from "node:test";
import assert from "node:assert/strict";
import { createHash } from "node:crypto";
import { existsSync, readFileSync, readdirSync } from "node:fs";

import * as links from "../deeplinks.js";
import * as rules from "../rules.js";

const here = (rel) => new URL(rel, import.meta.url);
const read = (rel) => readFileSync(here(rel), "utf8");
const table = JSON.parse(read("./rules_cases.json"));
const byName = new Map(table.cases.map((item) => [item.name, item]));
const fixtureMeta = JSON.parse(read("./fixtures/sample_data/meta.json"));
const AREAS = fixtureMeta.areas;

function deepFreeze(value) {
  if (value && typeof value === "object") {
    Object.values(value).forEach(deepFreeze);
    Object.freeze(value);
  }
  return value;
}

const seedBuffer = byName.get("seed_tx_all_criteria_match").profile;
const seedDrive = byName.get("seed_listing_age_boundary_pending").profile;
const good = { v: 1, profiles: [seedBuffer, seedDrive] };

// ---------------------------------------------------------------- 案例表（C4）

test("rules_cases.json：每一筆案例", async (t) => {
  assert.ok(table.cases.length >= 40);
  for (const item of table.cases) {
    await t.test(item.name, () => {
      assert.deepEqual(rules.evaluate(item.kind, item.rec, item.profile, item.today), item.expect);
    });
  }
});

test("NaN 與 Infinity 也是缺值（由 *_empty 案例推出，R3-02）", () => {
  const derived = rules.nanCases(table);
  assert.ok(derived.length >= 90, `只推出 ${derived.length} 筆`);
  for (const item of derived) {
    assert.deepEqual(rules.evaluate(item.kind, item.rec, item.profile, item.today), item.expect, item.name);
  }
});

test("案例表用到每一個原因代碼，原因代碼都有說明文字", () => {
  const used = new Set(table.cases.flatMap((item) => item.expect.reasons));
  for (const reason of rules.REASONS) {
    assert.ok(used.has(reason), `沒有案例用到 ${reason}`);
    assert.equal(typeof rules.REASON_LABELS[reason], "string", reason);
  }
  assert.deepEqual(Object.keys(rules.REASON_LABELS).sort(), [...rules.REASONS].sort());
});

test("規則用到的每個匯出欄位都有 null、缺欄位、空字串三種缺值案例（r4 §3.3）", () => {
  const groups = {
    missing_listing: [
      "price_min",
      "facts_price_excl_parking",
      "facts_main_cp",
      "facts_main_kind",
      "facts_floor_lo",
      "facts_floor_hi",
      "facts_built_lo",
      "facts_built_hi",
      "facts_elevator",
      "facts_parking",
      "facts_parking_public",
      "facts",
      "loc",
      "loc_precision",
      "loc_conflict",
      "metrics",
      "metrics_d_zhonglu_m",
      "metrics_d_yiwen_m",
      "metrics_d_rca_m",
      "metrics_in_district",
    ],
    missing_listing_excl: ["facts_parking_price_yuan"],
    missing_listing_nomain: ["facts_title_area_cp"],
    missing_listing_road: ["metrics_road_frac"],
    missing_listing_drive: ["metrics_in_district", "metrics_drive_min"],
    missing_listing_drive_road: ["metrics_road_frac"],
    missing_tx: [
      "date",
      "built_lo",
      "built_hi",
      "floor_lo",
      "floor_hi",
      "main_cm2",
      "elevator",
      "parking",
      "parking_0_ptype",
      "up",
      "up_basis",
      "up_q_lo",
      "up_q_hi",
      "up_k_lo",
      "up_k_hi",
      "precision",
      "metrics",
      "metrics_d_zhonglu_m",
      "metrics_d_yiwen_m",
      "metrics_d_rca_m",
    ],
    missing_tx_drive: ["metrics_in_district", "metrics_drive_min"],
  };
  for (const [prefix, fields] of Object.entries(groups)) {
    for (const field of fields) {
      for (const variant of ["null", "absent", "empty"]) {
        assert.ok(byName.has(`${prefix}_${field}_${variant}`), `${prefix}_${field}_${variant}`);
      }
    }
  }
});

test("缺值不會讓上限型的條件通過（R3-02 的反例）", () => {
  // 開價缺值、車程缺值、單價缺值，都不能變成「符合」。
  for (const name of [
    "missing_listing_price_min_null",
    "missing_listing_drive_metrics_drive_min_absent",
    "missing_tx_up_q_hi_null",
    "missing_tx_drive_metrics_in_district_null",
  ]) {
    const item = byName.get(name);
    assert.notEqual(rules.evaluate(item.kind, item.rec, item.profile, item.today).status, "match", name);
  }
});

test("evaluate 不改動傳入的資料", () => {
  for (const item of table.cases) {
    const rec = deepFreeze(structuredClone(item.rec));
    const profile = deepFreeze(structuredClone(item.profile));
    assert.deepEqual(rules.evaluate(item.kind, rec, profile, item.today), item.expect, item.name);
  }
});

test("條件或參數不合格式時 evaluate 丟出 TypeError，不會默默少評一條", () => {
  const base = byName.get("seed_tx_all_criteria_match");
  const badProfiles = [
    { ...seedBuffer, price_max: 1234 },
    { ...seedBuffer, price_max_wan: Number.NaN },
    { ...seedBuffer, unit_price_max_wan: "29" },
    { ...seedBuffer, near_m: undefined },
    { ...seedBuffer, drive_min: 10 },
    { ...seedBuffer, built_years: [9, 2] },
  ];
  for (const profile of badProfiles) {
    assert.throws(() => rules.evaluate("tx", base.rec, profile, base.today), TypeError);
  }
  assert.throws(() => rules.evaluate("house", base.rec, seedBuffer, base.today), TypeError);
  assert.throws(() => rules.evaluate("tx", base.rec, seedBuffer, "2026-6-15"), TypeError);
  assert.throws(() => rules.evaluate("tx", null, seedBuffer, base.today), TypeError);
});

test("日曆計算：2 月 29 日加年份用 2 月 28 日；往回算月份遇到月底用月底", () => {
  const date = (text) => rules.parseDate(text);
  assert.equal(rules.formatDate(rules.addYears(date("2016-02-29"), 10)), "2026-02-28");
  assert.equal(rules.formatDate(rules.addYears(date("2016-02-29"), 4)), "2020-02-29");
  assert.equal(rules.formatDate(rules.addYears(date("2016-02-29"), -1)), "2015-02-28");
  assert.equal(rules.formatDate(rules.addMonths(date("2026-05-31"), -3)), "2026-02-28");
  assert.equal(rules.formatDate(rules.addMonths(date("2026-01-15"), -1)), "2025-12-15");
  assert.equal(rules.formatDate(rules.addMonths(date("2024-02-29"), -24)), "2022-02-28");
  assert.equal(rules.parseDate("2026-02-29"), null);
  assert.equal(rules.parseDate("2026-13-01"), null);
  assert.equal(rules.parseDate("20260101"), null);
  assert.equal(rules.taipeiDate(Date.UTC(2026, 10, 20, 16, 0)), "2026-11-21");
  assert.equal(rules.taipeiDate(Date.UTC(2026, 10, 20, 15, 59)), "2026-11-20");
});

// ---------------------------------------------------------------- 條件（C3）

test("條件寫出再讀回結果一樣；鍵順序固定，沒設定的鍵不寫出", () => {
  const text = rules.serializeProfiles(good);
  const parsed = rules.parseProfiles(text);
  assert.equal(parsed.ok, true);
  assert.equal(rules.serializeProfiles({ v: 1, profiles: parsed.profiles }), text);
  assert.deepEqual(Object.keys(JSON.parse(text)), ["v", "profiles"]);
  assert.deepEqual(Object.keys(JSON.parse(text).profiles[1]), [
    "name",
    "area",
    "price_max_wan",
    "built_years",
    "floor",
    "main_min_ping",
    "elevator",
    "parking_ok",
    "lift_flat_ok",
    "drive_min",
    "unit_price_max_wan",
    "tx_months",
  ]);
  const [buffer, drive] = parsed.profiles;
  assert.deepEqual(Object.keys(buffer), [...rules.PROFILE_KEYS]);
  assert.equal(buffer.drive_min, null);
  assert.equal(drive.near_m, null);
  assert.ok(Object.isFrozen(buffer) && Object.isFrozen(buffer.floor) && Object.isFrozen(buffer.elevator));
  assert.equal(rules.profileAreaKind(buffer), "buffer");
  assert.equal(rules.profileAreaKind(drive), "drive");
});

test("案例表裡每一組條件都能寫出再讀回", () => {
  for (const item of table.cases) {
    const text = rules.serializeProfiles({ v: 1, profiles: [item.profile] });
    const parsed = rules.parseProfiles(text);
    assert.equal(parsed.ok, true, item.name);
    assert.equal(rules.serializeProfiles({ v: 1, profiles: parsed.profiles }), text, item.name);
  }
});

test("有不認得的鍵或不合法的值就整份拒絕（r4 §2.2 #8）", () => {
  const p = seedBuffer;
  const d = seedDrive;
  const many = Array.from({ length: rules.MAX_PROFILES + 1 }, (_, index) => ({ ...p, name: `示意${index}` }));
  const rejects = [
    ["打錯的鍵", { v: 1, profiles: [{ ...p, price_max: 1234 }] }],
    ["多一個最外層的鍵", { v: 1, profiles: [p], extra: true }],
    ["版本 2", { v: 2, profiles: [p] }],
    ["沒有版本", { profiles: [p] }],
    ["profiles 不是清單", { v: 1, profiles: {} }],
    ["條件不是物件", { v: 1, profiles: [[1, 2]] }],
    ["數字寫成文字", { v: 1, profiles: [{ ...p, price_max_wan: "1234" }] }],
    ["負的開價", { v: 1, profiles: [{ ...p, price_max_wan: -1 }] }],
    ["三位小數", { v: 1, profiles: [{ ...p, price_max_wan: 1234.567 }] }],
    ["屋齡顛倒", { v: 1, profiles: [{ ...p, built_years: [9, 2] }] }],
    ["屋齡相等", { v: 1, profiles: [{ ...p, built_years: [5, 5] }] }],
    ["屋齡有小數", { v: 1, profiles: [{ ...p, built_years: [2.5, 9] }] }],
    ["屋齡三個值", { v: 1, profiles: [{ ...p, built_years: [2, 9, 12] }] }],
    ["樓層顛倒", { v: 1, profiles: [{ ...p, floor: [9, 4] }] }],
    ["電梯少一個鍵", { v: 1, profiles: [{ ...p, elevator: { required: true } }] }],
    ["電梯多一個鍵", { v: 1, profiles: [{ ...p, elevator: { required: true, inferred_ok: true, x: 1 } }] }],
    ["電梯不是是非值", { v: 1, profiles: [{ ...p, elevator: { required: "yes", inferred_ok: true } }] }],
    ["車位放推出來的類型", { v: 1, profiles: [{ ...p, parking_ok: ["flat_unknown_access"] }] }],
    ["升降平面放在清單裡", { v: 1, profiles: [{ ...p, parking_ok: ["lift_flat"] }] }],
    ["車位放不明", { v: 1, profiles: [{ ...p, parking_ok: ["unknown"] }] }],
    ["車位重複", { v: 1, profiles: [{ ...p, parking_ok: ["ramp_flat", "ramp_flat"] }] }],
    ["車位不是清單", { v: 1, profiles: [{ ...p, parking_ok: "ramp_flat" }] }],
    ["升降平面選項是文字", { v: 1, profiles: [{ ...p, lift_flat_ok: "true" }] }],
    ["只設周邊沒設 RCA", { v: 1, profiles: [{ ...p, rca_m: null }] }],
    ["周邊和車程混用", { v: 1, profiles: [{ ...p, drive_min: 10 }] }],
    ["沒有區域門檻", { v: 1, profiles: [{ ...p, near_m: null, rca_m: null }] }],
    ["距離有小數", { v: 1, profiles: [{ ...p, near_m: 300.5 }] }],
    ["車程 0 分鐘", { v: 1, profiles: [{ ...d, drive_min: 0 }] }],
    ["區域代號大寫", { v: 1, profiles: [{ ...p, area: "Zhongli_IC" }] }],
    ["名稱空白", { v: 1, profiles: [{ ...p, name: "" }] }],
    ["名稱太長", { v: 1, profiles: [{ ...p, name: "長".repeat(41) }] }],
    ["名稱前面有空白", { v: 1, profiles: [{ ...p, name: " 示意" }] }],
    ["名稱有控制字元", { v: 1, profiles: [{ ...p, name: `示意${String.fromCharCode(7)}` }] }],
    ["名稱有改變文字方向的字元", { v: 1, profiles: [{ ...p, name: `示意${String.fromCharCode(0x202e)}` }] }],
    ["成交月數 0", { v: 1, profiles: [{ ...p, tx_months: 0 }] }],
    ["成交月數太大", { v: 1, profiles: [{ ...p, tx_months: 241 }] }],
    ["名稱重複", { v: 1, profiles: [p, p] }],
    ["太多組", { v: 1, profiles: many }],
  ];
  for (const [label, obj] of rejects) {
    const result = rules.parseProfiles(JSON.stringify(obj));
    assert.equal(result.ok, false, label);
    assert.equal(typeof result.error, "string", label);
    assert.throws(() => rules.serializeProfiles(obj), Error, label);
  }
});

test("serializeProfiles 不會把 NaN、Infinity 寫成 null（那會讓條件消失）", () => {
  assert.throws(() => rules.serializeProfiles({ v: 1, profiles: [{ ...seedBuffer, price_max_wan: Number.NaN }] }));
  assert.throws(() => rules.serializeProfiles({ v: 1, profiles: [{ ...seedBuffer, near_m: Number.POSITIVE_INFINITY }] }));
});

test("parseProfiles 拒絕非文字、太長、不是 JSON 的輸入", () => {
  for (const input of [null, 42, {}, "", "{", "not json", "x".repeat(rules.MAX_PROFILE_TEXT + 1), "[]", "null"]) {
    assert.equal(rules.parseProfiles(input).ok, false);
  }
  assert.deepEqual(rules.parseProfiles('{"v":1,"profiles":[]}'), { ok: true, profiles: [] });
});

test("錯誤訊息不引用外來的鍵名或值（R2-01）", () => {
  const evil = "<img src=x onerror=alert(1)>";
  const unknownKey = rules.parseProfiles(JSON.stringify({ v: 1, profiles: [{ ...seedBuffer, [evil]: 1 }] }));
  const badArea = rules.parseProfiles(JSON.stringify({ v: 1, profiles: [{ ...seedBuffer, area: evil }] }));
  const topKey = rules.parseProfiles(JSON.stringify({ v: 1, profiles: [], [evil]: 1 }));
  for (const result of [unknownKey, badArea, topKey]) {
    assert.equal(result.ok, false);
    assert.ok(!result.error.includes("img") && !result.error.includes("onerror"), result.error);
  }
  // 名稱本身可以是任何文字：網頁只用 textContent 顯示。
  assert.equal(rules.parseProfiles(JSON.stringify({ v: 1, profiles: [{ ...seedBuffer, name: evil }] })).ok, true);
});

test("傳入 meta 的 areas 時，檢查區域代號、區域種類與門檻選項（R3-16）", () => {
  assert.equal(rules.parseProfiles(rules.serializeProfiles(good), { areas: AREAS }).ok, true);
  const rejects = [
    { ...seedBuffer, area: "nowhere" },
    { ...seedBuffer, area: "zhongli_ic" },
    { ...seedDrive, area: "taoyuan_core" },
    { ...seedBuffer, near_m: 400 },
    { ...seedDrive, drive_min: 7 },
  ];
  for (const profile of rejects) {
    const result = rules.parseProfiles(JSON.stringify({ v: 1, profiles: [profile] }), { areas: AREAS });
    assert.equal(result.ok, false, JSON.stringify(profile));
  }
});

// ---------------------------------------------------------------- 分享片段

test("分享片段 #p=<base64url(JSON)>：寫出後讀回，壞掉的片段整份拒絕且不回顯", () => {
  const fragment = rules.profilesToFragment(good);
  assert.match(fragment, /^#p=[A-Za-z0-9_-]+$/);
  const decoded = rules.readFragment(fragment);
  assert.equal(decoded.present, true);
  assert.equal(decoded.ok, true);
  assert.equal(decoded.text, rules.serializeProfiles(good));
  assert.equal(rules.parseProfiles(decoded.text).ok, true);

  assert.deepEqual(rules.readFragment(""), { present: false });
  assert.deepEqual(rules.readFragment("#map"), { present: false });
  assert.deepEqual(rules.readFragment(undefined), { present: false });
  const longBody = "A".repeat(40000);
  for (const bad of ["#p=", "#p=@@@", "#p=a", "#p=__4", `#p=${longBody}`, "#p=abc def"]) {
    const result = rules.readFragment(bad);
    assert.equal(result.present, true, bad);
    assert.equal(result.ok, false, bad);
    assert.ok(!result.error.includes(bad.slice(3, 8) || "@@@"), result.error);
  }
  // 合法的 base64url 但不是 JSON：交給 parseProfiles 拒絕。
  const notJson = rules.readFragment("#p=bm90IGpzb24");
  assert.equal(notJson.ok, true);
  assert.equal(rules.parseProfiles(notJson.text).ok, false);
});

// ---------------------------------------------------------------- 連結（deeplinks.js）

test("物件連結由「來源＋編號」套樣板產生，格式不對就不給連結", () => {
  assert.equal(links.listingUrl("cthouse", "2145001"), "https://buy.cthouse.com.tw/house/2145001.html");
  assert.equal(links.listingUrl("hbhousing", "CS900003"), "https://www.hbhousing.com.tw/detail?sn=CS900003");
  assert.equal(links.listingUrl("rakuya", "0a1b2c3d4e5f601"), "https://www.rakuya.com.tw/sell_item/info?ehid=0a1b2c3d4e5f601");
  assert.equal(links.listingUrl("etwarm", "773004"), "https://www.etwarm.com.tw/houses/buy/773004");
  assert.equal(links.listingUrl("century21", "7600005"), "https://www.century21.com.tw/buypage/7600005");
  const bad = [
    ["cthouse", "12a"],
    ["cthouse", "../1"],
    ["hbhousing", "CS 1"],
    ["rakuya", "javascript:alert(1)"],
    ["etwarm", ""],
    ["othersite", "1"],
    ["__proto__", "1"],
    ["constructor", "1"],
    [null, "1"],
    ["cthouse", 123],
  ];
  for (const [src, id] of bad) assert.equal(links.listingUrl(src, id), null, `${src}/${id}`);
  assert.equal(links.sourceLabel("cthouse"), "中信房屋");
  assert.equal(links.sourceLabel("toString"), null);
});

test("外部網址只允許白名單上的 https 主機；連結加 noopener noreferrer", () => {
  for (const url of [
    "http://buy.cthouse.com.tw/house/1.html",
    "javascript:alert(1)",
    "data:text/html,hi",
    "https://evil.example/",
    "https://user:pw@buy.cthouse.com.tw/",
    "https://buy.cthouse.com.tw:8443/",
    "//buy.cthouse.com.tw/house/1.html",
    "https://buy.cthouse.com.tw.evil.example/",
  ]) {
    assert.equal(links.safeExternalUrl(url), null, url);
  }
  assert.equal(links.safeExternalUrl("https://www.openstreetmap.org/copyright"), "https://www.openstreetmap.org/copyright");
  assert.equal(links.LINK_REL, "noopener noreferrer");
  for (const host of links.ALLOWED_HOSTS) assert.match(host, /^[a-z0-9.-]+$/);
});

test("591 搜尋網址只在瀏覽器依條件組出，只用紀錄過的參數（R2-04）", () => {
  const [buffer, drive] = rules.parseProfiles(rules.serializeProfiles(good)).profiles;
  assert.equal(
    links.search591Url(buffer),
    "https://sale.591.com.tw/?shType=list&regionid=6&section=73&kind=9&shape=2,5&price=$_$1234&houseage=$1_$9&floor=$4_$9&mainarea=$23_$&order=posttime_desc",
  );
  assert.match(links.search591Url(drive), /[?&]section=67&/);
  assert.equal(
    links.search591Url({ area: "taoyuan_core" }),
    "https://sale.591.com.tw/?shType=list&regionid=6&section=73&kind=9&shape=1,2,5&order=posttime_desc",
  );
  const loose = links.search591Url({ ...buffer, price_max_wan: 1234.5, main_min_ping: 23.75, built_years: [0, 5] });
  assert.match(loose, /price=\$_\$1235&houseage=\$_\$5&/);
  assert.match(loose, /mainarea=\$23_\$/);
  assert.doesNotMatch(loose, /parking=/);
  assert.equal(links.search591Url({ area: "nowhere" }), null);
});

test("信義搜尋網址只用驗證過的路徑段", () => {
  const [buffer, drive] = rules.parseProfiles(rules.serializeProfiles(good)).profiles;
  assert.equal(links.searchSinyiUrl(buffer), "https://www.sinyi.com.tw/buy/list/min-1234-price/dalou-huaxia-type/Taoyuan-city/330-zip/1");
  assert.equal(links.searchSinyiUrl(drive), "https://www.sinyi.com.tw/buy/list/min-1234-price/dalou-huaxia-type/Taoyuan-city/320-zip/1");
  assert.equal(links.searchSinyiUrl({ area: "taoyuan_core" }), "https://www.sinyi.com.tw/buy/list/dalou-huaxia-type/Taoyuan-city/330-zip/1");
  assert.equal(links.searchSinyiUrl({ area: "nowhere" }), null);
});

// ---------------------------------------------------------------- 新鮮度、比價、徽章

test("新鮮度依各來源的門檻與現在時間判斷（R1-17、R2-15）", () => {
  const now = Date.parse("2026-11-21T02:00:00Z");
  const fresh = rules.freshness(fixtureMeta, now);
  const source = Object.fromEntries(fresh.sources.map((item) => [item.id, item]));
  assert.equal(source.cthouse.stale, false);
  assert.equal(source.hbhousing.stale, true);
  assert.equal(source.hbhousing.errorCode, "http_5xx");
  assert.equal(source.etwarm.paused, true);
  assert.equal(source.century21.publish, false);
  const dataset = Object.fromEntries(fresh.datasets.map((item) => [item.id, item]));
  assert.equal(dataset.lvr.stale, false);
  assert.equal(dataset.lvr.asOf, "2026-10-31");
  assert.equal(dataset.doorindex.stale, false);
  assert.equal(fresh.generated.stale, false);
  assert.equal(fresh.generated.limitHours, 12);

  const late = rules.freshness({ ...fixtureMeta, generated_at: "2026-11-20T13:00:00Z" }, now);
  assert.equal(late.generated.stale, true);
  const never = rules.freshness({ sources: [{ id: "x", publish: true, last_ok: null }], freshness_hours: { x: 12 } }, now);
  assert.equal(never.sources[0].stale, true);
  const noLimit = rules.freshness({ sources: [{ id: "y", publish: true, last_ok: "2026-11-21T01:00:00Z" }], freshness_hours: {} }, now);
  assert.equal(noLimit.sources[0].stale, null);
  assert.deepEqual(rules.freshness(null, now).sources, []);
});

test("比價：300 m、完工日 ±3 年、最近 N 個月，含估算與不含估算兩種中位數（R1-23）", () => {
  const groups = JSON.parse(read("./fixtures/sample_data/listings_taoyuan_core.json")).groups;
  const txs = JSON.parse(read("./fixtures/sample_data/tx_taoyuan_core.json")).tx;
  const listing = groups.find((group) => group.gid === 1);
  const result = rules.comps(listing, txs, { today: "2026-11-21", months: 12 });
  assert.equal(result.ok, true);
  assert.deepEqual(
    result.items.map((item) => item.tx.serial),
    ["FXTAO0001", "FXTAO0002", "FXTAO0004", "FXTAO0003"],
  );
  assert.ok(result.items.every((item) => item.distanceM <= 300));
  const unit = (q, k) => (4 * q) / (121 * k);
  const values = result.items.map((item) => {
    const up = item.tx.up;
    return unit((up.q_lo + up.q_hi) / 2, (up.k_lo + up.k_hi) / 2);
  });
  assert.equal(result.n, 4);
  assert.equal(result.nOfficial, 2);
  assert.equal(result.medianAll, rules.median(values));
  assert.equal(
    result.medianOfficial,
    rules.median(result.items.filter((item) => item.basis === "official").map((item) => item.unit)),
  );
  // 月數放寬到 24 個月，較早的那筆官方拆價成交就進來了。
  assert.equal(rules.comps(listing, txs, { today: "2026-11-21", months: 24 }).items[0].tx.serial, "FXTAO0006");
  for (const gid of [2, 7, 8]) {
    const other = groups.find((group) => group.gid === gid);
    assert.deepEqual(rules.comps(other, txs, { today: "2026-11-21" }), { ok: false, reason: "precision" });
  }
  const noBuilt = { ...listing, facts: { ...listing.facts, built_lo: null, built_hi: null } };
  assert.deepEqual(rules.comps(noBuilt, txs, { today: "2026-11-21" }), { ok: false, reason: "built" });
});

test("徽章：新上架看首次出現時間、基準不標；降價只看刊登自己的價格歷史（R1-09、R1-22）", () => {
  const now = Date.parse("2026-11-21T02:00:00Z");
  const groups = JSON.parse(read("./fixtures/sample_data/listings_taoyuan_core.json")).groups;
  const badges = (gid) => rules.listingBadges(groups.find((group) => group.gid === gid), now);
  assert.ok(badges(2).includes("new"));
  assert.ok(!badges(1).includes("new"), "基準群組不標新上架");
  assert.ok(badges(1).includes("price_drop"));
  assert.deepEqual(badges(3).slice(0, 1), ["gone"]);
  assert.ok(!badges(3).includes("price_drop"), "下架的刊登不再標降價");
  assert.ok(badges(10).includes("relisted"));
  assert.ok(badges(2).includes("approx_location") && badges(2).includes("inferred_elevator"));
  assert.equal(rules.BADGE_LABELS.gone, "下架（列表連續兩次未見）");

  const base = groups.find((group) => group.gid === 2);
  const old = { ...base, first_seen: "2026-11-18T00:00:00Z" };
  assert.ok(!rules.listingBadges(old, now).includes("new"), "超過 72 小時不算新上架");
  const original = {
    ...base,
    members: [{ ...base.members[0], price: 13200000, price_first: 13200000, site_original_price: 15000000 }],
  };
  assert.ok(!rules.listingBadges(original, now).includes("price_drop"), "站方原價不產生降價");
  // 事後審查 CE-1：差不到 1 萬不算一次變動（R3-22），和 Python 的 price_summary 同一條規則。
  const withPrices = (price, first) => ({ ...base, members: [{ ...base.members[0], price, price_first: first }] });
  assert.ok(!rules.listingBadges(withPrices(12336000, 12340000), now).includes("price_drop"), "只降 4 千不算降價");
  assert.ok(rules.listingBadges(withPrices(12330000, 12340000), now).includes("price_drop"), "剛好降 1 萬算降價");

  const txs = JSON.parse(read("./fixtures/sample_data/tx_taoyuan_core.json")).tx;
  assert.ok(rules.txBadges(txs.find((tx) => tx.serial === "FXTAO0002")).includes("estimated"));
  assert.ok(!rules.txBadges(txs.find((tx) => tx.serial === "FXTAO0001")).includes("estimated"));
  for (const group of groups) {
    for (const code of rules.listingBadges(group, now)) assert.equal(typeof rules.BADGE_LABELS[code], "string", code);
  }
});

test("runCases 會回報不一致或丟例外的案例（selftest.html 靠它）", () => {
  const item = structuredClone(byName.get("seed_tx_all_criteria_match"));
  assert.equal(rules.runCases({ cases: [item] })[0].ok, true);
  const wrong = { ...item, expect: { status: "fail", reasons: [] } };
  assert.equal(rules.runCases({ cases: [wrong] })[0].ok, false);
  const broken = rules.runCases({ cases: [{ ...item, today: "nope" }] })[0];
  assert.equal(broken.ok, false);
  assert.equal(broken.got.status, "error");
});

// ---------------------------------------------------------------- 靜態安全檢查（R2-01）

const CSP =
  "default-src 'none'; script-src 'self'; style-src 'self'; img-src 'self' data: https://tile.openstreetmap.org; connect-src 'self'; base-uri 'none'; form-action 'none'";

test("web/*.js 沒有把字串當 HTML 或程式碼的寫法（R2-01）", () => {
  const files = readdirSync(here("../")).filter((name) => name.endsWith(".js"));
  for (const expected of ["app.js", "deeplinks.js", "rules.js"]) assert.ok(files.includes(expected), expected);
  const banned = [
    /innerHTML/,
    /outerHTML/,
    /insertAdjacentHTML/,
    /document\.write/,
    /\beval\s*\(/,
    /\bnew\s+Function\b/,
    /\bFunction\s*\(/,
    /createContextualFragment/,
    /\bsrcdoc\b/,
    /DOMParser/,
    // Leaflet 這幾個入口會把字串當 HTML：
    /bindPopup/,
    /bindTooltip/,
    /setContent/,
    /\bhtml\s*:\s*["'`]/,
    /\battribution\s*:/,
    /setAttribute\(\s*["'](?:style|on)/i,
    /\.on[a-z]+\s*=(?!=)/,
    /cssText/,
    /javascript:/i,
    /\bset(?:Timeout|Interval)\s*\(\s*["'`]/,
    /XMLHttpRequest|WebSocket|sendBeacon|EventSource/,
    /\bimport\s*\(/,
    /fetch\(\s*["'`]https?:/,
  ];
  for (const file of files) {
    const text = read(`../${file}`);
    for (const pattern of banned) assert.doesNotMatch(text, pattern, `${file}：${pattern}`);
  }
});

function attributes(tag) {
  return Object.fromEntries([...tag.matchAll(/([a-zA-Z-]+)="([^"]*)"/g)].map((m) => [m[1].toLowerCase(), m[2]]));
}

test("HTML 沒有行內程式、行內事件、行內樣式；CSP 照 R2-01（Leaflet 自帶、OSM 圖磚）", () => {
  for (const file of ["../index.html", "./selftest.html"]) {
    const html = read(file);
    const meta = html.match(/<meta http-equiv="Content-Security-Policy" content="([^"]*)">/);
    assert.ok(meta, `${file} 沒有 CSP`);
    assert.equal(meta[1], CSP, file);
    assert.ok(html.indexOf("Content-Security-Policy") < html.search(/<(?:script|link)\b/i), `${file}：CSP 要在最前面`);
    const scripts = [...html.matchAll(/<script\b([^>]*)>([\s\S]*?)<\/script>/gi)];
    assert.ok(scripts.length >= 1, file);
    for (const [, attrs, body] of scripts) {
      const src = attributes(attrs).src;
      assert.ok(src && !/^[a-z]+:|^\/\//i.test(src), `${file}：script 要是本站的外部檔`);
      assert.equal(body.trim(), "", `${file}：不可以有行內程式`);
    }
    for (const match of html.matchAll(/<link\b([^>]*)>/gi)) {
      const href = attributes(match[1]).href;
      assert.ok(href && !/^[a-z]+:|^\/\//i.test(href), `${file}：樣式要是本站的檔`);
    }
    assert.doesNotMatch(html, /<[^>]*\son[a-z]+\s*=/i, `${file}：不可以有行內事件`);
    assert.doesNotMatch(html, /<[^>]*\sstyle\s*=/i, `${file}：不可以有行內樣式`);
    assert.doesNotMatch(html, /<style\b/i, file);
    assert.doesNotMatch(html, /javascript:/i, file);
    assert.doesNotMatch(html, /name="referrer"/i, `${file}：不要關掉 Referer，OSM 圖磚需要它`);
    for (const match of html.matchAll(/<a\b([^>]*)>/gi)) {
      const attrs = attributes(match[1]);
      assert.equal(links.safeExternalUrl(attrs.href), attrs.href, `${file}：${attrs.href}`);
      assert.equal(attrs.rel, "noopener noreferrer", file);
      assert.equal(attrs.target, "_blank", file);
    }
  }
  assert.match(read("../index.html"), /<meta name="robots" content="noindex">/);
});

test("Leaflet 1.9.4 的檔案和官方發布的相同（SRI 與雜湊）", () => {
  const digest = (bytes, encoding) => createHash("sha256").update(bytes).digest(encoding);
  const file = (rel) => readFileSync(here(rel));
  // 官方的 leaflet.css 每一行都是 CRLF；.gitattributes 的 eol=lf 可能在 commit 時把它換成 LF，
  // 所以沒有 CR 時先換回 CRLF，再和官方的 SRI 比對。
  const asCrlf = (bytes) =>
    bytes.includes(13) ? bytes : Buffer.from(bytes.toString("latin1").replace(/\n/g, "\r\n"), "latin1");
  // 官方網站 1.9.4 版的 SRI（Leaflet repo docs/_config.yml，「Update docs release 1.9.4」那次 commit）。
  assert.equal(`sha256-${digest(file("../vendor/leaflet/leaflet.js"), "base64")}`, "sha256-20nQCchB9co0qIjJZRGuk2/Z9VM+kNiyxNV1lvTlZBo=");
  assert.equal(`sha256-${digest(asCrlf(file("../vendor/leaflet/leaflet.css")), "base64")}`, "sha256-p4NxAoJBhIIN+hmNHrzRCf9tD/miZyoHS5obTRR9BMY=");
  // 圖檔和 npm leaflet@1.9.4 與 GitHub 發布的 leaflet.zip 相同。
  const images = {
    "layers-2x.png": "066daca850d8ffbef007af00b06eac0015728dee279c51f3cb6c716df7c42edf",
    "layers.png": "1dbbe9d028e292f36fcba8f8b3a28d5e8932754fc2215b9ac69e4cdecf5107c6",
    "marker-icon-2x.png": "00179c4c1ee830d3a108412ae0d294f55776cfeb085c60129a39aa6fc4ae2528",
    "marker-icon.png": "574c3a5cca85f4114085b6841596d62f00d7c892c7b03f28cbfa301deb1dc437",
    "marker-shadow.png": "264f5c640339f042dd729062cfc04c17f8ea0f29882b538e3848ed8f10edb4da",
  };
  for (const [name, hash] of Object.entries(images)) {
    assert.equal(digest(file(`../vendor/leaflet/images/${name}`), "hex"), hash, name);
  }
  assert.match(read("../vendor/leaflet/LICENSE"), /^BSD 2-Clause License/);
});

// ---------------------------------------------------------------- 開發用示意資料（test/fixtures）

test("示意資料符合 C2 的外形，每一筆都能判定，惡意字串的樣本還在", () => {
  assert.equal(fixtureMeta.schema_version, 1);
  assert.deepEqual(Object.keys(fixtureMeta), [...Object.keys(fixtureMeta)].sort());
  const profilesByArea = {
    taoyuan_core: rules.parseProfiles(rules.serializeProfiles({ v: 1, profiles: [seedBuffer] })).profiles[0],
    zhongli_ic: rules.parseProfiles(rules.serializeProfiles({ v: 1, profiles: [seedDrive] })).profiles[0],
  };
  for (const area of AREAS) {
    // 示意資料的 data/ 放在 sample_data/（.gitignore 會擋掉所有叫 data/ 的資料夾），app.js 的 ?fixtures 模式會換路徑。
    for (const file of area.files) {
      assert.ok(existsSync(here(`./fixtures/${file.replace(/^data\//, "sample_data/")}`)), file);
    }
    const listingsText = read(`./fixtures/sample_data/listings_${area.id}.json`);
    const txText = read(`./fixtures/sample_data/tx_${area.id}.json`);
    for (const text of [listingsText, txText]) {
      assert.ok(text.endsWith("\n"));
      assert.doesNotMatch(text, /\bNaN\b|\bInfinity\b/);
    }
    const listings = JSON.parse(listingsText);
    const txs = JSON.parse(txText);
    assert.deepEqual(Object.keys(listings), ["area", "groups", "schema_version"]);
    assert.deepEqual(Object.keys(txs), ["area", "as_of", "schema_version", "tx"]);
    assert.equal(listings.area, area.id);
    const gids = listings.groups.map((group) => group.gid);
    assert.deepEqual(gids, [...gids].sort((a, b) => a - b));
    const serials = txs.tx.map((tx) => tx.serial);
    assert.deepEqual(serials, [...serials].sort());
    for (const [kind, records] of [
      ["listing", listings.groups],
      ["tx", txs.tx],
    ]) {
      for (const record of records) {
        const result = rules.evaluate(kind, { ...record, area: area.id }, profilesByArea[area.id], "2026-11-21");
        assert.ok(["match", "pending", "fail"].includes(result.status));
      }
    }
    const geo = JSON.parse(read(`./fixtures/geo/${area.id}.geojson`));
    for (const feature of geo.features) {
      assert.equal(typeof feature.properties.role, "string");
      if (feature.properties.role === "drive") assert.ok(area.options.drive_min.includes(feature.properties.minutes));
      if (feature.properties.role === "near_band") assert.ok(area.options.near_m.includes(feature.properties.option));
      if (feature.properties.role === "avoid_band") assert.ok(area.options.rca_m.includes(feature.properties.option));
    }
  }
  const tao = read("./fixtures/sample_data/listings_taoyuan_core.json");
  for (const marker of ["onerror", "</script>", "javascript:"]) assert.ok(tao.includes(marker), marker);
  assert.ok(read("./fixtures/sample_data/tx_taoyuan_core.json").includes("onload"));
});
