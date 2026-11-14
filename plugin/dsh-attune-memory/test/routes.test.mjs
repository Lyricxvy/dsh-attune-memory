/**
 * Contract test for the panel's data path: the real routes handler in front of
 * the real Python engine, driven with the exact request shapes lib/client.js
 * sends. Catches wire-shape drift (a renamed field, a moved wrapper) without a
 * browser.
 *
 * Run: node test/routes.test.mjs
 */
import assert from "node:assert/strict";
import { existsSync, mkdtempSync, rmSync } from "node:fs";
import { tmpdir } from "node:os";
import { join, dirname } from "node:path";
import { fileURLToPath } from "node:url";

import { EngineClient } from "../lib/engine.js";
import { registerRoutes, ROUTE_BASE } from "../lib/routes.js";
import { PAGE_HTML } from "../lib/ui.js";

const here = dirname(fileURLToPath(import.meta.url));
const engineDir = join(here, "..", "..", "..", "engine");
const workdir = mkdtempSync(join(tmpdir(), "attune-routes-"));
const dbPath = join(workdir, "route-test.db");

/** Minimal Node req: only the fields the handler reads. */
function makeRequest(method, url, body) {
  const text = body === undefined ? "" : JSON.stringify(body);
  return {
    method,
    url,
    async *[Symbol.asyncIterator]() {
      if (text !== "") yield Buffer.from(text, "utf8");
    }
  };
}

/** Minimal Node res: collect status, headers and body. */
function makeResponse() {
  const chunks = [];
  return {
    statusCode: 200,
    headers: {},
    setHeader(name, value) {
      this.headers[name.toLowerCase()] = value;
    },
    end(body) {
      if (body !== undefined) chunks.push(Buffer.isBuffer(body) ? body : Buffer.from(String(body), "utf8"));
      this.finished = true;
    },
    get body() {
      return Buffer.concat(chunks).toString("utf8");
    },
    get json() {
      return JSON.parse(this.body);
    }
  };
}

let handler;
const ctx = {
  webServer: {
    register(options) {
      assert.equal(options.kind, "prefix", "routes mount as a prefix");
      assert.equal(options.path, ROUTE_BASE, "routes mount under ROUTE_BASE");
      handler = options.handler;
      return () => {};
    }
  },
  // The fence is exercised below by toggling this; the panel runs behind it.
  connection: { requestRejection: () => undefined },
  get() {
    return undefined;
  }
};

// 源码树里包名是 memory，发布树里被 rename.py 改成了 attune_memory；
// 测试两边都要能跑，所以按目录实际存在的那一个来选。
const engineModule = existsSync(join(engineDir, "attune_memory"))
  ? "attune_memory.rpc"
  : "attune_memory.rpc";
const engine = new EngineClient({ engineDir, engineModule, dbPath, timeoutMs: 15000 });
const dispose = registerRoutes(ctx, { engine, log: { warn() {} } });
assert.equal(typeof handler, "function", "registerRoutes installed a handler");

async function call(method, sub, body) {
  const response = makeResponse();
  await handler(makeRequest(method, `${ROUTE_BASE}${sub}`, body), response);
  return response;
}

