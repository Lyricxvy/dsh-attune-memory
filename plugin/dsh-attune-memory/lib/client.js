/**
 * @dsh-attune-memory — Client half (the memory panel in DSH's sidebar).
 *
 * The host half (lib/index.js) owns the engine, the injection channels and the
 * fenced HTTP routes. This half only renders: one icon row in the sidebar's
 * panel list plus one page in the layout's `main` keyed slot under the same id,
 * so clicking the icon selects the page.
 *
 * Data comes from the plugin's own routes over same-origin `fetch` — the exact
 * requests the standalone page at /dsh-attune-memory/ makes, so the host's
 * trust fence (`connection.requestRejection`) applies unchanged and this bundle
 * needs no extra transport service.
 *
 * Scope (PLAN_MEMORY_UI.md): view, edit, forget/restore, hard delete behind a
 * second confirmation. There is no "set status to superseded" control: a status
 * is the result of the fact changing, not a value a user picks (MEMORY_LIFECYCLE
 * §7).
 *
 * No build step: DSH loads this file as a Cordis client bundle via the
 * `dsh.client` declaration in package.json.
 */
window.__ModuleLoader__.load({
  id: "dsh-attune-memory",
  factory: (require) => {
    var module = { exports: {} };
    var exports = module.exports;
    Object.defineProperty(exports, Symbol.toStringTag, { value: "Module" });

    const React = require("react");
    const h = React.createElement;

    /** The `main` slot key and `sidebar.panellist` id — one id for both, so the
     * sidebar row and the page it selects cannot drift apart. */
    const PANEL_ID = "attune-memory";
    /** Same prefix lib/routes.js mounts (ROUTE_BASE). */
    const API = "/dsh-attune-memory/api";
    const FULL_PAGE = "/dsh-attune-memory/";

    const TYPES = [
      ["profile", "画像"],
      ["domain", "领域"],
      ["state", "状态"],
      ["episodic", "事件"]
    ];
    const STATUSES = [
      ["active", "生效"],
      ["superseded", "已取代"],
      ["archived", "已归档"],
      ["deleted", "已删除"],
      ["all", "全部"]
    ];
    const IMPORTANCES = [
      ["key", "key 关键"],
      ["important", "important 重要"],
      ["normal", "normal 一般"],
      ["low", "low 次要"]
    ];
    const POLICIES = [
      ["always", "always 每轮"],
      ["contextual", "contextual 相关时"],
      ["on_demand", "on_demand 按需"],
      ["never", "never 不注入"]
    ];
    const SCOPES = [
      ["global", "global 全局"],
      ["domain", "domain 领域"],
      ["task", "task 任务"]
    ];
    const SUBTYPES = {
      profile: ["identity", "preference", "interaction", "skill", "constraint"],
      domain: ["convention", "fact", "decision", "pitfall"],
      state: ["goal", "activity", "situation"],
      episodic: ["event", "decision", "lesson"]
    };
    const STATUS_TONE = { active: "ok", superseded: "warn", archived: "dim", deleted: "danger" };
    const SUBTYPE_LABEL = {
      identity: "身份",
      preference: "偏好",
      interaction: "交互方式",
      skill: "技能",
      constraint: "约束",
      convention: "约定",
      fact: "事实",
      decision: "决定",
      pitfall: "坑",
      goal: "目标",
      activity: "在做的事",
      situation: "处境",
      event: "事件",
      lesson: "教训"
    };

    const C = {
      text: "var(--dsw-alias-text-primary, #e8e8ef)",
      dim: "var(--dsw-alias-text-secondary, #9d9dae)",
      faint: "var(--dsw-alias-text-tertiary, #6f6f80)",
      line: "var(--dsw-alias-border-l3, #33333d)",
      bg: "var(--dsw-alias-bg-base, #1b1b22)",
      card: "var(--dsw-alias-bg-layer-3, #23232c)",
      cardAlt: "var(--dsw-alias-bg-layer-2, #1f1f27)",
      accent: "var(--dsw-alias-brand-primary, #6b8afd)",
      danger: "var(--dsw-alias-state-error-primary, #f2a1a1)",
      warn: "var(--dsw-alias-state-warning-primary, #e6c07b)",
      ok: "var(--dsw-alias-state-success-primary, #8fd18f)"
    };
    const TONE_COLOR = { ok: C.ok, warn: C.warn, danger: C.danger, dim: C.dim, accent: C.accent };

    const S = {
      root: {
        boxSizing: "border-box",
        height: "100%",
        display: "flex",
        flexDirection: "column",
        background: C.bg,
        color: C.text,
        fontSize: 13
      },
      header: {
        display: "flex",
        alignItems: "center",
        gap: 10,
        padding: "calc(var(--dsh-frame-top-clearance, 48px) + 4px) 20px 10px",
        borderBottom: `1px solid ${C.line}`
      },
      title: { margin: 0, fontSize: 17, fontWeight: 650 },
      spacer: { flex: 1 },
      body: { flex: 1, display: "flex", minHeight: 0 },
      listCol: {
        width: 372,
        minWidth: 280,
        borderRight: `1px solid ${C.line}`,
        display: "flex",
        flexDirection: "column",
        minHeight: 0
      },
      filters: { padding: "10px 12px", borderBottom: `1px solid ${C.line}`, display: "flex", flexDirection: "column", gap: 8 },
      list: { flex: 1, overflowY: "auto", padding: "8px 8px 24px" },
      detailCol: { flex: 1, overflowY: "auto", padding: "16px 20px 32px", minWidth: 0 },
      input: {
        width: "100%",
        boxSizing: "border-box",
        padding: "6px 8px",
        borderRadius: 6,
        border: `1px solid ${C.line}`,
        background: C.cardAlt,
        color: C.text,
        fontSize: 12,
        fontFamily: "inherit"
      },
      button: {
        padding: "5px 10px",
        borderRadius: 6,
        border: `1px solid ${C.line}`,
        background: C.card,
        color: C.text,
        fontSize: 12,
        cursor: "pointer"
      },
      row: {
        display: "block",
        width: "100%",
        textAlign: "left",
        padding: "9px 10px",
        marginBottom: 6,
        borderRadius: 8,
        border: `1px solid ${C.line}`,
        background: C.cardAlt,
        color: C.text,
        cursor: "pointer",
        fontFamily: "inherit",
        fontSize: 12
      },
      rowActive: {
        border: `1px solid ${C.accent}`,
        background: C.card
      },
      meta: { color: C.faint, fontSize: 11, marginTop: 4, display: "flex", gap: 6, flexWrap: "wrap" },
      content: { marginTop: 4, lineHeight: 1.55, color: C.text, wordBreak: "break-word" },
      section: { marginBottom: 18 },
      sectionTitle: { fontSize: 12, fontWeight: 650, color: C.dim, marginBottom: 8, letterSpacing: 0.3 },
      card: { border: `1px solid ${C.line}`, borderRadius: 10, background: C.cardAlt, padding: "12px 14px" },
      field: { display: "flex", flexDirection: "column", gap: 4, marginBottom: 10 },
      label: { fontSize: 11, color: C.dim },
      table: { width: "100%", borderCollapse: "collapse", fontSize: 12 },
      th: { textAlign: "left", color: C.faint, fontWeight: 500, padding: "4px 8px 4px 0", whiteSpace: "nowrap", verticalAlign: "top" },
      td: { padding: "4px 0", color: C.text, verticalAlign: "top", wordBreak: "break-word" }
    };

    function badge(text, tone) {
      const color = TONE_COLOR[tone ?? "dim"] ?? C.dim;
      return h(
        "span",
        {
          style: {
            display: "inline-block",
            padding: "1px 7px",
            borderRadius: 999,
            border: `1px solid ${color}`,
            color,
            fontSize: 10.5,
            lineHeight: 1.6,
            whiteSpace: "nowrap"
          }
        },
        text
      );
    }

    function stamp(iso) {
      if (typeof iso !== "string" || iso.length < 16) return "—";
      return `${iso.slice(0, 10)} ${iso.slice(11, 16)}`;
    }

    function typeLabel(row) {
      const head = row.type === "profile" ? "画像" : row.type === "domain" ? "领域" : row.type === "state" ? "状态" : "事件";
      const sub = SUBTYPE_LABEL[row.subtype] ?? row.subtype ?? "";
      const who = row.subject_type && row.subject_type !== "user" ? ` · ${row.subject_name ?? row.subject_type}` : "";
      return `${head}/${sub}${who}`;
    }

    /** Fetch one JSON endpoint. `requestRejection` answers 401 to peers the host
     * does not trust; surface that as its own message instead of "HTTP 401". */
    async function request(path, init) {
      const response = await fetch(`${API}${path}`, {
        credentials: "same-origin",
        headers: init?.body === undefined ? undefined : { "content-type": "application/json" },
        ...init
      });
      if (response.status === 401 || response.status === 403) {
        throw new Error("宿主拒绝了本次请求（信任栅栏）。请从 DSH 界面里打开这个面板。");
      }
      let payload = null;
      try {
        payload = await response.json();
      } catch {
        payload = null;
      }
      if (!response.ok || payload?.ok === false) {
        throw new Error(payload?.error ?? `HTTP ${response.status}`);
      }
      return payload;
    }

    const getJson = (path) => request(path);
    const postJson = (path, body) => request(path, { method: "POST", body: JSON.stringify(body ?? {}) });

    function Select(props) {
      return h(
        "select",
        {
          style: S.input,
          value: props.value,
          onChange: (event) => props.onChange(event.currentTarget.value),
          "aria-label": props.label
        },
        props.options.map(([value, label]) => h("option", { key: value, value }, label))
      );
    }

    function Field(props) {
      return h(
        "label",
        { style: S.field },
        h("span", { style: S.label }, props.label),
        props.children
      );
    }

    /** A scrollable list of one catalog. */
    function RowButton(props) {
      const { row, active, onClick } = props;
      return h(
        "button",
        {
          type: "button",
          style: active ? { ...S.row, ...S.rowActive } : S.row,
          onClick,
          title: row.content
        },
        h(
          "div",
          { style: { display: "flex", alignItems: "center", gap: 6, flexWrap: "wrap" } },
          badge(typeLabel(row), row.type === "state" ? "accent" : "dim"),
          badge(STATUSES.find(([value]) => value === row.status)?.[1] ?? row.status, STATUS_TONE[row.status])
        ),
        h("div", { style: S.content }, row.content),
        h(
          "div",
          { style: S.meta },
          h("span", null, `更新 ${stamp(row.updated_at)}`),
          row.domain_key ? h("span", null, `· ${row.domain_key}`) : null,
          row.injection_policy ? h("span", null, `· ${row.injection_policy}`) : null
        )
      );
    }

    function Empty(props) {
      return h(
        "div",
        { style: { padding: "28px 16px", color: C.faint, fontSize: 12, lineHeight: 1.8, textAlign: "center" } },
        props.title,
        props.hint ? h("div", { style: { marginTop: 6 } }, props.hint) : null
      );
    }

    /** A destructive confirmation that collects a reason. `confirmLabel` states
     * the effect; `danger` paints it red; `acknowledge` demands a typed phrase. */
    function ConfirmDialog(props) {
      const { title, body, confirmLabel, danger, acknowledge, onCancel, onConfirm } = props;
      const [reason, setReason] = React.useState("");
      const [typed, setTyped] = React.useState("");
      const blocked = acknowledge !== undefined && typed.trim() !== acknowledge;
      return h(
        "div",
        {
          style: {
            position: "fixed",
            inset: 0,
            background: "var(--dsw-alias-bg-mask-1, rgba(0,0,0,.55))",
            display: "flex",
            alignItems: "center",
            justifyContent: "center",
            zIndex: 2147483000
          }
        },
        h(
          "div",
          { style: { ...S.card, width: 420, maxWidth: "90vw", background: C.card } },
          h("div", { style: { fontSize: 14, fontWeight: 650, marginBottom: 8 } }, title),
          h("div", { style: { color: C.dim, fontSize: 12, lineHeight: 1.7, marginBottom: 12 } }, body),
          h(Field, { label: "原因（写进变更时间轴，可留空）" }, h("input", {
            style: S.input,
            value: reason,
            onChange: (event) => setReason(event.currentTarget.value),
            placeholder: "例如：这条记错了"
          })),
          acknowledge !== undefined &&
            h(Field, { label: `输入 ${acknowledge} 以确认` }, h("input", {
              style: S.input,
              value: typed,
              onChange: (event) => setTyped(event.currentTarget.value),
              placeholder: acknowledge
            })),
          h(
            "div",
            { style: { display: "flex", justifyContent: "flex-end", gap: 8, marginTop: 8 } },
            h("button", { type: "button", style: S.button, onClick: onCancel }, "取消"),
            h(
              "button",
              {
                type: "button",
                disabled: blocked,
                style: {
                  ...S.button,
                  borderColor: danger ? C.danger : C.accent,
                  color: danger ? C.danger : C.accent,
                  opacity: blocked ? 0.5 : 1
                },
                onClick: () => onConfirm(reason)
              },
              confirmLabel
            )
          )
        )
      );
    }

    /** The detail pane: fields, state reason, timeline, versions, relations. */
    function Detail(props) {
      const { detail, row, busy, onSave, onAction } = props;
      const [draft, setDraft] = React.useState(() => draftOf(row));
      const [dirty, setDirty] = React.useState(false);
      const [showVersions, setShowVersions] = React.useState(false);

      React.useEffect(() => {
        setDraft(draftOf(row));
        setDirty(false);
        setShowVersions(false);
      }, [row?.id, row?.updated_at]);

      if (row === null || row === undefined) {
        return h(Empty, { title: "从左边选一条记忆", hint: "修改、忘记、恢复都在这里完成；不可撤销的操作会再问一次。" });
      }

      const patch = (key, value) => {
        setDraft((current) => ({ ...current, [key]: value }));
        setDirty(true);
      };
      const explain = detail?.explain ?? null;
      const timeline = explain?.timeline ?? [];
      const versions = explain?.versions ?? [];
      const related = explain?.related ?? {};
      const subtypes = SUBTYPES[row.type] ?? [row.subtype];

      return h(
        "div",
        { style: { maxWidth: 820 } },
        h(
          "div",
          { style: { display: "flex", gap: 6, flexWrap: "wrap", alignItems: "center", marginBottom: 10 } },
          badge(typeLabel(row), row.type === "state" ? "accent" : "dim"),
          badge(STATUSES.find(([value]) => value === row.status)?.[1] ?? row.status, STATUS_TONE[row.status]),
          badge(row.importance, "dim"),
          badge(row.injection_policy, "dim"),
          badge(row.scope, "dim"),
          h("code", { style: { marginLeft: "auto", color: C.faint, fontSize: 11 } }, row.id)
        ),

        explain?.state_reason
          ? h("div", { style: { ...S.card, marginBottom: 14, color: C.dim, fontSize: 12, lineHeight: 1.7 } }, explain.state_reason)
          : null,

        h(
          "div",
          { style: S.section },
          h("div", { style: S.sectionTitle }, "内容"),
          h("textarea", {
            style: { ...S.input, minHeight: 90, resize: "vertical", lineHeight: 1.65, fontSize: 13 },
            value: draft.content,
            onChange: (event) => patch("content", event.currentTarget.value)
          })
        ),

        h(
          "div",
          { style: { display: "grid", gridTemplateColumns: "repeat(auto-fit, minmax(180px, 1fr))", gap: 10 } },
          h(Field, { label: "重要性（只影响排序）" }, h(Select, { value: draft.importance, options: IMPORTANCES, label: "重要性", onChange: (value) => patch("importance", value) })),
          h(Field, { label: "注入策略（是否每轮注入）" }, h(Select, { value: draft.injection_policy, options: POLICIES, label: "注入策略", onChange: (value) => patch("injection_policy", value) })),
          h(Field, { label: "作用域" }, h(Select, { value: draft.scope, options: SCOPES, label: "作用域", onChange: (value) => patch("scope", value) })),
          h(
            "label",
            { style: S.field },
            h("span", { style: S.label }, `置信度 ${Number(draft.confidence).toFixed(2)}`),
            h("input", {
              type: "range",
              min: 0,
              max: 1,
              step: 0.05,
              value: draft.confidence,
              onChange: (event) => patch("confidence", Number(event.currentTarget.value))
            })
          ),
          h(Field, { label: "子类型" }, h(Select, { value: draft.subtype, options: subtypes.map((value) => [value, `${value} ${SUBTYPE_LABEL[value] ?? ""}`]), label: "子类型", onChange: (value) => patch("subtype", value) })),
          h(Field, { label: "关键词（逗号分隔）" }, h("input", {
            style: S.input,
            value: draft.keywords,
            onChange: (event) => patch("keywords", event.currentTarget.value)
          }))
        ),

        h(
          "div",
          { style: { display: "flex", alignItems: "center", gap: 8, margin: "6px 0 18px", flexWrap: "wrap" } },
          h(Field, { label: "修改理由（写进时间轴）" }, h("input", {
            style: { ...S.input, width: 260 },
            value: draft.reason,
            placeholder: "可留空",
            onChange: (event) => patch("reason", event.currentTarget.value)
          })),
          h(
            "button",
            {
              type: "button",
              disabled: busy || !dirty,
              style: { ...S.button, marginTop: 16, color: C.accent, borderColor: C.accent, opacity: busy || !dirty ? 0.45 : 1 },
              onClick: () => onSave(draft, row)
            },
            "保存修改"
          ),
          h(
            "div",
            { style: { ...S.spacer, marginTop: 16, display: "flex", gap: 8 } },
            row.status === "deleted" || row.status === "archived"
              ? h(
                  "button",
                  { type: "button", disabled: busy, style: S.button, onClick: () => onAction("restore", row) },
                  "恢复"
                )
              : h(
                  "button",
                  { type: "button", disabled: busy, style: { ...S.button, color: C.warn }, onClick: () => onAction("forget", row) },
                  "忘记"
                ),
            h(
              "button",
              {
                type: "button",
                disabled: busy,
                style: { ...S.button, color: C.danger, borderColor: C.danger },
                onClick: () => onAction("purge", row)
              },
              "物理删除"
            )
          )
        ),

        h(
          "div",
          { style: S.section },
          h("div", { style: S.sectionTitle }, `变更时间轴 · ${timeline.length}`),
          timeline.length === 0
            ? h(Empty, { title: "没有变更记录" })
            : h(
                "table",
                { style: S.table },
                h("tbody", null, timeline.map((entry, index) =>
                  h(
                    "tr",
                    { key: `${entry.at ?? ""}-${index}` },
                    h("td", { style: { ...S.td, ...S.th, width: 132 } }, stamp(entry.at)),
                    h("td", { style: { ...S.td, width: 88 } }, entry.action),
                    h("td", { style: { ...S.td, color: C.dim } }, entry.reason ?? "—"),
                    h("td", { style: { ...S.td, ...S.th, width: 64 } }, entry.actor ?? "")
                  )
                ))
              )
        ),

        versions.length > 0 &&
          h(
            "div",
            { style: S.section },
            h(
              "div",
              { style: { ...S.sectionTitle, cursor: "pointer" }, onClick: () => setShowVersions((value) => !value) },
              `${showVersions ? "▾" : "▸"} 历史版本 · ${versions.length}（旧值 → 新值）`
            ),
            showVersions &&
              versions.map((version, index) =>
                h(
                  "div",
                  { key: `${version.at ?? ""}-${index}`, style: { ...S.card, marginBottom: 8 } },
                  h("div", { style: { color: C.faint, fontSize: 11, marginBottom: 6 } }, `${stamp(version.at)} · ${version.reason ?? "—"}`),
                  Object.entries(version.diff ?? {}).map(([field, change]) =>
                    h(
                      "div",
                      { key: field, style: { fontSize: 12, lineHeight: 1.7, marginBottom: 4 } },
                      h("span", { style: { color: C.dim } }, `${field}: `),
                      h("span", { style: { color: C.danger, textDecoration: "line-through" } }, format(change?.from)),
                      h("span", { style: { color: C.faint } }, " → "),
                      h("span", { style: { color: C.ok } }, format(change?.to))
                    )
                  )
                )
              )
          ),

        h(
          "div",
          { style: S.section },
          h("div", { style: S.sectionTitle }, "关系"),
          h(
            "div",
            { style: { color: C.dim, fontSize: 12, lineHeight: 1.9 } },
            related.superseded_by ? h("div", null, `被取代者：${related.superseded_by.id}（${related.superseded_by.content ?? ""}）`) : null,
            related.supersedes ? h("div", null, `本条取代了：${related.supersedes.id}（${related.supersedes.content ?? ""}）`) : null,
            Array.isArray(related.merged_from) && related.merged_from.length > 0
              ? h("div", null, `由 ${related.merged_from.length} 条合并而来：${related.merged_from.map((item) => item.id).join("、")}`)
              : null,
            Array.isArray(related.same_slot_active) && related.same_slot_active.length > 0
              ? h("div", null, `同槽位仍生效：${related.same_slot_active.map((item) => item.id).join("、")}`)
              : null,
            h(
              "div",
              { style: { color: C.faint } },
              `来源 ${row.source_message_id ? `消息 ${row.source_message_id}` : "—"} · 建于 ${stamp(row.created_at)} · 注入 ${explain?.counts?.injected ?? row.inject_count ?? 0} 次 / 命中 ${explain?.counts?.used ?? row.use_count ?? 0} 次`
            )
          )
        ),
        explain === null ? h(Empty, { title: "正在读取解释…" }) : null
      );
    }

    function format(value) {
      if (value === null || value === undefined || value === "") return "（空）";
      if (Array.isArray(value)) return value.join(", ");
      return String(value);
    }

    function draftOf(row) {
      return {
        content: row?.content ?? "",
        importance: row?.importance ?? "normal",
        injection_policy: row?.injection_policy ?? "contextual",
        scope: row?.scope ?? "global",
        confidence: Number(row?.confidence ?? 0.8),
        subtype: row?.subtype ?? "",
        keywords: Array.isArray(row?.keywords) ? row.keywords.join(", ") : row?.keywords ?? "",
        reason: ""
      };
    }

    /** The panel page (mounted in the layout's `main` keyed slot). */
    function MemoryPanel() {
      const [status, setStatus] = React.useState("active");
      const [type, setType] = React.useState("");
      const [query, setQuery] = React.useState("");
      const [rows, setRows] = React.useState([]);
      const [selectedId, setSelectedId] = React.useState(null);
      const [detail, setDetail] = React.useState(null);
      const [busy, setBusy] = React.useState(false);
      const [error, setError] = React.useState(null);
      const [notice, setNotice] = React.useState(null);
      const [confirm, setConfirm] = React.useState(null);
      const [stats, setStats] = React.useState(null);

      const loadList = React.useCallback(async () => {
        setError(null);
        const params = new URLSearchParams({ limit: "200" });
        if (status !== "all") params.set("status", status);
        if (type !== "") params.set("type", type);
        const payload = await getJson(`/list?${params.toString()}`);
        setRows(Array.isArray(payload.rows) ? payload.rows : []);
        const statsPayload = await getJson("/stats");
        setStats(statsPayload.stats ?? null);
      }, [status, type]);

      const loadDetail = React.useCallback(async (id) => {
        const payload = await getJson(`/explain?id=${encodeURIComponent(id)}`);
        setDetail({ explain: payload.explain });
      }, []);

      React.useEffect(() => {
        let alive = true;
        setBusy(true);
        loadList()
          .catch((failure) => {
            if (alive) setError(failure instanceof Error ? failure.message : String(failure));
          })
          .finally(() => {
            if (alive) setBusy(false);
          });
        return () => {
          alive = false;
        };
      }, [loadList]);

      const refresh = async (id) => {
        setBusy(true);
        try {
          await loadList();
          if (id !== undefined && id !== null) {
            setSelectedId(id);
            await loadDetail(id);
          }
        } catch (failure) {
          setError(failure instanceof Error ? failure.message : String(failure));
        } finally {
          setBusy(false);
        }
      };

      const select = (id) => {
        setSelectedId(id);
        setDetail(null);
        loadDetail(id).catch((failure) => setError(failure instanceof Error ? failure.message : String(failure)));
      };

      const save = async (draft, row) => {
        setBusy(true);
        setError(null);
        try {
          await postJson("/update", {
            id: row.id,
            content: draft.content,
            importance: draft.importance,
            injection_policy: draft.injection_policy,
            scope: draft.scope,
            confidence: draft.confidence,
            subtype: draft.subtype,
            keywords: draft.keywords,
            reason: draft.reason || "在 DSH 记忆面板里修改"
          });
          setNotice("已保存，旧值留在时间轴里");
          await refresh(row.id);
        } catch (failure) {
          setError(failure instanceof Error ? failure.message : String(failure));
        } finally {
          setBusy(false);
        }
      };

      const act = (kind, row) => {
        if (kind === "forget") {
          setConfirm({
            title: "忘记这条记忆？",
            body: "软删除：不再参与检索和注入，但记录仍在，可以恢复。",
            confirmLabel: "忘记",
            danger: false,
            run: async (reason) => {
              await postJson("/forget", { id: row.id, reason: reason || "在 DSH 记忆面板里忘记" });
              setNotice("已忘记（可恢复）");
              await refresh(row.id);
            }
          });
          return;
        }
        if (kind === "purge") {
          setConfirm({
            title: "物理删除这条记忆？",
            body: "不可恢复，变更时间轴也会一起删除。确认这一步是最后的撤销机会。",
            confirmLabel: "永久删除",
            danger: true,
            acknowledge: "删除",
            run: async (reason) => {
              await postJson("/forget", { id: row.id, hard: true, reason: reason || "在 DSH 记忆面板里物理删除" });
              setNotice("已物理删除");
              setSelectedId(null);
              setDetail(null);
              await refresh();
            }
          });
          return;
        }
        if (kind === "restore") {
          setBusy(true);
          postJson("/restore", { id: row.id, reason: "在 DSH 记忆面板里恢复" })
            .then(async () => {
              setNotice("已恢复");
              await refresh(row.id);
            })
            .catch((failure) => setError(failure instanceof Error ? failure.message : String(failure)))
            .finally(() => setBusy(false));
        }
      };

      const visible = query.trim() === ""
        ? rows
        : rows.filter((row) => String(row.content ?? "").toLowerCase().includes(query.trim().toLowerCase()));
      const selectedRow = rows.find((row) => row.id === selectedId) ?? null;
      const byStatus = stats?.by_status ?? {};

      return h(
        "div",
        { style: S.root },
        h(
          "header",
          { style: S.header },
          h("h1", { style: S.title }, "记忆"),
          h("span", { style: { color: C.faint, fontSize: 11 } }, `生效 ${byStatus.active ?? 0} · 取代 ${byStatus.superseded ?? 0} · 总数 ${stats?.total ?? rows.length}`),
          h("span", { style: S.spacer }),
          notice !== null ? h("span", { style: { color: C.ok, fontSize: 11 } }, notice) : null,
          h("button", { type: "button", style: S.button, disabled: busy, onClick: () => refresh(selectedId) }, busy ? "读取中…" : "刷新"),
          h("a", { href: FULL_PAGE, target: "_blank", rel: "noreferrer", style: { color: C.accent, fontSize: 11, textDecoration: "none" } }, "完整页 ↗")
        ),
        error !== null
          ? h(
              "div",
              { style: { padding: "8px 20px", color: C.danger, fontSize: 12, borderBottom: `1px solid ${C.line}` } },
              error
            )
          : null,
        h(
          "div",
          { style: S.body },
          h(
            "div",
            { style: S.listCol },
            h(
              "div",
              { style: S.filters },
              h("input", {
                style: S.input,
                value: query,
                placeholder: "在已加载的条目里搜内容…",
                onChange: (event) => setQuery(event.currentTarget.value)
              }),
              h(
                "div",
                { style: { display: "flex", gap: 6 } },
                h(
                  "div",
                  { style: { flex: 1 } },
                  h(Select, { value: status, options: STATUSES, label: "状态", onChange: (value) => setStatus(value) })
                ),
                h(
                  "div",
                  { style: { flex: 1 } },
                  h(Select, {
                    value: type,
                    options: [["", "全部类型"], ...TYPES],
                    label: "类型",
                    onChange: (value) => setType(value)
                  })
                )
              )
            ),
            h(
              "div",
              { style: S.list },
              visible.length === 0
                ? h(Empty, {
                    title: busy ? "读取中…" : "这个筛选下没有条目",
                    hint: busy ? undefined : "换个状态或类型看看；也可用 memory_remember 让模型记一条。"
                  })
                : visible.map((row) => h(RowButton, { key: row.id, row, active: row.id === selectedId, onClick: () => select(row.id) }))
            )
          ),
          h(
            "div",
            { style: S.detailCol },
            h(Detail, {
              row: selectedRow,
              detail,
              busy,
              onSave: save,
              onAction: act
            })
          )
        ),
        confirm !== null &&
          h(ConfirmDialog, {
            title: confirm.title,
            body: confirm.body,
            confirmLabel: confirm.confirmLabel,
            danger: confirm.danger,
            acknowledge: confirm.acknowledge,
            onCancel: () => setConfirm(null),
            onConfirm: (reason) => {
              const run = confirm.run;
              setConfirm(null);
              setBusy(true);
              setError(null);
              run(reason)
                .catch((failure) => setError(failure instanceof Error ? failure.message : String(failure)))
                .finally(() => setBusy(false));
            }
          })
      );
    }

    /** The sidebar icon. The host calls it with `{ size, active }` and expects a
     * node; an inline SVG keeps the bundle free of icon-package dependencies. */
    function MemoryIcon(props) {
      const size = props?.size ?? 16;
      const color = props?.active === true ? C.accent : "currentColor";
      return h(
        "svg",
        {
          width: size,
          height: size,
          viewBox: "0 0 16 16",
          fill: "none",
          stroke: color,
          strokeWidth: 1.3,
          strokeLinecap: "round",
          strokeLinejoin: "round",
          "aria-hidden": "true"
        },
        // A layered stack: memory is a set of maintained facts, not a chat log.
        h("path", { d: "M2.5 4.5h11M2.5 8h11M2.5 11.5h7" }),
        h("circle", { cx: 12.4, cy: 11.4, r: 2.1 })
      );
    }

    /** Report a mount failure instead of blanking the panel (same idea as the
     * other third-party bundles: a broken surface must stay visible). */
    function report(phase, error) {
      const message = error instanceof Error ? error.stack ?? error.message : String(error);
      // eslint-disable-next-line no-console
      console.error(`[dsh-attune-memory] ${phase} failed:`, message);
    }

    const inject = ["slots"];

    /**
     * Client plugin body.
     * @param ctx - the client Cordis context (slots).
     */
    function apply(ctx) {
      try {
        ctx.slots.inject("sidebar.panellist", () =>
          ctx.slots.register(
            { name: "sidebar.panellist", id: PANEL_ID, order: 40, label: () => "记忆" },
            MemoryIcon
          )
        );
        ctx.slots.inject("main", () => ctx.slots.register({ name: "main", key: PANEL_ID }, MemoryPanel));
      } catch (error) {
        report("register", error);
      }
    }

    exports.apply = apply;
    exports.inject = inject;
    exports.PANEL_ID = PANEL_ID;
    // Internals for the stub test: rendering the detail pane against a recorded
    // engine payload is the only way to check the wire shapes without a browser.
    exports.__test = { Detail, RowButton, request };
    return module.exports;
  }
});
