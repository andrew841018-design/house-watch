// WP0 的契約測試：rules.js 的匯出形狀、ENUMS／REASONS 標記區塊、案例表的格式。
// 規則案例本身由 WP8a 的 rules.test.mjs 執行；這裡不呼叫 evaluate。
import { test } from "node:test";
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";

import * as rules from "../rules.js";

const rulesSource = readFileSync(new URL("../rules.js", import.meta.url), "utf8");
const table = JSON.parse(readFileSync(new URL("./rules_cases.json", import.meta.url), "utf8"));

function contractBlock(name) {
  const pattern = new RegExp(`/\\*<contract:${name}>\\*/([\\s\\S]*?)/\\*</contract:${name}>\\*/`);
  const found = rulesSource.match(pattern);
  assert.ok(found, `rules.js 缺少 ${name} 的標記區塊`);
  return JSON.parse(found[1]);
}

test("rules.js exports the C4 API", () => {
  for (const name of ["evaluate", "parseProfiles", "serializeProfiles"]) {
    assert.equal(typeof rules[name], "function", name);
  }
  assert.equal(typeof rules.ENUMS, "object");
  assert.ok(Array.isArray(rules.REASONS));
});

test("ENUMS and REASONS equal their contract blocks", () => {
  assert.deepEqual(rules.ENUMS, contractBlock("ENUMS"));
  assert.deepEqual(rules.REASONS, contractBlock("REASONS"));
});

test("case table follows the C4 format", () => {
  assert.deepEqual(Object.keys(table).sort(), ["cases", "version"]);
  assert.equal(table.version, 1);
  const names = new Set();
  for (const item of table.cases) {
    assert.deepEqual(Object.keys(item).sort(), ["expect", "kind", "name", "profile", "rec", "today"]);
    assert.ok(!names.has(item.name), `重複的案例名稱：${item.name}`);
    names.add(item.name);
    assert.ok(["listing", "tx"].includes(item.kind), item.name);
    assert.match(item.today, /^\d{4}-\d{2}-\d{2}$/);
    assert.equal(typeof item.rec.area, "string", item.name);
    assert.equal(typeof item.profile.area, "string", item.name);
    assert.deepEqual(Object.keys(item.expect).sort(), ["reasons", "status"]);
    assert.ok(["match", "pending", "fail"].includes(item.expect.status), item.name);
    for (const reason of item.expect.reasons) {
      assert.ok(rules.REASONS.includes(reason), `${item.name}：${reason} 不在 REASONS`);
    }
  }
});