try {
  // --- the static page stays readable (no data in it)
  const page = await call("GET", "/");
  assert.equal(page.statusCode, 200, "page status");
  assert.match(page.headers["content-type"], /text\/html/, "page is html");
  assert.equal(page.body, PAGE_HTML, "page body is the UI module's export");

  // --- seed two rows through the same endpoint the tools use
  const added1 = await engine.call("add", {
    content: "用户叫李四，正在备考 2027 年考研。",
    type: "profile",
    subtype: "identity",
    importance: "key",
    injection_policy: "always",
    confidence: 0.95,
    keywords: ["李四", "考研"]
  });
  assert.equal(added1.action, "add", "first add");
  const id1 = added1.memory.id;

  const added2 = await engine.call("add", {
    content: "用户做极限题时总想不到变形方向。",
    type: "profile",
    subtype: "skill",
    scope: "domain",
    domain_key: "study.math",
    importance: "normal",
    injection_policy: "contextual",
    confidence: 0.8,
    keywords: ["极限", "变形"]
  });
  assert.equal(added2.action, "add", "second add");
  const id2 = added2.memory.id;

  // --- list, exactly as the panel asks for it
  const list = await call("GET", "/api/list?status=active&limit=200");
  assert.equal(list.statusCode, 200, "list status");
  assert.equal(list.json.ok, true, "list ok");
  assert.equal(list.json.rows.length, 2, "list returns both rows");
  for (const row of list.json.rows) {
    for (const field of ["id", "type", "subtype", "content", "status", "importance", "injection_policy", "scope", "confidence", "updated_at"]) {
      assert.ok(field in row, `list row carries ${field}`);
    }
  }

  // type filter
  const typed = await call("GET", "/api/list?status=active&type=profile&limit=200");
  assert.equal(typed.json.rows.length, 2, "type filter keeps profile rows");

  // --- stats wrapper (the panel reads payload.stats, not the payload itself)
  const stats = await call("GET", "/api/stats");
  assert.equal(stats.json.ok, true, "stats ok");
  assert.ok(stats.json.stats, "stats are wrapped under .stats");
  assert.equal(stats.json.stats.total, 2, "stats total");
  assert.equal(stats.json.stats.by_status.active, 2, "stats by_status.active");

  // --- explain (detail pane)
  const explain = await call("GET", `/api/explain?id=${encodeURIComponent(id1)}`);
  assert.equal(explain.statusCode, 200, "explain status");
  const detail = explain.json.explain;
  assert.ok(detail.current, "explain.current");
  assert.ok(Array.isArray(detail.timeline), "explain.timeline is an array");
  assert.ok(detail.timeline.length >= 1, "the add shows up in the timeline");
  assert.ok(Array.isArray(detail.versions), "explain.versions is an array");
  assert.ok(detail.relations ?? detail.related, "explain carries relations");

  // --- update (panel save): content + importance + keywords + reason
  const updated = await call("POST", "/api/update", {
    id: id1,
    content: "用户叫李四，正在准备 2027 年研究生入学考试。",
    importance: "key",
    injection_policy: "always",
    scope: "global",
    confidence: 0.9,
    subtype: "identity",
    keywords: "李四, 考研, 2027",
    reason: "测试：改措辞"
  });
  assert.equal(updated.statusCode, 200, "update status");
  assert.equal(updated.json.ok, true, "update ok");
  assert.match(updated.json.row.content, /研究生入学考试/, "content changed");
  // 引擎里 keywords 是空格分隔的字符串；面板同时接受字符串与数组（client.js 会 join/split）。
  // 这里只断言"三个词都在"，不去规定线上形状。
  const kw = Array.isArray(updated.json.row.keywords)
    ? updated.json.row.keywords
    : String(updated.json.row.keywords).split(/\s+/);
  assert.deepEqual(kw.filter(Boolean), ["李四", "考研", "2027"], "keywords parsed from the comma string");

  const afterUpdate = await call("GET", `/api/explain?id=${encodeURIComponent(id1)}`);
  assert.ok(afterUpdate.json.explain.versions.length >= 1, "the change left a version");
  assert.equal(
    afterUpdate.json.explain.timeline[afterUpdate.json.explain.timeline.length - 1].reason,
    "测试：改措辞",
    "the reason lands in the timeline"
  );

  // --- update rejects an invalid enum without leaking the CHECK constraint
  const bad = await call("POST", "/api/update", { id: id1, importance: "super-high" });
  assert.equal(bad.statusCode, 400, "invalid enum is a 400");
  assert.equal(bad.json.ok, false, "invalid enum is reported as failure");
  assert.match(bad.json.error, /key/, "the error lists the legal values");

  // --- forget (soft) → restore → hard delete, the panel's three destructive paths
  const forgotten = await call("POST", "/api/forget", { id: id2, reason: "测试：先忘记" });
  assert.equal(forgotten.json.ok, true, "forget ok");
  assert.equal(forgotten.json.row.status, "deleted", "forget is soft");

  const activeAfterForget = await call("GET", "/api/list?status=active&limit=200");
  assert.equal(activeAfterForget.json.rows.length, 1, "the forgotten row left the active list");

  const restored = await call("POST", "/api/restore", { id: id2, reason: "测试：恢复" });
  assert.equal(restored.json.row.status, "active", "restore brings it back");

  const purged = await call("POST", "/api/forget", { id: id2, hard: true, reason: "测试：物理删除" });
  assert.equal(purged.json.ok, true, "hard delete ok");
  assert.equal(purged.json.purged, true, "hard delete reports purged");

  const gone = await call("GET", `/api/get?id=${encodeURIComponent(id2)}`);
  assert.equal(gone.statusCode, 404, "purged row is gone");

  // --- the fence: a rejected peer gets 401 and no body
  ctx.connection.requestRejection = () => 401;
  const fenced = await call("GET", "/api/list?status=active");
  assert.equal(fenced.statusCode, 401, "fence answers 401");
  assert.equal(fenced.body, "", "fence sends no data");
  ctx.connection.requestRejection = () => undefined;

  // --- unknown route
  const unknown = await call("GET", "/api/nope");
  assert.equal(unknown.statusCode, 404, "unknown route is a 404");

  console.log("ok — page, list, stats, explain, update, forget, restore, purge, fence, 404");
} finally {
  dispose?.();
  await engine.close();
  // Windows 上临时目录偶尔被尚未退出的子进程占着，删除会 EPERM；
  // 清理失败不该让一个已经跑完的测试变红，所以吞掉并重试一次。
  try {
    rmSync(workdir, { recursive: true, force: true, maxRetries: 3, retryDelay: 100 });
  } catch {
    /* 临时目录留给系统清理 */
  }
}
