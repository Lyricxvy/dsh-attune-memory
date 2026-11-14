/**
 * Routes glue test with a stubbed engine: no Python, no child process, so it
 * also runs in a sandbox that forbids spawn. Verifies the request parsing,
 * the JSON wrappers the panel reads, the trust fence and the 404 paths.
 *
 * The real-engine variant lives in routes.test.mjs (needs a spawnable shell).
 *
 * Run: node test/routes.stub.test.mjs
 */
import assert from "node:assert/strict";

import { registerRoutes, ROUTE_BASE } from "../lib/routes.js";
import { PAGE_HTML } from "../lib/ui.js";

const ROW = {
  id: "m-1-2",
  type: "profile",
  subtype: "identity",
  content: "用户叫李四。",
  status: "active",
  importance: "key",
  injection_policy: "always",
  scope: "global",
  confidence: 0.95,
  keywords: ["李四"],
  updated_at: "2026-11-14T02:00:00Z"
};

const calls = [];
const engine = {
  async call(op, args) {
    calls.push([op, args]);
    switch (op) {
      case "list":
        return [ROW];
      case "stats":
        return { total: 1, by_status: { active: 1 }, problems: [] };
      case "get":
        return args.id === ROW.id ? ROW : null;
      case "explain":
        return {
          current: ROW,
          timeline: [{ at: "2026-11-14T02:00:00Z", action: "add", reason: "首轮", actor: "user" }],
          versions: [],
          related: { superseded_by: null, supersedes: null, merged_from: [], same_slot_active: [] }
        };
      case "update": {
        const fields = args.fields ?? {};
        if (fields.importance === "super-high") throw new Error("importance 只能是 key/important/normal/low 之一，收到 'super-high'");
        return { ...ROW, content: fields.content ?? ROW.content, keywords: fields.keywords ?? ROW.keywords };
      }
      case "forget":
        return { ...ROW, id: args.id, status: "deleted" };
      case "purge":
        return { id: args.id, purged: true };
      case "restore":
        return { ...ROW, id: args.id, status: "active" };
      case "tick":
        return { archived: 0 };
      default:
        throw new Error(`unexpected op ${op}`);
    }
  }
};

let handler;
const ctx = {
  webServer: {
    register(options) {
      assert.equal(options.kind, "prefix");
      assert.equal(options.path, ROUTE_BASE);
      handler = options.handler;
      return () => {};
    }
  },
  connection: { requestRejection: () => undefined },
  get() {
    return undefined;
  }
};

const dispose = registerRoutes(ctx, { engine, log: { warn() {} } });
assert.equal(typeof handler, "function");

function makeRequest(method, url, body) {
  const text = body === undefined ? "" : JSON.stringify(body);
  return {
    method,
    url,
    async *[Symbol.asyncIterator]() {
      if (text) yield Buffer.from(text, "utf8");
    }
  };
}

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
    },
    get body() {
      return Buffer.concat(chunks).toString("utf8");
    },
    get json() {
      return JSON.parse(this.body);
    }
  };
}

async function call(method, sub, body) {
  const res = makeResponse();
  await handler(makeRequest(method, `${ROUTE_BASE}${sub}`, body), res);
  return res;
}

try {
  // static page
  const page = await call("GET", "/");
  assert.equal(page.statusCode, 200);
  assert.match(page.headers["content-type"], /text\/html/);
  assert.equal(page.body, PAGE_HTML);

  // list passes filters through to the engine and wraps them as { ok, rows }
  const list = await call("GET", "/api/list?status=active&type=profile&subtype=identity&domain_key=study&policy=always&limit=50&offset=10");
  assert.equal(list.json.ok, true);
  assert.equal(list.json.rows.length, 1, "list returns the engine's rows array");
  const listCall = calls.find(([op]) => op === "list");
  assert.deepEqual(listCall[1], { status: "active", type: "profile", subtype: "identity", domain_key: "study", policy: "always", limit: 50, offset: 10 });

  // stats are wrapped under .stats (the panel reads payload.stats)
  const stats = await call("GET", "/api/stats");
  assert.equal(stats.json.ok, true);
  assert.equal(stats.json.stats.total, 1);

  // get → 404 when the engine says null
  assert.equal((await call("GET", "/api/get?id=m-1-2")).json.row.id, ROW.id);
  assert.equal((await call("GET", "/api/get?id=nope")).statusCode, 404);

  // explain
  const explain = await call("GET", "/api/explain?id=m-1-2");
  assert.equal(explain.json.explain.timeline.length, 1);

  // update: keywords string → array, wrapper .row
  const updated = await call("POST", "/api/update", { id: "m-1-2", content: "用户叫李四，备考 2027。", keywords: "李四, 考研, 2027", importance: "key" });
  assert.equal(updated.json.ok, true);
  assert.deepEqual(updated.json.row.keywords, ["李四", "考研", "2027"]);

  // update with a bad enum → 400 and the legal values survive in the message
  const bad = await call("POST", "/api/update", { id: "m-1-2", importance: "super-high" });
  assert.equal(bad.statusCode, 400);
  assert.equal(bad.json.ok, false);
  assert.match(bad.json.error, /key\/important\/normal\/low/);

  // forget / restore / hard delete
  const soft = await call("POST", "/api/forget", { id: "m-1-2", reason: "测试" });
  assert.equal(soft.json.row.status, "deleted");
  const hard = await call("POST", "/api/forget", { id: "m-1-2", hard: true, reason: "测试" });
  assert.equal(hard.json.purged, true);
  const back = await call("POST", "/api/restore", { id: "m-1-2", reason: "测试" });
  assert.equal(back.json.row.status, "active");

  // missing id on a POST → 400, not a crash
  const noId = await call("POST", "/api/update", { content: "x" });
  assert.equal(noId.statusCode, 400);

  // the fence
  ctx.connection.requestRejection = () => 403;
  const fenced = await call("GET", "/api/list?status=active");
  assert.equal(fenced.statusCode, 403);
  assert.equal(fenced.body, "");
  ctx.connection.requestRejection = () => undefined;

  // fail-closed #1：宿主没给 connection 服务 → 503，而不是放行
  let handler2;
  const ctxNoConn = { webServer: { register(options) { handler2 = options.handler; return () => {}; } }, get() {} };
  const dispose2 = registerRoutes(ctxNoConn, { engine, log: { warn() {} } });
  const noConnRes = makeResponse();
  await handler2(makeRequest("GET", `${ROUTE_BASE}/api/list`), noConnRes);
  assert.equal(noConnRes.statusCode, 503, "missing connection service must fail closed");
  assert.match(noConnRes.json.error, /Trust barrier unavailable/);
  dispose2?.();

  // fail-closed #2：requestRejection 抛错 → 503（我们不知道这个请求可不可信）
  ctx.connection.requestRejection = () => { throw new Error("boom"); };
  const thrown = await call("GET", "/api/stats");
  assert.equal(thrown.statusCode, 503, "throwing trust check must fail closed");
  assert.match(thrown.json.error, /Trust check failed/);
  ctx.connection.requestRejection = () => undefined;

  // unknown route
  assert.equal((await call("GET", "/api/nope")).statusCode, 404);

  console.log(`ok — ${calls.length} engine calls, page/list/stats/get/explain/update/forget/restore/fence(fail-closed)/404 all wired`);
} finally {
  dispose?.();
}
