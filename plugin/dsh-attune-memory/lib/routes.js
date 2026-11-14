/**
 * 记忆管理页的 HTTP 路由（P9）。
 *
 * 设计约束（对齐任务书 P9 与 MEMORY_LIFECYCLE.md §7）：
 *
 *   - **不给裸 SQL**。所有操作都走引擎已有的 `list/get/explain/update/forget/restore/tick`，
 *     页面能做的事就是模型能做的事，不多也不少。
 *   - **写操作只有四个**：改内容/重要性/注入策略、忘记（软删，可恢复）、恢复、物理删除
 *     （二次确认，且必须先软删）。没有"直接把状态改成 superseded"这种入口——状态是
 *     事实变化的**结果**，不是用户在界面上选的选项（P6 §7 明确要求不给状态下拉框）。
 *   - **每个数据请求都过信任栅栏**。`ctx.connection.requestRejection(req)` 不通过就
 *     直接结束：否则用户随手打开的任意网页都能 fetch 本机接口读写记忆库。
 *     静态页面本身不过栅栏（它不含任何数据），见 `servePage` 的注释。
 *
 * 挂载在 `/dsh-attune-memory` 前缀下：
 *
 *     GET  /dsh-attune-memory/            管理页（静态 HTML，无数据）
 *     GET  /dsh-attune-memory/api/list    列表
 *     GET  /dsh-attune-memory/api/get     单条原始行
 *     GET  /dsh-attune-memory/api/explain 单条解释（时间轴 + 版本 + 关系）
 *     GET  /dsh-attune-memory/api/stats   概况（含运行时计数）
 *     GET  /dsh-attune-memory/api/reads   最近检索的过程数字
 *     GET  /dsh-attune-memory/api/discarded 被丢弃的候选与理由
 *     POST /dsh-attune-memory/api/update  改一条
 *     POST /dsh-attune-memory/api/forget  忘记（soft）/ 物理删除（hard）
 *     POST /dsh-attune-memory/api/restore 恢复
 *     POST /dsh-attune-memory/api/tick    跑一次惰性维护
 */

import { PAGE_HTML } from './ui.js';

export const ROUTE_BASE = '/dsh-attune-memory';
const MAX_BODY = 1024 * 1024;

/**
 * 把 URL 上的 limit 收敛成安全整数：非数字 / 0 / 负数回退默认值，上限封顶。
 * 直接把手里的字符串丢给引擎，等于让 SQL 的 LIMIT 去解释它 —— 参数校验应该在边界做完。
 */
function clampLimit(raw, fallback, max = 200) {
  const value = Number(raw);
  if (!Number.isFinite(value) || value <= 0) return fallback;
  return Math.min(Math.floor(value), max);
}

function sendJson(res, status, payload) {
  const body = JSON.stringify(payload);
  res.statusCode = status;
  res.setHeader('content-type', 'application/json; charset=utf-8');
  res.setHeader('cache-control', 'no-store');
  res.end(body);
}

function sendHtml(res, html) {
  res.statusCode = 200;
  res.setHeader('content-type', 'text/html; charset=utf-8');
  res.setHeader('cache-control', 'no-store');
  res.end(html);
}

async function readBody(req) {
  const chunks = [];
  let size = 0;
  for await (const chunk of req) {
    size += chunk.length;
    if (size > MAX_BODY) throw new Error('请求体过大');
    chunks.push(chunk);
  }
  const text = Buffer.concat(chunks).toString('utf8').trim();
  if (!text) return {};
  try {
    return JSON.parse(text);
  } catch {
    throw new Error('请求体不是合法 JSON');
  }
}

/** 引擎错误转成人话，页面直接显示。 */
function fail(res, err) {
  const message = String(err?.message ?? err);
  return sendJson(res, 400, { ok: false, error: message });
}

