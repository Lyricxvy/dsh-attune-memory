/**
 * 管理页（单文件，无构建步骤）。
 *
 * 只做四件事：看列表、看一条的来龙去脉、改一条、忘掉一条。
 * 状态不做成下拉框——状态是事实变化的**结果**，不是可选项（MEMORY_LIFECYCLE.md §7）。
 * 页面里所有 fetch 都是同源相对路径，因此浏览器自带的 Cookie 会随请求带上，
 * 服务端据此过信任栅栏。
 */
export const PAGE_HTML = `<!doctype html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>记忆库 · DeepSeek Harness</title>
<style>
  :root {
    --bg: #fbfbfa; --panel: #fff; --line: #e5e3df; --text: #1f2328; --muted: #6b7280;
    --accent: #2563eb; --danger: #b91c1c; --ok: #15803d; --warn: #b45309;
    --chip: #f3f4f6;
  }
  @media (prefers-color-scheme: dark) {
    :root {
      --bg: #16181c; --panel: #1d2025; --line: #2d3138; --text: #e6e6e3; --muted: #9aa0a6;
      --accent: #6ea8fe; --danger: #f87171; --ok: #4ade80; --warn: #fbbf24; --chip: #262a30;
    }
  }
  * { box-sizing: border-box; }
  body {
    margin: 0; background: var(--bg); color: var(--text);
    font: 14px/1.6 -apple-system, "Segoe UI", "Microsoft YaHei", system-ui, sans-serif;
  }
  header {
    display: flex; align-items: baseline; gap: 16px; flex-wrap: wrap;
    padding: 14px 20px; border-bottom: 1px solid var(--line); background: var(--panel);
    position: sticky; top: 0; z-index: 5;
  }
  header h1 { font-size: 15px; margin: 0; font-weight: 650; letter-spacing: .2px; }
  header .stats { color: var(--muted); font-size: 12.5px; }
  header .spacer { flex: 1; }
  button {
    font: inherit; padding: 5px 12px; border-radius: 7px; cursor: pointer;
    border: 1px solid var(--line); background: var(--panel); color: var(--text);
  }
  button:hover { border-color: var(--accent); color: var(--accent); }
  button.primary { background: var(--accent); border-color: var(--accent); color: #fff; }
  button.primary:hover { filter: brightness(1.05); color: #fff; }
  button.danger { color: var(--danger); border-color: color-mix(in srgb, var(--danger) 40%, var(--line)); }
  button:disabled { opacity: .5; cursor: default; }

  main { display: grid; grid-template-columns: minmax(320px, 420px) 1fr; gap: 0; min-height: calc(100vh - 57px); }
  @media (max-width: 900px) { main { grid-template-columns: 1fr; } }

  .col-list { border-right: 1px solid var(--line); display: flex; flex-direction: column; min-height: 0; }
  .filters { display: flex; gap: 8px; padding: 10px 14px; border-bottom: 1px solid var(--line); flex-wrap: wrap; }
  select, input[type=text], input[type=number], textarea {
    font: inherit; color: var(--text); background: var(--panel);
    border: 1px solid var(--line); border-radius: 7px; padding: 5px 8px;
  }
  .rows { overflow: auto; }
  .row {
    padding: 10px 14px; border-bottom: 1px solid var(--line); cursor: pointer;
    display: grid; grid-template-columns: 1fr auto; gap: 4px 10px;
  }
  .row:hover { background: var(--chip); }
  .row.sel { background: color-mix(in srgb, var(--accent) 12%, transparent); }
  .row .text { overflow: hidden; display: -webkit-box; -webkit-line-clamp: 2; -webkit-box-orient: vertical; }
  .row .meta { grid-column: 1 / -1; color: var(--muted); font-size: 12px; display: flex; gap: 8px; flex-wrap: wrap; }
  .chip {
    display: inline-block; padding: 0 6px; border-radius: 4px; background: var(--chip);
    color: var(--muted); font-size: 11.5px; line-height: 18px;
  }
  .chip.always { color: var(--ok); }
  .chip.on_demand, .chip.contextual { color: var(--accent); }
  .chip.never { color: var(--muted); }
  .chip.deleted { color: var(--danger); }
  .chip.archived, .chip.superseded { color: var(--warn); }

  .col-detail { padding: 16px 20px 40px; overflow: auto; }
  .empty { color: var(--muted); padding: 40px 0; text-align: center; }
  .field { margin-bottom: 14px; }
  .field label { display: block; font-size: 12px; color: var(--muted); margin-bottom: 4px; }
  textarea { width: 100%; min-height: 84px; resize: vertical; line-height: 1.55; }
  .inline { display: flex; gap: 10px; flex-wrap: wrap; }
  .inline .field { flex: 1; min-width: 150px; }
  .actions { display: flex; gap: 8px; flex-wrap: wrap; margin: 18px 0 6px; }
  h2 { font-size: 13px; margin: 26px 0 10px; font-weight: 650; }
  .reason { color: var(--muted); font-size: 12px; margin: 0 0 14px; }
  table { border-collapse: collapse; width: 100%; font-size: 13px; }
  td, th { text-align: left; padding: 6px 8px; border-bottom: 1px solid var(--line); vertical-align: top; }
  th { color: var(--muted); font-weight: 500; font-size: 12px; }
  .diff { color: var(--muted); font-size: 12px; word-break: break-word; }
  .toast {
    position: fixed; left: 50%; bottom: 24px; transform: translateX(-50%);
    background: var(--panel); border: 1px solid var(--line); border-radius: 9px;
    padding: 9px 16px; box-shadow: 0 6px 24px rgba(0,0,0,.16); opacity: 0;
    transition: opacity .18s; pointer-events: none; max-width: 80vw;
  }
  .toast.show { opacity: 1; }
  .toast.err { border-color: var(--danger); color: var(--danger); }
.diag { position: fixed; right: 16px; bottom: 16px; width: min(560px, 60vw); max-height: 70vh; overflow: auto;
  background: #14161a; border: 1px solid #2a2f39; border-radius: 10px; box-shadow: 0 10px 30px rgba(0,0,0,.5); z-index: 50; }
.diag-head { display: flex; align-items: center; gap: 8px; padding: 8px 12px; border-bottom: 1px solid #2a2f39; }
.diag-body { padding: 10px 12px; }
.diag-body h3 { font-size: 12px; color: #8b93a1; margin: 10px 0 4px; font-weight: 600; }
.diag-body pre { margin: 0; white-space: pre-wrap; word-break: break-all; font-size: 12px; line-height: 1.55; color: #c9d1d9; }
</style>
</head>
<body>
<header>
  <h1>记忆库</h1>
  <span class="stats" id="stats">加载中…</span>
  <span class="spacer"></span>
  <button id="btn-tick" title="只归档过期的 state 与半年前的 superseded，不删任何东西">维护</button>
  <button id="btn-diag" title="看看最近检索了几条、丢了几条、注入有没有消失">诊断</button>
  <button id="btn-refresh">刷新</button>
</header>

<main>
  <section class="col-list">
    <div class="filters">
      <select id="f-status">
        <option value="active">生效中</option>
        <option value="archived">已归档</option>
        <option value="deleted">已忘记</option>
        <option value="superseded">已被取代</option>
        <option value="all">全部</option>
      </select>
      <select id="f-type">
        <option value="">全部类型</option>
        <option value="profile">profile</option>
        <option value="domain">domain</option>
        <option value="state">state</option>
        <option value="episodic">episodic</option>
      </select>
    </div>
    <div class="rows" id="rows"><div class="empty">加载中…</div></div>
  </section>

  <section class="col-detail" id="detail">
    <div class="empty">左边选一条记忆，看它为什么会是现在这样。</div>
  </section>
</main>

<section id="diag" class="diag" hidden>
  <div class="diag-head"><b>运行时诊断</b><span class="spacer"></span><button id="btn-diag-close">关闭</button></div>
  <div class="diag-body">
    <h3>计数</h3><pre id="diag-counters">…</pre>
    <h3>最近检索</h3><pre id="diag-reads">…</pre>
    <h3>被丢弃的候选（只给人看）</h3><pre id="diag-discarded">…</pre>
  </div>
</section>

<div class="toast" id="toast"></div>

<script>
(function () {
  'use strict';
  var BASE = '/dsh-attune-memory';
  var state = { rows: [], selected: null, explain: null };

  function el(id) { return document.getElementById(id); }
  function esc(s) {
    return String(s == null ? '' : s).replace(/[&<>"']/g, function (c) {
      return { '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c];
    });
  }
  function toast(msg, isErr) {
    var t = el('toast');
    t.textContent = msg;
    t.className = 'toast show' + (isErr ? ' err' : '');
    clearTimeout(toast._h);
    toast._h = setTimeout(function () { t.className = 'toast'; }, 3200);
  }
  function api(path, opts) {
    return fetch(BASE + path, Object.assign({ credentials: 'same-origin' }, opts || {}))
      .then(function (r) { return r.json().then(function (j) { return { status: r.status, body: j }; }); })
      .then(function (res) {
        if (!res.body || res.body.ok !== true) throw new Error((res.body && res.body.error) || ('HTTP ' + res.status));
        return res.body;
      });
  }

  function loadStats() {
    api('/api/stats').then(function (r) {
      var s = r.stats || {};
      var parts = ['共 ' + (s.total || 0) + ' 条'];
      var by = s.by_status || {};
      Object.keys(by).forEach(function (k) { parts.push(k + ' ' + by[k]); });
      if (s.problems && s.problems.length) parts.push('⚠ ' + s.problems.length + ' 处不变式异常');
      var c = s.counters || {};
      if (c.inject_expected) {
        var faded = (c.inject_faded_unexplained || 0);
        parts.push('注入 ' + c.inject_expected + ' 次' + (faded ? ('，其中 ' + faded + ' 次无解释消失 ⚠') : '，无异常消失'));
      }
      el('stats').textContent = parts.join(' · ');
    }).catch(function (e) { el('stats').textContent = '统计失败：' + e.message; });
  }

  function loadDiag() {
    el('diag').hidden = false;
    api('/api/stats').then(function (r) {
      var c = (r.stats && r.stats.counters) || {};
      var lines = Object.keys(c).sort().map(function (k) { return k + ' = ' + c[k]; });
      el('diag-counters').textContent = lines.length ? lines.join('\n') : '（还没有计数）';
    }).catch(function (e) { el('diag-counters').textContent = '失败：' + e.message; });
    api('/api/reads?limit=8').then(function (r) {
      var rows = r.rows || [];
      if (!rows.length) { el('diag-reads').textContent = '（还没有检索记录）'; return; }
      el('diag-reads').textContent = rows.map(function (x) {
        var g = x.groups || {}, gt = Object.keys(g).map(function (k) { return k + g[k]; }).join('/') || '-';
        return x.at + '  「' + (x.query || '') + '」\n  候选 ' + (x.candidates || 0) + ' → 命中 ' + (x.hits || 0) +
          ' → 注入 ' + (x.injected || 0) + '（' + gt + '）' + (x.dropped && x.dropped.length ? '，丢弃 ' + x.dropped.length + ' 条' : '');
      }).join('\n');
    }).catch(function (e) { el('diag-reads').textContent = '失败：' + e.message; });
    api('/api/discarded?limit=8').then(function (r) {
      var rows = r.rows || [];
      el('diag-discarded').textContent = rows.length
        ? rows.map(function (x) { return x.at + '  「' + (x.content || '') + '」\n  ' + (x.reason || '-'); }).join('\n')
        : '（还没有丢弃记录）';
    }).catch(function (e) { el('diag-discarded').textContent = '失败：' + e.message; });
  }

  function loadList() {
    var q = '?status=' + encodeURIComponent(el('f-status').value) +
            '&type=' + encodeURIComponent(el('f-type').value) + '&limit=200';
    api('/api/list' + q).then(function (r) {
      state.rows = r.rows || [];
      renderList();
    }).catch(function (e) {
      el('rows').innerHTML = '<div class="empty">列不出来：' + esc(e.message) + '</div>';
    });
  }

  function renderList() {
    var box = el('rows');
    if (!state.rows.length) { box.innerHTML = '<div class="empty">没有符合条件的记忆。</div>'; return; }
    box.innerHTML = state.rows.map(function (r) {
      var sel = r.id === state.selected ? ' sel' : '';
      return '<div class="row' + sel + '" data-id="' + esc(r.id) + '">' +
        '<div class="text">' + esc(r.content) + '</div>' +
        '<div><span class="chip ' + esc(r.status) + '">' + esc(r.status) + '</span></div>' +
        '<div class="meta">' +
          '<span class="chip">' + esc(r.type) + '/' + esc(r.subtype) + '</span>' +
          '<span class="chip ' + esc(r.injection_policy) + '">' + esc(r.injection_policy) + '</span>' +
          '<span class="chip">' + esc(r.importance) + '</span>' +
          (r.domain_key ? '<span class="chip">' + esc(r.domain_key) + '</span>' : '') +
          '<span>' + esc(r.updated_at || '') + '</span>' +
        '</div>' +
      '</div>';
    }).join('');
    Array.prototype.forEach.call(box.querySelectorAll('.row'), function (node) {
      node.addEventListener('click', function () { select(node.getAttribute('data-id')); });
    });
  }

  function select(id) {
    state.selected = id;
    renderList();
    api('/api/explain?id=' + encodeURIComponent(id))
      .then(function (r) { state.explain = r.explain; renderDetail(); })
      .catch(function (e) { el('detail').innerHTML = '<div class="empty">打不开：' + esc(e.message) + '</div>'; });
  }

  function renderDetail() {
    var d = state.explain;
    if (!d) return;
    var cur = d.current || {};
    var status = cur.status || '';
    var isDeleted = status === 'deleted' || status === 'archived';
    var counts = d.counts || {};
    var timeline = d.timeline || [];
    var versions = d.versions || [];
    var related = d.related || {};

    var html = '';
    html += '<div class="field"><label>内容</label><textarea id="d-content">' + esc(cur.content) + '</textarea></div>';
    html += '<div class="reason">' + esc(cur.id) + ' · ' + esc(cur.type) + '/' + esc(cur.subtype) +
            ' · 主体 ' + esc(cur.subject_type) + (cur.subject_name ? ':' + esc(cur.subject_name) : '') +
            ' · 被注入 ' + esc(counts.injected || 0) + ' 次 / 被检索 ' + esc(counts.used || 0) + ' 次</div>';

    html += '<div class="inline">' +
      '<div class="field"><label>重要性</label><select id="d-importance">' +
        ['key', 'important', 'normal', 'low'].map(function (v) {
          return '<option value="' + v + '"' + (cur.importance === v ? ' selected' : '') + '>' + v + '</option>';
        }).join('') + '</select></div>' +
      '<div class="field"><label>注入策略</label><select id="d-policy">' +
        ['always', 'contextual', 'on_demand', 'never'].map(function (v) {
          return '<option value="' + v + '"' + (cur.injection_policy === v ? ' selected' : '') + '>' + v + '</option>';
        }).join('') + '</select></div>' +
      '<div class="field"><label>置信度</label><input id="d-confidence" type="number" step="0.05" min="0" max="1" value="' + esc(cur.confidence) + '"></div>' +
      '<div class="field"><label>领域</label><input id="d-domain" type="text" value="' + esc(cur.domain_key || '') + '" disabled></div>' +
    '</div>';
    html += '<div class="field"><label>修改理由（会写进变更历史）</label>' +
            '<input id="d-reason" type="text" placeholder="例如：用户更正了年份" style="width:100%"></div>';

    html += '<div class="actions">' +
      '<button class="primary" id="btn-save"' + (status === 'deleted' || status === 'superseded' ? ' disabled' : '') + '>保存修改</button>' +
      (status === 'deleted' || status === 'archived'
        ? '<button id="btn-restore">恢复</button>'
        : '<button class="danger" id="btn-forget">忘记</button>') +
      (status === 'deleted' ? '<button class="danger" id="btn-purge">物理删除</button>' : '') +
    '</div>';

    html += '<h2>为什么是现在这样</h2><div class="reason">' + esc(d.state_reason || '') + '</div>';

    html += '<h2>变更历史</h2>';
    if (!timeline.length) {
      html += '<div class="empty">没有变更记录。</div>';
    } else {
      html += '<table><tr><th>时间</th><th>动作</th><th>谁</th><th>理由</th><th>改了什么</th></tr>';
      timeline.forEach(function (c) {
        var diff = c.diff || {};
        var parts = Object.keys(diff).map(function (k) {
          var v = diff[k] || {};
          return '<span class="diff">' + esc(k) + ': ' + esc(v.from) + ' → ' + esc(v.to) + '</span>';
        });
        html += '<tr><td>' + esc(c.at) + '</td><td>' + esc(c.action) + '</td><td>' + esc(c.actor) +
                '</td><td>' + esc(c.reason || '-') + '</td><td>' + (parts.join('<br>') || '（新建）') + '</td></tr>';
      });
      html += '</table>';
    }

    if (versions.length) {
      html += '<h2>历史版本</h2><table><tr><th>时间</th><th>旧版</th><th>新版</th></tr>';
      versions.forEach(function (v) {
        function txt(x) { return x && typeof x === 'object' ? (x.content || '') : String(x || ''); }
        html += '<tr><td>' + esc(v.at) + '</td><td>' + esc(txt(v.from)) + '</td><td>' + esc(txt(v.to)) + '</td></tr>';
      });
      html += '</table>';
    }

    var rel = [];
    function label(x) { return x && typeof x === 'object' ? (x.id + '（' + (x.content || '').slice(0, 40) + '）') : String(x); }
    if (related.superseded_by) rel.push('被 ' + esc(label(related.superseded_by)) + ' 取代');
    (related.supersedes || []).forEach(function (x) { rel.push('取代了 ' + esc(label(x))); });
    (related.merged_from || []).forEach(function (x) { rel.push('由 ' + esc(label(x)) + ' 合并而来'); });
    (related.same_slot_active || []).forEach(function (x) { rel.push('同槽位仍在生效：' + esc(label(x))); });
    if (rel.length) {
      html += '<h2>关系</h2><ul>' + rel.map(function (t) { return '<li>' + t + '</li>'; }).join('') + '</ul>';
    }

    el('detail').innerHTML = html;

    if (el('btn-save')) el('btn-save').addEventListener('click', save);
    if (el('btn-forget')) el('btn-forget').addEventListener('click', forgetSoft);
    if (el('btn-restore')) el('btn-restore').addEventListener('click', restore);
    if (el('btn-purge')) el('btn-purge').addEventListener('click', purge);
  }

  function save() {
    var body = {
      id: state.selected,
      content: el('d-content').value.trim(),
      importance: el('d-importance').value,
      injection_policy: el('d-policy').value,
      confidence: Number(el('d-confidence').value),
      reason: (el('d-reason') && el('d-reason').value.trim()) || '用户在管理页修改',
    };
    api('/api/update', {
      method: 'POST',
      headers: { 'content-type': 'application/json' },
      body: JSON.stringify(body),
    }).then(function () {
      toast('已保存');
      loadStats();
      loadList();
      select(state.selected);
    }).catch(function (e) { toast('保存失败：' + e.message, true); });
  }

  function forgetSoft() {
    if (!confirm('忘记这条？它会从检索和注入里消失，但仍可恢复。')) return;
    api('/api/forget', {
      method: 'POST',
      headers: { 'content-type': 'application/json' },
      body: JSON.stringify({ id: state.selected }),
    }).then(function () {
      toast('已忘记（可恢复）');
      loadStats();
      loadList();
      select(state.selected);
    }).catch(function (e) { toast('操作失败：' + e.message, true); });
  }

  function restore() {
    api('/api/restore', {
      method: 'POST',
      headers: { 'content-type': 'application/json' },
      body: JSON.stringify({ id: state.selected }),
    }).then(function () {
      toast('已恢复');
      loadStats();
      loadList();
      select(state.selected);
    }).catch(function (e) { toast('恢复失败：' + e.message, true); });
  }

  function purge() {
    if (!confirm('物理删除后不可恢复。确认？')) return;
    api('/api/forget', {
      method: 'POST',
      headers: { 'content-type': 'application/json' },
      body: JSON.stringify({ id: state.selected, hard: true }),
    }).then(function () {
      toast('已物理删除');
      state.selected = null;
      state.explain = null;
      el('detail').innerHTML = '<div class="empty">这条已经彻底删除了。</div>';
      loadStats();
      loadList();
    }).catch(function (e) { toast('删除失败：' + e.message, true); });
  }

  el('btn-refresh').addEventListener('click', function () { loadStats(); loadList(); });
  if (el('btn-diag')) el('btn-diag').addEventListener('click', loadDiag);
  if (el('btn-diag-close')) el('btn-diag-close').addEventListener('click', function () { el('diag').hidden = true; });
  el('btn-tick').addEventListener('click', function () {
    api('/api/tick', { method: 'POST' }).then(function (r) {
      var res = r.result || {};
      var moved = (res.archived || []).length;
      toast(moved ? ('已归档 ' + moved + ' 条') : '没有需要归档的条目');
      loadStats();
      loadList();
    }).catch(function (e) { toast('维护失败：' + e.message, true); });
  });
  el('f-status').addEventListener('change', loadList);
  el('f-type').addEventListener('change', loadList);

  loadStats();
  loadList();
})();
</script>
</body>
</html>
`;
