/**
 * Loads lib/client.js the way the host's client module system does, with a stub
 * React, and checks the two registrations and the initial render tree.
 *
 * Run: node test/panel.test.mjs
 */
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { fileURLToPath } from "node:url";
import { dirname, join } from "node:path";

const here = dirname(fileURLToPath(import.meta.url));
const source = readFileSync(join(here, "..", "lib", "client.js"), "utf8");

// --- stub React: components are plain functions, hooks return their first value
const React = {
  Fragment: "Fragment",
  createElement(type, props, ...children) {
    return { type, props: props === null || props === undefined ? {} : props, children };
  },
  useState(initial) {
    return [typeof initial === "function" ? initial() : initial, () => {}];
  },
  useCallback(fn) {
    return fn;
  },
  useEffect() {}
};

/** Minimal module table: only react is resolved. */
function requireStub(name) {
  if (name === "react") return React;
  throw new Error(`unexpected require(${JSON.stringify(name)})`);
}

let bundle;
globalThis.window = {
  __ModuleLoader__: {
    load({ id, factory }) {
      bundle = { id, exports: factory(requireStub) };
    }
  }
};

// The bundle is a script, not a module: evaluate it in this realm.
new Function(source)();

assert.equal(bundle.id, "dsh-attune-memory", "bundle id");
assert.equal(typeof bundle.exports.apply, "function", "exports.apply");
assert.deepEqual(bundle.exports.inject, ["slots"], "exports.inject");

// --- stub client ctx: slots.inject runs its callback, slots.register records
const registrations = [];
const ctx = {
  slots: {
    inject(name, callback) {
      callback();
    },
    register(options, component) {
      registrations.push({ options, component });
      return () => {};
    }
  }
};

bundle.exports.apply(ctx);

const row = registrations.find((entry) => entry.options.name === "sidebar.panellist");
const page = registrations.find((entry) => entry.options.name === "main");

assert.ok(row, "sidebar.panellist registered");
assert.ok(page, "main registered");
assert.equal(row.options.id, bundle.exports.PANEL_ID, "row id");
assert.equal(page.options.key, bundle.exports.PANEL_ID, "page key matches row id");
assert.equal(typeof row.component, "function", "icon component");
assert.equal(typeof page.component, "function", "panel component");
assert.equal(row.options.label(), "记忆", "sidebar label");

// --- icon renders at the sizes the host passes
for (const size of [16, 18]) {
  const icon = row.component({ size, active: false });
  assert.equal(icon.type, "svg", "icon is an svg");
  assert.equal(icon.props.width, size, "icon width follows the host size");
}

// --- the panel's first render builds a tree (hooks stubbed, no fetch)
const tree = page.component();
const texts = [];
const hrefs = [];
const placeholders = [];
(function walk(node) {
  if (node === null || node === undefined || node === false) return;
  if (Array.isArray(node)) {
    node.forEach(walk);
    return;
  }
  if (typeof node === "string" || typeof node === "number") {
    texts.push(String(node));
    return;
  }
  if (typeof node === "object") {
    if (typeof node.props?.href === "string") hrefs.push(node.props.href);
    if (typeof node.props?.placeholder === "string") placeholders.push(node.props.placeholder);
    // Function components are rendered by the host's React; here we call them
    // directly so the assertion sees real text instead of an element stub.
    if (typeof node.type === "function" && node.type !== page.component) {
      walk(node.type(node.props));
      return;
    }
    if (Array.isArray(node.children)) node.children.forEach(walk);
  }
})(tree);
const joined = texts.join("");
assert.ok(joined.includes("记忆"), "panel title");
assert.ok(joined.includes("这个筛选下没有条目"), "empty list state before any data arrives");
assert.ok(joined.includes("从左边选一条记忆"), "empty detail state");
assert.ok(placeholders.includes("在已加载的条目里搜内容…"), "search box");
assert.deepEqual(hrefs, ["/dsh-attune-memory/"], "link target is the fenced management page");