export function registerRoutes(ctx, deps) {
  const { engine, log } = deps;
  const webServer = ctx.webServer ?? (typeof ctx.get === 'function' ? ctx.get('webServer') : undefined);
  const connection = ctx.connection ?? (typeof ctx.get === 'function' ? ctx.get('connection') : undefined);
  if (!webServer || typeof webServer.register !== 'function') {
    log?.warn?.('dsh-attune-memory: 没有 webServer 服务，管理页不可用');
    return () => {};
  }

  /**
   * 信任栅栏：没通过就应答并返回 true。
   *
   * **必须 fail-closed。** 这里保护的是用户的长期记忆和原话片段：列全部记忆、读原文、
   * 看 explain 时间轴、看最近查询、看被拒候选原话、改/忘/恢复/物理删除，全在下面这些
   * 接口里。所以"拿不到信任检查结果"只能当成"拒绝"，不能当成"放行"：
   *   - connection 服务不存在 / 没有 requestRejection → 503（宿主版本不匹配或未注入）
   *   - requestRejection 抛错 → 503（我们不知道这个请求可不可信）
   * 早期版本在这两种情况下 `return false`（放行），等于把接口暴露给任意网页的 fetch，
   * 三方审查把这条列为 P0，见 docs/ADR.md ADR-025。
   */
  function rejected(req, res) {
    if (!connection || typeof connection.requestRejection !== 'function') {
      log?.warn?.('dsh-attune-memory: 没有 connection 服务，管理 API 拒绝服务（fail-closed）');
      res.statusCode = 503;
      res.setHeader('content-type', 'application/json; charset=utf-8');
      res.end(JSON.stringify({ ok: false, error: 'Trust barrier unavailable' }));
      return true;
    }
    try {
      const code = connection.requestRejection(req);
      if (code === undefined || code === null || code === false) return false; // 宿主放行
      res.statusCode = typeof code === 'number' ? code : 403;
      res.end();
      return true;
    } catch (err) {
      log?.warn?.(`dsh-attune-memory: 信任检查失败 ${String(err?.message ?? err)}`);
      res.statusCode = 503;
      res.setHeader('content-type', 'application/json; charset=utf-8');
      res.end(JSON.stringify({ ok: false, error: 'Trust check failed' }));
      return true;
    }
  }

  function servePage(res) {
    // 页面不过信任栅栏：它是纯静态 HTML，不含任何记忆数据；数据接口在下面逐个过栅栏。
    // 若在这里就拦，用户从书签/新标签直达会被 401 挡住，而页面本身泄露不了任何东西。
    sendHtml(res, PAGE_HTML);
  }

  async function handler(req, res) {
    let url;
    try {
      url = new URL(req.url, 'http://localhost');
    } catch {
      return sendJson(res, 400, { ok: false, error: '无效 URL' });
    }

    const sub = url.pathname.slice(ROUTE_BASE.length) || '/';
    const method = (req.method || 'GET').toUpperCase();
    const query = Object.fromEntries(url.searchParams.entries());

    if (sub === '/' || sub === '') {
      if (method !== 'GET' && method !== 'HEAD') return sendJson(res, 405, { ok: false, error: '只支持 GET' });
      return servePage(res);
    }

    if (rejected(req, res)) return;

    try {
      if (sub === '/api/list' && method === 'GET') {
        const rows = await engine.call('list', {
          status: query.status && query.status !== 'all' ? query.status : undefined,
          type: query.type || undefined,
          subtype: query.subtype || undefined,
          domain_key: query.domain_key || undefined,
          policy: query.policy || undefined,
          limit: Math.min(Number(query.limit) || 100, 500),
          offset: Math.max(Number(query.offset) || 0, 0),
        });
        return sendJson(res, 200, { ok: true, rows });
      }

      if (sub === '/api/get' && method === 'GET') {
        if (!query.id) return sendJson(res, 400, { ok: false, error: '缺少 id' });
        const row = await engine.call('get', { id: query.id });
        if (!row) return sendJson(res, 404, { ok: false, error: `没有这条记忆: ${query.id}` });
        return sendJson(res, 200, { ok: true, row });
      }

      if (sub === '/api/explain' && method === 'GET') {
        if (!query.id) return sendJson(res, 400, { ok: false, error: '缺少 id' });
        const data = await engine.call('explain', { id: query.id });
        return sendJson(res, 200, { ok: true, explain: data });
      }

      if (sub === '/api/stats' && method === 'GET') {
        return sendJson(res, 200, { ok: true, stats: await engine.call('stats', {}) });
      }

      // 两条诊断接口：引擎返回的是行数组，按页面既有的约定包成 { ok, rows }
      // （ui.js 的 loadDiag 读的就是 r.rows；换成别的键名会让诊断区永远显示空）。
      if (sub === '/api/reads' && method === 'GET') {
        const limit = clampLimit(query.limit, 20);
        return sendJson(res, 200, { ok: true, rows: await engine.call('reads', { limit }) });
      }

      if (sub === '/api/discarded' && method === 'GET') {
        const limit = clampLimit(query.limit, 20);
        return sendJson(res, 200, { ok: true, rows: await engine.call('discarded', { limit }) });
      }

      if (sub === '/api/update' && method === 'POST') {
        const body = await readBody(req);
        if (!body.id) return sendJson(res, 400, { ok: false, error: '缺少 id' });
        const fields = {};
        for (const key of ['content', 'importance', 'injection_policy', 'confidence', 'scope', 'keywords', 'subtype']) {
          if (body[key] !== undefined && body[key] !== null && body[key] !== '') fields[key] = body[key];
        }
        if (Object.keys(fields).length === 0) {
          return sendJson(res, 400, { ok: false, error: '没有任何要改的字段' });
        }
        if (fields.confidence !== undefined) fields.confidence = Number(fields.confidence);
        if (typeof fields.keywords === 'string') {
          fields.keywords = fields.keywords.split(/[,，\s]+/).filter(Boolean);
        }
        const row = await engine.call('update', {
          id: body.id,
          fields,
          reason: body.reason || '用户在管理页修改',
        });
        return sendJson(res, 200, { ok: true, row });
      }

      if (sub === '/api/forget' && method === 'POST') {
        const body = await readBody(req);
        if (!body.id) return sendJson(res, 400, { ok: false, error: '缺少 id' });
        const reason = body.reason || '用户在管理页忘记';
        if (body.hard) {
          // 物理删除是两步：先软删（这是引擎的合法前置条件），再 purge。
          await engine.call('forget', { id: body.id, reason });
          await engine.call('purge', { id: body.id, reason });
          return sendJson(res, 200, { ok: true, purged: true });
        }
        const row = await engine.call('forget', { id: body.id, reason });
        return sendJson(res, 200, { ok: true, row });
      }

      if (sub === '/api/restore' && method === 'POST') {
        const body = await readBody(req);
        if (!body.id) return sendJson(res, 400, { ok: false, error: '缺少 id' });
        const row = await engine.call('restore', { id: body.id, reason: body.reason || '用户恢复' });
        return sendJson(res, 200, { ok: true, row });
      }

      if (sub === '/api/tick' && method === 'POST') {
        return sendJson(res, 200, { ok: true, result: await engine.call('tick', {}) });
      }

      return sendJson(res, 404, { ok: false, error: `未知路由: ${sub}` });
    } catch (err) {
      log?.warn?.(`dsh-attune-memory: 管理接口出错 ${String(err?.message ?? err)}`);
      return fail(res, err);
    }
  }

  const dispose = webServer.register({ kind: 'prefix', path: ROUTE_BASE, handler });
  return dispose;
}