console.log(`ok — ${registrations.length} registrations, panel tree renders ${texts.length} text nodes`);

// --- the detail pane, fed a payload shaped exactly like the engine's explain
function collect(node, bag) {
  if (node === null || node === undefined || node === false) return bag;
  if (Array.isArray(node)) {
    node.forEach((child) => collect(child, bag));
    return bag;
  }
  if (typeof node === "string" || typeof node === "number") {
    bag.texts.push(String(node));
    return bag;
  }
  if (typeof node === "object") {
    const tag = node.type;
    if (typeof tag === "string") bag.tags.push(tag);
    if (typeof node.props?.value === "string") bag.values.push(node.props.value);
    if (typeof node.props?.placeholder === "string") bag.placeholders.push(node.props.placeholder);
    if (typeof node.props?.title === "string") bag.titles.push(node.props.title);
    if (typeof tag === "function") {
      collect(tag(node.props), bag);
      return bag;
    }
    if (Array.isArray(node.children)) collect(node.children, bag);
  }
  return bag;
}

const detailRow = {
  id: "m-abc-1",
  type: "profile",
  subtype: "identity",
  subject_type: "user",
  subject_name: null,
  domain_key: null,
  content: "用户叫李四，正在备考 2027 年考研。",
  status: "active",
  importance: "key",
  injection_policy: "always",
  scope: "global",
  confidence: 0.95,
  keywords: ["李四", "考研"],
  created_at: "2026-11-14T02:00:00Z",
  updated_at: "2026-11-14T03:00:00Z"
};
const detailPayload = {
  explain: {
    current: detailRow,
    state_reason: "这条记忆是 2026-11-14 由用户写入的，之后改过一次措辞。",
    timeline: [
      { at: "2026-11-14T02:00:00Z", action: "add", reason: "首轮自述", actor: "user" },
      { at: "2026-11-14T03:00:00Z", action: "update", reason: "测试：改措辞", actor: "user" }
    ],
    versions: [
      {
        at: "2026-11-14T03:00:00Z",
        reason: "测试：改措辞",
        from: "用户叫李四，正在备考 2027 年考研。",
        to: "用户叫李四，正在准备 2027 年研究生入学考试。"
      }
    ],
    related: { superseded_by: null, supersedes: null, merged_from: [], same_slot_active: [] }
  }
};

const detailBag = collect(
  bundle.exports.__test.Detail({ detail: detailPayload, row: detailRow, busy: false, onSave() {}, onAction() {} }),
  { texts: [], tags: [], values: [], placeholders: [], titles: [] }
);
const detailText = detailBag.texts.join("");
assert.ok(detailBag.values.some((value) => value.includes("李四")), "the content textarea is filled from the row");
assert.ok(detailText.includes("首轮自述"), "timeline reason is on screen");
assert.ok(detailBag.values.some((value) => value.includes("李四")), "textarea carries the stored content");
assert.ok(detailText.includes("忘记"), "forget action");
assert.ok(detailText.includes("物理删除"), "hard delete stays available behind confirmation");

// --- the trust fence surfaces as a readable Chinese message, not a raw 401
const realFetch = globalThis.fetch;
globalThis.fetch = async () => ({ status: 401, ok: false, async json() { return { ok: false, error: "unauthorized" }; } });
await assert.rejects(
  () => bundle.exports.__test.request("/list?status=active"),
  /信任栅栏/,
  "401 becomes the panel's fence message"
);
globalThis.fetch = async () => ({ status: 200, ok: true, async json() { return { ok: false, error: "importance 只能是 key/important/normal/low 之一" }; } });
await assert.rejects(
  () => bundle.exports.__test.request("/update", { method: "POST", body: "{}" }),
  /importance 只能是/,
  "engine errors travel to the panel"
);
globalThis.fetch = realFetch;

console.log("ok — detail pane renders content, timeline and destructive actions; fence and engine errors surface");
