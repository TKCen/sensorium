(function () {
  "use strict";

  const SDK = window.__HERMES_PLUGIN_SDK__;
  const PLUGINS = window.__HERMES_PLUGINS__;
  if (!SDK || !PLUGINS) return;

  const { React } = SDK;
  const { useEffect, useState } = SDK.hooks;
  const { Card, CardContent, Badge, Button } = SDK.components;
  const h = React.createElement;

  function bandColor(band) {
    if (band === "green") return "#22c55e";
    if (band === "yellow") return "#eab308";
    if (band === "red") return "#ef4444";
    if (band === "violet") return "#828fff";
    return "#94a3b8";
  }

  function get(obj, path, fallback) {
    const parts = path.split(".");
    let cur = obj;
    for (let i = 0; i < parts.length; i += 1) {
      if (!cur || typeof cur !== "object" || !(parts[i] in cur)) return fallback;
      cur = cur[parts[i]];
    }
    return cur === undefined || cur === null ? fallback : cur;
  }

  function fmt(value, digits) {
    if (value === null || value === undefined || value === "") return "—";
    if (typeof value === "number" && Number.isFinite(value)) {
      return value.toLocaleString(undefined, { maximumFractionDigits: digits ?? 2 });
    }
    return String(value);
  }

  function fmtRatio(value) {
    if (value === null || value === undefined || Number.isNaN(Number(value))) return "—";
    return Number(value).toFixed(3);
  }

  function timeText(value) {
    if (!value) return "—";
    const d = new Date(value);
    if (Number.isNaN(d.getTime())) return String(value);
    const diff = d.getTime() - Date.now();
    const abs = Math.round(Math.abs(diff) / 1000);
    const suffix = diff >= 0 ? "" : " ago";
    const prefix = diff >= 0 ? "in " : "";
    if (abs < 90) return prefix + Math.max(1, abs) + "s" + suffix;
    if (abs < 90 * 60) return prefix + Math.max(1, Math.round(abs / 60)) + "m" + suffix;
    if (abs < 48 * 3600) return prefix + Math.max(1, Math.round(abs / 3600)) + "h" + suffix;
    if (abs < 21 * 86400) return prefix + Math.max(1, Math.round(abs / 86400)) + "d" + suffix;
    return d.toLocaleString(undefined, { month: "short", day: "numeric", hour: "2-digit", minute: "2-digit" });
  }

  function listText(items) {
    return items && items.length ? items.join(", ") : "—";
  }

  function plural(n, one, many) {
    return String(n) + " " + (Number(n) === 1 ? one : (many || one + "s"));
  }

  function countsText(obj) {
    if (!obj) return "—";
    return Object.keys(obj).sort().map(function (k) { return k + " " + obj[k]; }).join(", ") || "—";
  }

  function useSnapshot(autoRefreshMs) {
    const [data, setData] = useState(null);
    const [loading, setLoading] = useState(false);
    const [error, setError] = useState("");

    function load() {
      setLoading(true);
      setError("");
      SDK.fetchJSON("/api/plugins/agent-sensorium/snapshot")
        .then(function (d) { setData(d); })
        .catch(function (err) { setError(err && err.message ? err.message : String(err)); })
        .finally(function () { setLoading(false); });
    }

    useEffect(function () {
      load();
      if (!autoRefreshMs) return undefined;
      const id = setInterval(load, autoRefreshMs);
      return function () { clearInterval(id); };
    }, [autoRefreshMs]);

    return { data, loading, error, reload: load };
  }

  function usePluginJson(path, autoRefreshMs) {
    const [data, setData] = useState(null);
    const [loading, setLoading] = useState(false);
    const [error, setError] = useState("");

    function load() {
      setLoading(true);
      setError("");
      SDK.fetchJSON(path)
        .then(function (d) { setData(d); })
        .catch(function (err) { setError(err && err.message ? err.message : String(err)); })
        .finally(function () { setLoading(false); });
    }

    useEffect(function () {
      load();
      if (!autoRefreshMs) return undefined;
      const id = setInterval(load, autoRefreshMs);
      return function () { clearInterval(id); };
    }, [path, autoRefreshMs]);

    return { data, loading, error, reload: load };
  }

  function Pill(props) {
    return h("span", {
      className: "sx-pill" + (props.strong ? " sx-pill-strong" : ""),
      style: props.band ? { borderColor: bandColor(props.band), color: bandColor(props.band) } : undefined,
    }, props.children);
  }

  function MetricCard(props) {
    return h(Card, { className: "sx-card sx-metric-card " + (props.hot ? "sx-metric-hot" : "") },
      h(CardContent, { className: "sx-metric-content" },
        h("div", { className: "sx-metric-label" }, props.label),
        h("div", { className: "sx-metric-value" }, String(props.value ?? "—")),
        props.hint ? h("div", { className: "sx-muted" }, props.hint) : null,
      ),
    );
  }

  function Empty(props) {
    return h(Card, { className: "sx-card sx-empty-card" },
      h(CardContent, { className: "sx-empty" }, props.text || "Nothing visible."),
    );
  }

  function ViewNav(props) {
    const views = props.views && props.views.length ? props.views : [
      { id: "overview", label: "Overview", summary: "Immediate pressure and efficiency.", count: 0, band: "neutral" },
      { id: "perception", label: "Perception", summary: "Signal → event → candidate trace.", count: 0, band: "neutral" },
      { id: "substrate", label: "Substrate", summary: "Candidates, threads, and receipts.", count: 0, band: "neutral" },
      { id: "actuators", label: "Actuators", summary: "Actions, outbox, and artifacts.", count: 0, band: "neutral" },
    ];
    return h("div", { className: "sx-view-nav", role: "tablist", "aria-label": "Sensorium views" },
      views.map(function (v) {
        const active = props.active === v.id;
        return h("button", {
          key: v.id,
          type: "button",
          role: "tab",
          "aria-selected": active ? "true" : "false",
          className: "sx-view-tab" + (active ? " sx-view-tab-active" : ""),
          style: active ? { borderColor: bandColor(v.band), boxShadow: "inset 0 -2px 0 " + bandColor(v.band) } : undefined,
          onClick: function () { props.onChange(v.id); },
        },
          h("span", { className: "sx-view-label" }, v.label || v.id),
          h("span", { className: "sx-view-summary" }, v.summary || ""),
          h(Pill, { band: v.band }, fmt(v.count ?? 0, 0)),
        );
      }),
    );
  }

  function MiniTrend(props) {
    const series = (props.series || []).map(function (row) { return Number(get(row, props.path, NaN)); }).filter(function (v) { return Number.isFinite(v); });
    if (series.length < 2) {
      return h(Card, { className: "sx-card sx-trend-card" }, h(CardContent, { className: "sx-card-content" },
        h("div", { className: "sx-title" }, props.title),
        h("p", { className: "sx-summary" }, "Need at least two samples for a trend."),
      ));
    }
    const width = 260;
    const height = 68;
    const min = Math.min.apply(null, series);
    const max = Math.max.apply(null, series);
    const span = max - min || 1;
    const points = series.map(function (v, i) {
      const x = series.length === 1 ? width / 2 : (i / (series.length - 1)) * width;
      const y = height - ((v - min) / span) * (height - 8) - 4;
      return x.toFixed(1) + "," + y.toFixed(1);
    }).join(" ");
    const first = series[0];
    const last = series[series.length - 1];
    const delta = last - first;
    const deltaText = (delta > 0 ? "+" : "") + delta.toFixed(Math.abs(delta) < 1 ? 3 : 1);
    return h(Card, { className: "sx-card sx-trend-card" }, h(CardContent, { className: "sx-card-content" },
      h("div", { className: "sx-row sx-between" },
        h("div", null, h("div", { className: "sx-title" }, props.title), h("div", { className: "sx-muted" }, props.subtitle || "")),
        h(Pill, { band: props.lowerIsBetter ? (delta <= 0 ? "green" : "yellow") : (delta >= 0 ? "green" : "yellow"), strong: true }, deltaText),
      ),
      h("svg", { className: "sx-spark", viewBox: "0 0 " + width + " " + height, preserveAspectRatio: "none" },
        h("polyline", { points: points, fill: "none", stroke: props.color || "#828fff", strokeWidth: "2.5", strokeLinecap: "round", strokeLinejoin: "round" }),
      ),
      h("div", { className: "sx-meta" },
        h("span", null, "first: ", fmt(first, 3)),
        h("span", null, "last: ", fmt(last, 3)),
        h("span", null, "samples: ", series.length),
      ),
    ));
  }

  function Section(props) {
    return h("section", { className: "sx-section" },
      h("div", { className: "sx-section-head" },
        h("h3", null, props.title),
        props.subtitle ? h("p", null, props.subtitle) : null,
      ),
      props.children,
    );
  }

  function DrillDown(props) {
    return h("details", { className: "sx-drill", open: !!props.open },
      h("summary", null,
        h("span", null, props.title),
        props.badge ? h(Pill, { band: props.band || "neutral" }, props.badge) : null,
      ),
      h("div", { className: "sx-drill-body" }, props.children),
    );
  }

  function ThreadCard(props) {
    const t = props.thread;
    return h(Card, { className: "sx-card sx-thread-card" }, h(CardContent, { className: "sx-card-content" },
      h("div", { className: "sx-row sx-between" },
        h("div", null, h("div", { className: "sx-title" }, t.title || t.id), h("div", { className: "sx-id" }, t.id)),
        h(Badge, { variant: "outline", className: "sx-badge" }, t.status),
      ),
      h("div", { className: "sx-meta" },
        h("span", null, "origin: ", t.origin_candidate_id || "—"),
        h("span", null, "surfaces: ", listText(t.allowed_surfaces || [])),
        h("span", null, "expires: ", timeText(t.expires_at)),
        t.dirty ? h("span", { className: "sx-warn" }, "dirty") : null,
        t.pinned ? h("span", null, "pinned") : null,
      ),
    ));
  }

  function CandidateCard(props) {
    const c = props.candidate;
    const pressure = c.pressure === null || c.pressure === undefined ? "—" : Number(c.pressure).toFixed(2);
    return h(Card, { className: "sx-card" }, h(CardContent, { className: "sx-card-content" },
      h("div", { className: "sx-row sx-between" },
        h("div", null, h("div", { className: "sx-title" }, c.kind || "candidate"), h("div", { className: "sx-id" }, c.id)),
        h(Badge, { variant: "outline", className: "sx-badge sx-pressure" }, pressure),
      ),
      h("p", { className: "sx-summary" }, c.summary || "No summary."),
      h("div", { className: "sx-meta" },
        h("span", null, "status: ", c.status || "—"),
        h("span", null, "surfaces: ", listText(c.allowed_surfaces || [])),
      ),
    ));
  }

  function SignalCard(props) {
    const s = props.signal;
    const strength = s.strength_hint === null || s.strength_hint === undefined ? "—" : Number(s.strength_hint).toFixed(2);
    return h(Card, { className: "sx-card" }, h(CardContent, { className: "sx-card-content" },
      h("div", { className: "sx-row sx-between" },
        h("div", null, h("div", { className: "sx-title" }, s.kind || "signal"), h("div", { className: "sx-id" }, s.id || s.sensor || "—")),
        h(Badge, { variant: "outline", className: "sx-badge sx-pressure" }, s.pressure_level || strength),
      ),
      h("p", { className: "sx-summary" }, s.summary || "No summary."),
      h("div", { className: "sx-meta" },
        h("span", null, "sensor: ", s.sensor || "—"),
        h("span", null, "transition: ", s.transition || "—"),
        h("span", null, "when: ", timeText(s.ts)),
        h("span", null, "keys: ", listText((s.correlation_keys || []).slice(0, 3))),
      ),
    ));
  }

  function ActionCard(props) {
    const a = props.action;
    return h(Card, { className: "sx-card sx-action-card" }, h(CardContent, { className: "sx-card-content" },
      h("div", { className: "sx-row sx-between" },
        h("div", null, h("div", { className: "sx-title" }, a.title || a.intent || "thread action"), h("div", { className: "sx-id" }, a.id)),
        h(Badge, { variant: "outline", className: "sx-badge" }, (a.status || "unknown") + (a.outcome ? " / " + a.outcome : "")),
      ),
      a.summary ? h("p", { className: "sx-summary" }, a.summary) : null,
      a.result_summary ? h("p", { className: "sx-summary sx-result" }, a.result_summary) : null,
      h("div", { className: "sx-meta" },
        h("span", null, "thread: ", a.origin_thread_id || "—"),
        h("span", null, "candidate: ", a.origin_candidate_id || "—"),
        h("span", null, "updated: ", timeText(a.updated_at)),
      ),
      h("div", { className: "sx-ref-row" },
        h(Pill, null, "attachments ", a.attachment_count || 0),
        (a.artifact_refs || []).slice(0, 4).map(function (id) { return h(Pill, { key: "art" + id }, "artifact ", id); }),
        (a.outbox_refs || []).slice(0, 4).map(function (id) { return h(Pill, { key: "obx" + id }, "outbox ", id); }),
      ),
    ));
  }

  function ArtifactGroupCard(props) {
    const g = props.group;
    const items = g.items || [];
    return h(Card, { className: "sx-card sx-artifact-group" }, h(CardContent, { className: "sx-card-content" },
      h("div", { className: "sx-row sx-between" },
        h("div", null, h("div", { className: "sx-title" }, g.title || g.id), h("div", { className: "sx-id" }, g.id)),
        h(Badge, { variant: "outline", className: "sx-badge" }, (g.count || 0) + " artifacts"),
      ),
      h("div", { className: "sx-meta" },
        h("span", null, "type: ", g.group_type || "—"),
        h("span", null, "kinds: ", countsText(g.kinds)),
        h("span", null, "states: ", countsText(g.delivery_states)),
        h("span", null, "updated: ", timeText(g.latest_updated_at)),
      ),
      h("div", { className: "sx-artifact-mini-list" }, items.map(function (a) {
        return h("div", { key: a.id, className: "sx-artifact-mini" },
          h("span", { className: "sx-artifact-mini-title" }, (a.kind || "artifact") + (a.ref_name ? ": " + a.ref_name : "")),
          h("span", { className: "sx-id" }, a.id),
          h("span", { className: "sx-muted" }, a.delivery_state || a.status || "recorded"),
        );
      })),
    ));
  }

  function OutboxCard(props) {
    const o = props.request;
    const safety = o.safety || {};
    return h(Card, { className: "sx-card" }, h(CardContent, { className: "sx-card-content" },
      h("div", { className: "sx-row sx-between" },
        h("div", null, h("div", { className: "sx-title" }, o.title || o.request_type || "outbox request"), h("div", { className: "sx-id" }, o.id)),
        h(Badge, { variant: "outline", className: "sx-badge", style: { borderColor: bandColor(safety.band), color: bandColor(safety.band) } }, safety.label || o.status || "unknown"),
      ),
      o.message_preview ? h("p", { className: "sx-summary" }, o.message_preview) : null,
      h("p", { className: "sx-summary sx-safety" }, safety.detail || "No safety detail."),
      h("div", { className: "sx-meta" },
        h("span", null, o.surface || "—", " / ", o.delivery_mode || "—"),
        h("span", null, "thread: ", o.origin_thread_id || "—", " (", safety.origin_thread_status || "—", ")"),
        h("span", null, "action: ", safety.attached_action_id || "—"),
        h("span", null, "updated: ", timeText(o.updated_at || o.created_at)),
      ),
      h("div", { className: "sx-ref-row" },
        h(Pill, { band: safety.outbound_delivery ? "yellow" : "green" }, safety.outbound_delivery ? "outbound mode" : "no direct delivery"),
        h(Pill, null, safety.dispatch_requires_execute ? "requires execute=True" : "no execute pending"),
      ),
    ));
  }

  function StageChip(props) {
    const s = props.stage;
    return h("span", {
      className: "sx-stage-chip" + (s.reached ? " sx-stage-reached" : " sx-stage-dim"),
      title: s.detail || s.label,
    }, s.label);
  }

  function PerceptionTraceCard(props) {
    const t = props.trace;
    const pressure = t.pressure === null || t.pressure === undefined ? "—" : Number(t.pressure).toFixed(2);
    const stages = t.stages || [];
    const settlement = t.settlement || null;
    const flags = t.flags || [];
    const signals = t.signals || [];
    const events = t.events || [];
    const missingIds = t.missing_event_ids || [];
    const topSignal = signals[0] || null;
    const decisionBand = settlement && settlement.decision === "PROMOTE_CONSCIOUS" ? "violet"
      : settlement && settlement.decision === "SAVE" ? "green"
      : settlement && settlement.decision === "DROP" ? "neutral"
      : "neutral";

    return h(Card, { className: "sx-card sx-trace-card", style: { borderColor: bandColor(t.band) } },
      h(CardContent, { className: "sx-card-content" },
        h("div", { className: "sx-row sx-between" },
          h("div", null,
            h("div", { className: "sx-title" }, (t.kind || "candidate") + " " + (t.candidate_id ? t.candidate_id.slice(0, 16) : "—")),
            h("div", { className: "sx-id" }, t.candidate_id || "—"),
          ),
          h("div", { className: "sx-row", style: { gap: "0.4rem", flexWrap: "wrap" } },
            h(Badge, { variant: "outline", className: "sx-badge sx-pressure" }, pressure),
            t.status ? h(Badge, { variant: "outline", className: "sx-badge" }, t.status) : null,
          ),
        ),
        h("div", { className: "sx-stage-strip" },
          stages.map(function (s, i) { return h(StageChip, { key: s.key || String(i), stage: s }); }),
        ),
        t.summary ? h("p", { className: "sx-summary" }, t.summary) : null,
        settlement ? h("div", { className: "sx-trace-settlement" },
          h("span", { className: "sx-trace-decision", style: { color: bandColor(decisionBand) } }, settlement.decision || "unresolved"),
          settlement.reason ? h("span", { className: "sx-muted" }, settlement.reason) : null,
        ) : null,
        (settlement && (settlement.intake_task_id || settlement.review_task_id || settlement.conscious_task_id)) ? h("div", { className: "sx-meta" },
          settlement.intake_task_id ? h("span", null, "intake: ", settlement.intake_task_id.slice(0, 20)) : null,
          settlement.review_task_id ? h("span", null, "review: ", settlement.review_task_id.slice(0, 20)) : null,
          settlement.conscious_task_id ? h("span", null, "conscious: ", settlement.conscious_task_id.slice(0, 20)) : null,
        ) : null,
        h("div", { className: "sx-meta" },
          h("span", null, "signals: ", signals.length),
          h("span", null, "events: ", events.length),
          t.updated_at ? h("span", null, "updated: ", timeText(t.updated_at)) : null,
        ),
        topSignal ? h("p", { className: "sx-summary sx-muted", style: { fontSize: "0.8rem" } }, "signal: ", topSignal.summary || topSignal.kind || "—") : null,
        missingIds.length ? h("p", { className: "sx-muted", style: { fontSize: "0.78rem" } }, "missing events: ", missingIds.join(", ")) : null,
        flags.length ? h("div", { className: "sx-ref-row" },
          flags.map(function (f) {
            return h(Pill, { key: f, band: (t.band === "red" || t.band === "yellow") ? t.band : undefined }, f);
          }),
        ) : null,
      ),
    );
  }


  function compactId(value, limit) {
    const text = String(value || "");
    const max = limit || 28;
    return text.length > max ? text.slice(0, Math.max(1, max - 1)) + "…" : text;
  }

  function humanizeAtom(value) {
    return String(value || "")
      .replace(/^[a-z]+:/i, "")
      .replace(/[_-]+/g, " ")
      .replace(/\s+/g, " ")
      .trim();
  }

  // ---------------------------------------------------------------------
  // Flow DAG (sera-ck9.3): primary graph view. Fetches /topology +
  // /runtime-status and merges them client-side instead of reusing the
  // /snapshot-shaped inner-life projection below. Configured-but-unobserved
  // topology nodes are first-class here (they come straight from /topology,
  // not from any JSONL row), and configured edges are visually distinct from
  // observed/runtime edges -- which /runtime-status currently always reports
  // as `edges: []`, so this never invents an observed traversal.
  // ---------------------------------------------------------------------

  const FLOW_DAG_COLUMN_ORDER = ["sensor", "emitter", "processor", "queue", "gate", "review", "router", "candidate", "thread", "action", "outbox", "receipt", "sink", "unknown"];
  const FLOW_DAG_COLUMN_LABELS = {
    sensor: "Sense",
    emitter: "Emit",
    processor: "Process",
    queue: "Queue",
    gate: "Gate",
    review: "Review",
    router: "Route",
    candidate: "Candidates",
    thread: "Threads",
    action: "Actions",
    outbox: "Outbox",
    receipt: "Receipts",
    sink: "Surface",
    unknown: "Unknown",
  };
  const FLOW_DAG_NODE_WIDTH = 220;
  const FLOW_DAG_NODE_HEIGHT = 88;
  const FLOW_DAG_COL_GAP = 74;
  const FLOW_DAG_ROW_GAP = 18;
  const FLOW_DAG_LEFT = 28;
  const FLOW_DAG_TOP = 86;

  function flowStatusBand(status) {
    if (status === "active" || status === "settled") return "green";
    if (status === "error" || status === "blocked") return "red";
    if (status === "degraded" || status === "waiting" || status === "reviewing" || status === "held" || status === "processing") return "yellow";
    return "neutral"; // quiet / stale / unknown
  }

  function flowNodeSort(a, b) {
    const aStatus = flowStatusBand(a.status);
    const bStatus = flowStatusBand(b.status);
    const rank = { red: 0, yellow: 1, green: 2, violet: 3, neutral: 4 };
    const ar = rank[aStatus] ?? 9;
    const br = rank[bStatus] ?? 9;
    if (ar !== br) return ar - br;
    if ((a.origin || "") !== (b.origin || "")) return a.origin === "instance" ? -1 : 1;
    return String(a.label || a.id).localeCompare(String(b.label || b.id));
  }

  const FLOW_DAG_KIND_DETAIL = {
    sensor: "Senses one pressure/source and emits compact signals into the inbox.",
    emitter: "Turns an observation into a signal that can enter the pipeline.",
    processor: "Normalizes or reflects on signals before they become pressure.",
    queue: "Holds compact signal/event pressure before promotion decisions.",
    gate: "Filters, dampens, or promotes pressure according to policy.",
    review: "Judgment aperture: subconscious triage or conscious choice before action.",
    router: "Routes reviewed pressure toward attention, Kanban, pointers, or surfaces.",
    candidate: "Live candidate currently asking for review or settlement.",
    thread: "Held/dormant conscious thread that can be resumed or settled.",
    action: "Prepared thread action; still needs explicit lifecycle/authority state.",
    outbox: "Prepared pointer/message artifact; not a direct send unless policy allows it.",
    receipt: "Records settlement/action receipts so choices do not vanish.",
    sink: "Projects compact state outward into the dashboard or operator surface.",
    unknown: "Configured or live item without a mapped display role yet.",
  };

  const FLOW_DAG_LABEL_DETAIL = {
    "active-session salience hook": "Captures corrections, design insights, relational residue, and unresolved live-session pressure.",
    "hindsight pressure": "Checks memory/reflection pressure so old context can influence current salience without flooding it.",
    "inference budget pressure": "Tracks model/quota energy so expensive reasoning is spent deliberately.",
    "kanban pressure": "Reads task-board pressure, blockers, and work-state signals into Sensorium.",
    "machine body pressure": "Local body-state sensor: disk/process/runtime pressure that affects capacity.",
    "media capacity": "Senses media-generation capacity and whether creative lanes are currently practical.",
    "network pressure": "Senses external/network availability and failure pressure.",
    "process pressure": "Senses live process/workload state so stuck or noisy loops become visible.",
    "runtime heartbeat": "Basic clock pulse proving the quiet tick and runtime loop are alive.",
    "tts sidecar pressure": "Watches voice/TTS sidecar capacity and failure pressure.",
    "memory reflection": "Reflects compact memory pressure before it becomes candidate attention.",
    "signal inbox": "Aggregates incoming signals/events before gates or review choose what matters.",
    "promotion gate": "Decides whether pressure stays quiet, becomes a candidate, or opens review.",
    "conscious aperture": "Foreground/conscious review lane for choices that need Sera-level judgment.",
    "subconscious review": "Cheap/background triage that classifies candidates before conscious work.",
    "attention inbox": "Visible attention surface: what can be offered or opened now.",
    "kanban bridge": "Converts approved durable work into task-board pressure without owning judgment.",
    "pointer hook": "Chooses whether a safe, visible pointer can be surfaced to the current chat.",
    "receipt writer": "Writes durable receipts for settlement, presentation, action, or archival decisions.",
    "dashboard projection": "Read-only projection layer for the operator cockpit and trace UI.",
  };

  function flowNodeDetail(node) {
    const labelKey = String(node.label || "").toLowerCase();
    const idKey = humanizeAtom(node.id).toLowerCase();
    if (node.detail) return node.detail;
    if (FLOW_DAG_LABEL_DETAIL[labelKey]) return FLOW_DAG_LABEL_DETAIL[labelKey];
    if (FLOW_DAG_LABEL_DETAIL[idKey]) return FLOW_DAG_LABEL_DETAIL[idKey];
    if (node.origin === "instance") {
      if (node.kind === "candidate") return "Live candidate from the attention inbox; select it to inspect provenance and pressure.";
      if (node.kind === "thread") return "Held or dormant conscious thread; select it to see what keeps it alive.";
      if (node.kind === "action") return "Prepared action record; lifecycle state decides whether it is still work.";
      if (node.kind === "outbox") return "Prepared outbox/pointer record; not a direct delivery by itself.";
    }
    return FLOW_DAG_KIND_DETAIL[node.kind] || FLOW_DAG_KIND_DETAIL.unknown;
  }

  function flowNodeDisplayLabel(node) {
    const label = String(node.label || "");
    if (node.origin === "instance" && (node.kind === "outbox" || node.kind === "action") && /^outbox:|^action:/i.test(label)) {
      return node.kind === "outbox" ? "Prepared outbox pointer" : "Prepared thread action";
    }
    return label || humanizeAtom(node.id) || "unnamed node";
  }

  function findFlowNodeId(nodes, needles, preferredKind) {
    const lowerNeedles = needles.map(function (n) { return String(n).toLowerCase(); });
    const list = Array.from(nodes.values ? nodes.values() : nodes);
    return (list.find(function (n) {
      if (preferredKind && n.kind !== preferredKind) return false;
      const text = [n.id, n.label, n.kind].join(" ").toLowerCase();
      return lowerNeedles.some(function (needle) { return text.indexOf(needle) >= 0; });
    }) || {}).id;
  }

  function buildFlowDagGraph(topoData, runtimeData, attentionData) {
    const nodes = new Map();
    const topoNodes = (topoData && topoData.nodes) || [];
    const topoEdges = (topoData && topoData.edges) || [];
    const runtimeNodes = (runtimeData && runtimeData.nodes) || [];
    const attentionItems = (attentionData && attentionData.items) || [];
    const attentionIds = new Set(attentionItems.map(function (item) { return (item.type || item.kind || "candidate") + ":" + item.id; }));
    const runtimeById = {};
    runtimeNodes.forEach(function (n) { runtimeById[n.id] = n; });

    topoNodes.forEach(function (n) {
      const overlay = runtimeById[n.id];
      nodes.set(n.id, {
        id: n.id,
        kind: n.kind,
        label: n.label || n.id,
        detail: n.detail || n.summary || n.description,
        origin: "topology",
        configured_status: n.configured_status,
        enabled: n.enabled,
        status: overlay ? overlay.status : "unknown",
        status_source: overlay ? overlay.source : "no_runtime_overlay",
      });
    });

    const hasAttentionFilter = attentionIds.size > 0;
    runtimeNodes.forEach(function (n) {
      if (n.origin !== "instance" || nodes.has(n.id)) return;
      const alwaysShow = n.kind === "action" || n.kind === "outbox";
      if (hasAttentionFilter && !attentionIds.has(n.id) && !alwaysShow) return;
      nodes.set(n.id, {
        id: n.id,
        kind: n.kind,
        label: n.label || compactId(n.id),
        detail: n.detail || n.summary || n.description,
        origin: "instance",
        status: n.status,
        status_source: n.source,
      });
    });

    attentionItems.forEach(function (item) {
      const kind = item.type || item.kind || "candidate";
      const id = kind + ":" + item.id;
      if (nodes.has(id)) {
        const existing = nodes.get(id);
        existing.label = item.title || item.summary || existing.label || id;
        existing.detail = item.summary && item.title ? item.summary : (existing.detail || flowNodeDetail(existing));
        existing.status = item.status || existing.status;
        existing.attention = true;
        existing.pressure = item.pressure;
        existing.freshness = item.freshness;
        return;
      }
      nodes.set(id, {
        id: id,
        kind: kind,
        label: item.title || item.summary || id,
        detail: item.summary || null,
        origin: "instance",
        status: item.status || "waiting",
        status_source: "attention_inbox",
        attention: true,
        pressure: item.pressure,
        freshness: item.freshness,
      });
    });

    const edges = topoEdges.map(function (e) {
      return { id: e.id, from: e.from, to: e.to, kind: e.kind, status: e.status, observed: false };
    });

    const subconsciousId = findFlowNodeId(nodes, ["subconscious_review", "Subconscious review"], "review");
    const consciousId = findFlowNodeId(nodes, ["conscious_aperture", "Conscious aperture"], "review");
    const routerId = findFlowNodeId(nodes, ["attention_inbox", "Attention inbox"], "router");
    const pointerId = findFlowNodeId(nodes, ["pointer_hook", "Pointer hook"], "router");
    const kanbanId = findFlowNodeId(nodes, ["kanban_bridge", "Kanban bridge"], "router");
    const receiptId = findFlowNodeId(nodes, ["receipt_writer", "Receipt writer"], "receipt");

    function addProjection(from, to, kind) {
      if (!from || !to || !nodes.has(from) || !nodes.has(to)) return;
      edges.push({ id: "projection:" + from + "->" + to, from: from, to: to, kind: kind || "runtime_projection", observed: true, projection: true });
    }

    Array.from(nodes.values()).forEach(function (node) {
      if (node.origin !== "instance") return;
      if (node.kind === "candidate") addProjection(subconsciousId || routerId, node.id, "current_candidate");
      if (node.kind === "thread") addProjection(consciousId || pointerId || routerId, node.id, "current_thread");
      if (node.kind === "action") addProjection(consciousId || kanbanId, node.id, "prepared_action");
      if (node.kind === "outbox") addProjection(pointerId || kanbanId || receiptId, node.id, "prepared_outbox");
    });

    return {
      nodes: Array.from(nodes.values()),
      edges: edges,
      meta: {
        topology_node_count: topoNodes.length,
        instance_node_count: Array.from(nodes.values()).filter(function (n) { return n.origin === "instance"; }).length,
        configured_edge_count: topoEdges.length,
        projection_edge_count: edges.filter(function (e) { return e.projection; }).length,
        observed_edge_count: 0,
        attention_item_count: attentionItems.length,
      },
    };
  }

  function flowDagLayout(graph) {
    const byKind = {};
    (graph.nodes || []).forEach(function (n) {
      const kind = FLOW_DAG_COLUMN_ORDER.indexOf(n.kind) >= 0 ? n.kind : "unknown";
      byKind[kind] = byKind[kind] || [];
      byKind[kind].push(n);
    });
    Object.keys(byKind).forEach(function (kind) { byKind[kind].sort(flowNodeSort); });
    const cols = FLOW_DAG_COLUMN_ORDER.filter(function (k) { return byKind[k] && byKind[k].length; });
    const width = Math.max(720, FLOW_DAG_LEFT * 2 + cols.length * FLOW_DAG_NODE_WIDTH + Math.max(0, cols.length - 1) * FLOW_DAG_COL_GAP);
    const maxRows = Math.max.apply(null, cols.map(function (k) { return byKind[k].length; }).concat([1]));
    const height = Math.max(360, FLOW_DAG_TOP + maxRows * (FLOW_DAG_NODE_HEIGHT + FLOW_DAG_ROW_GAP) + 30);
    const positions = {};
    const positioned = [];
    cols.forEach(function (kind, ci) {
      const x = FLOW_DAG_LEFT + ci * (FLOW_DAG_NODE_WIDTH + FLOW_DAG_COL_GAP);
      (byKind[kind] || []).forEach(function (node, ri) {
        const y = FLOW_DAG_TOP + ri * (FLOW_DAG_NODE_HEIGHT + FLOW_DAG_ROW_GAP);
        const item = Object.assign({}, node, { x: x, y: y, w: FLOW_DAG_NODE_WIDTH, h: FLOW_DAG_NODE_HEIGHT, col: kind });
        positioned.push(item);
        positions[node.id] = { x: x, y: y, w: FLOW_DAG_NODE_WIDTH, h: FLOW_DAG_NODE_HEIGHT, col: kind };
      });
    });
    return { cols: cols, nodes: positioned, positions: positions, width: width, height: height };
  }

  function FlowDagNode(props) {
    const node = props.node;
    const active = props.selected && props.selected.id === node.id && props.selected.selType === "node";
    const band = flowStatusBand(node.status);
    const displayLabel = flowNodeDisplayLabel(node);
    const detail = flowNodeDetail(node);
    const stateBits = [node.status || "unknown"];
    if (node.origin === "topology" && node.configured_status === "disabled") stateBits.push("disabled");
    if (node.pressure !== null && node.pressure !== undefined) stateBits.push("p " + Number(node.pressure).toFixed(2));
    if (node.freshness) stateBits.push(node.freshness);
    if (node.status_source) stateBits.push(node.status_source);
    return h("button", {
      type: "button",
      className: "sx-flow-node" + (active ? " sx-flow-node-active" : "") + (node.origin === "topology" ? " sx-flow-node-topology" : " sx-flow-node-instance") + (node.attention ? " sx-flow-node-attention" : ""),
      style: { left: node.x + "px", top: node.y + "px", borderColor: bandColor(band) },
      onClick: function () { props.onSelect({ id: node.id, selType: "node", kind: node.kind }); },
      title: displayLabel + " — " + detail + " — " + (node.status || "unknown"),
    },
      h("span", { className: "sx-flow-node-topline" },
        h("span", { className: "sx-flow-node-kind" }, FLOW_DAG_COLUMN_LABELS[node.kind] || node.kind),
        h("span", { className: "sx-flow-node-origin" }, node.origin === "instance" ? "live" : "config"),
      ),
      h("span", { className: "sx-flow-node-label" }, compactId(displayLabel, 46)),
      h("span", { className: "sx-flow-node-state" }, h("span", { className: "sx-flow-state-dot", style: { background: bandColor(band) } }), compactId(stateBits.join(" · "), 62)),
      h("span", { className: "sx-flow-node-id" }, compactId(node.id, 38)),
    );
  }

  function flowEdgePath(a, b) {
    const x1 = a.x + a.w;
    const y1 = a.y + a.h / 2;
    const x2 = b.x;
    const y2 = b.y + b.h / 2;
    if (x2 <= x1 + 8) {
      const lift = Math.max(28, Math.abs(y2 - y1) * 0.35 + 24);
      const midX = Math.max(x1 + 32, x2 + 32);
      return "M" + x1 + " " + y1 + " C" + midX + " " + y1 + "," + midX + " " + (y2 - lift) + "," + (x2 + 4) + " " + y2;
    }
    const bend = Math.min(120, Math.max(44, (x2 - x1) * 0.45));
    return "M" + x1 + " " + y1 + " C" + (x1 + bend) + " " + y1 + "," + (x2 - bend) + " " + y2 + "," + x2 + " " + y2;
  }

  function FlowDagCanvas(props) {
    const layout = flowDagLayout(props.graph || { nodes: [], edges: [] });
    const positions = layout.positions;
    const edges = (props.graph && props.graph.edges) || [];
    const projectionCount = edges.filter(function (edge) { return edge && edge.projection; }).length;
    return h("div", { className: "sx-flow-map-wrap" },
      h("div", { className: "sx-flow-map-canvas", style: { minWidth: layout.width + "px", height: layout.height + "px" } },
        h("svg", { className: "sx-flow-map-svg", viewBox: "0 0 " + layout.width + " " + layout.height, preserveAspectRatio: "xMinYMin meet", "aria-hidden": "true" },
          h("defs", null,
            h("marker", { id: "sx-flow-arrow", markerWidth: "10", markerHeight: "10", refX: "8", refY: "4", orient: "auto", markerUnits: "strokeWidth" },
              h("path", { d: "M0,0 L0,8 L9,4 z", fill: "rgba(130,143,255,0.9)" }),
            ),
          ),
          layout.cols.map(function (kind, i) {
            const x = FLOW_DAG_LEFT + i * (FLOW_DAG_NODE_WIDTH + FLOW_DAG_COL_GAP);
            return h("g", { key: "col-" + kind },
              h("rect", { className: "sx-flow-col-band", x: x - 8, y: 48, width: FLOW_DAG_NODE_WIDTH + 16, height: Math.max(120, layout.height - 58), rx: 14 }),
              h("text", { x: x + FLOW_DAG_NODE_WIDTH / 2, y: 28, className: "sx-flow-col-label", textAnchor: "middle" }, FLOW_DAG_COLUMN_LABELS[kind] || kind),
              h("text", { x: x + FLOW_DAG_NODE_WIDTH / 2, y: 43, className: "sx-flow-col-sub", textAnchor: "middle" }, kind),
            );
          }),
          edges.map(function (edge) {
            const a = positions[edge.from];
            const b = positions[edge.to];
            if (!a || !b) return null;
            const active = props.selected && props.selected.selType === "edge" && props.selected.id === edge.id;
            return h("path", {
              key: edge.id,
              d: flowEdgePath(a, b),
              className: "sx-flow-edge" + (edge.projection ? " sx-flow-edge-projection" : " sx-flow-edge-configured") + (active ? " sx-flow-edge-active" : ""),
              markerEnd: "url(#sx-flow-arrow)",
              onClick: function () { props.onSelect({ id: edge.id, selType: "edge", kind: edge.kind, from: edge.from, to: edge.to }); },
            });
          }),
        ),
        layout.nodes.map(function (node) { return h(FlowDagNode, { key: node.id, node: node, selected: props.selected, onSelect: props.onSelect }); }),
      ),
      h("div", { className: "sx-map-caption" },
        h(Pill, { band: "violet" }, "configured nodes " + layout.nodes.filter(function (n) { return n.origin === "topology"; }).length),
        h(Pill, { band: "yellow" }, "current items " + layout.nodes.filter(function (n) { return n.origin === "instance"; }).length),
        h(Pill, { band: "green" }, "configured edges " + edges.filter(function (edge) { return !edge.projection; }).length),
        h(Pill, null, "projection edges " + projectionCount + " (current-state overlay)"),
      ),
    );
  }

  function useTrace(selected) {
    const [data, setData] = useState(null);
    const [loading, setLoading] = useState(false);
    const [error, setError] = useState("");

    useEffect(function () {
      if (!selected) {
        setData(null);
        setError("");
        return undefined;
      }
      setLoading(true);
      setError("");
      const param = selected.selType === "edge"
        ? "edge_id=" + encodeURIComponent(selected.id)
        : "node_id=" + encodeURIComponent(selected.id);
      SDK.fetchJSON("/api/plugins/agent-sensorium/trace?" + param)
        .then(function (d) { setData(d); })
        .catch(function (err) { setError(err && err.message ? err.message : String(err)); })
        .finally(function () { setLoading(false); });
      return undefined;
    }, [selected && selected.id, selected && selected.selType]);

    return { data: data, loading: loading, error: error };
  }

  function TraceRefList(props) {
    const items = props.items || [];
    if (!items.length) return h("p", { className: "sx-muted" }, props.empty || "None.");
    return h("div", { className: "sx-ref-row" }, items.slice(0, 12).map(function (ref, i) {
      const text = typeof ref === "string" ? ref : ((ref.kind || ref.type || "ref") + ": " + compactId(ref.id || ref.ref || "", 34) + (ref.relation ? " (" + ref.relation + ")" : ""));
      return h(Pill, { key: String(i) + text }, text);
    }));
  }

  function selectedFlowContext(graph, selected) {
    const nodes = (graph && graph.nodes) || [];
    const edges = (graph && graph.edges) || [];
    if (!selected) return {};
    if (selected.selType === "node") {
      return { node: nodes.find(function (n) { return n.id === selected.id; }) || null };
    }
    const edge = edges.find(function (e) { return e.id === selected.id; }) || selected;
    return {
      edge: edge,
      fromNode: nodes.find(function (n) { return n.id === edge.from; }) || null,
      toNode: nodes.find(function (n) { return n.id === edge.to; }) || null,
    };
  }

  function flowStatusMeaning(status, source) {
    const s = String(status || "unknown");
    if (s === "active") return source === "signal_match" ? "Recent signal evidence touched this configured organ." : "Currently active in the compact runtime overlay.";
    if (s === "quiet") return "Configured and healthy-looking, but no recent per-node activity is attached.";
    if (s === "waiting") return "Visible work is waiting for review, dispatch, or lifecycle settlement.";
    if (s === "reviewing") return "Already inside a review/aperture lane.";
    if (s === "processing") return "Prepared work exists and has not settled into a receipt yet.";
    if (s === "held") return "Durable/open thread state is being held rather than forgotten.";
    if (s === "stale") return "The available freshness signal says this lane is old or no longer current.";
    if (s === "error") return "An open/runtime item reports failure state.";
    if (s === "blocked") return "Policy or lifecycle state is blocking progress.";
    if (s === "settled") return "A receipt/settlement has closed the loop.";
    return "No more specific runtime overlay is available yet.";
  }

  function TraceField(props) {
    if (props.value === null || props.value === undefined || props.value === "") return null;
    return h("div", { className: "sx-trace-field" },
      h("span", null, props.label),
      h("strong", null, props.value),
    );
  }

  function TraceDetailRail(props) {
    const selected = props.selected;
    const traceState = props.trace || {};
    if (!selected) {
      return h(Card, { className: "sx-card sx-detail-rail" }, h(CardContent, { className: "sx-card-content" },
        h("div", { className: "sx-title" }, "Trace / provenance"),
        h("p", { className: "sx-summary" }, "Select a node or edge to call /trace and inspect where it came from, what influenced it, and where it leads."),
      ));
    }
    if (traceState.loading && !traceState.data) {
      return h(Card, { className: "sx-card sx-detail-rail" }, h(CardContent, { className: "sx-card-content" },
        h("div", { className: "sx-title" }, "Trace / provenance"),
        h("p", { className: "sx-summary" }, "Loading trace for ", compactId(selected.id), "…"),
      ));
    }
    if (traceState.error) {
      return h(Card, { className: "sx-card sx-detail-rail sx-error" }, h(CardContent, { className: "sx-card-content" },
        h("div", { className: "sx-title" }, "Trace / provenance"),
        h("p", { className: "sx-summary" }, "Trace fetch failed: ", traceState.error),
      ));
    }
    const data = traceState.data || {};
    const subject = data.subject;
    const meta = data.meta || {};
    const limitations = meta.limitations || [];
    if (!subject) {
      return h(Card, { className: "sx-card sx-detail-rail" }, h(CardContent, { className: "sx-card-content" },
        h("div", { className: "sx-title" }, "Trace / provenance"),
        h("p", { className: "sx-summary" }, "No trace available for ", compactId(selected.id), "."),
        limitations.length ? h("p", { className: "sx-muted" }, limitations.join(" ")) : null,
      ));
    }
    const graphCtx = selectedFlowContext(props.graph, selected);
    const graphNode = graphCtx.node;
    const graphEdge = graphCtx.edge;
    const fromNode = graphCtx.fromNode;
    const toNode = graphCtx.toNode;
    const status = (subject && subject.status) || (graphNode && graphNode.status) || (graphEdge && graphEdge.status) || "unknown";
    const title = graphNode ? flowNodeDisplayLabel(graphNode) : (graphEdge ? "Configured flow" : (subject.kind || subject.type || "trace subject"));
    const detail = graphNode ? flowNodeDetail(graphNode) : (graphEdge
      ? ((graphEdge.projection ? "Current-state projection" : "Configured topology edge") + " from " + (fromNode ? flowNodeDisplayLabel(fromNode) : compactId(graphEdge.from, 34)) + " to " + (toNode ? flowNodeDisplayLabel(toNode) : compactId(graphEdge.to, 34)) + ".")
      : "Compact provenance subject.");
    const statusSource = graphNode && graphNode.status_source;
    const upstreamCount = (data.upstream || []).length;
    const influenceCount = (data.influences || []).length;
    const downstreamCount = (data.downstream || []).length;
    const evidenceCount = (data.evidence_refs || []).length;
    return h(Card, { className: "sx-card sx-detail-rail", style: { borderColor: bandColor(flowStatusBand(status)) } }, h(CardContent, { className: "sx-card-content" },
      h("div", { className: "sx-row sx-between" },
        h("div", { className: "sx-trace-title-block" },
          h("div", { className: "sx-title" }, title),
          h("div", { className: "sx-id" }, subject.id),
        ),
        h(Pill, { band: flowStatusBand(status), strong: true }, status || "unknown"),
      ),
      h("p", { className: "sx-summary sx-trace-detail" }, detail),
      h("div", { className: "sx-trace-fields" },
        h(TraceField, { label: "role", value: graphNode ? ((FLOW_DAG_COLUMN_LABELS[graphNode.kind] || graphNode.kind) + " / " + graphNode.kind) : subject.kind }),
        h(TraceField, { label: "origin", value: graphNode ? (graphNode.origin === "instance" ? "live overlay" : "configured topology") : subject.origin }),
        h(TraceField, { label: "status source", value: statusSource }),
        h(TraceField, { label: "configured", value: graphNode && graphNode.configured_status }),
        h(TraceField, { label: "enabled", value: graphNode && graphNode.enabled !== undefined && graphNode.enabled !== null ? String(graphNode.enabled) : null }),
        h(TraceField, { label: "pressure", value: graphNode && graphNode.pressure !== undefined && graphNode.pressure !== null ? Number(graphNode.pressure).toFixed(3) : null }),
        h(TraceField, { label: "freshness", value: graphNode && graphNode.freshness }),
        h(TraceField, { label: "edge kind", value: graphEdge && graphEdge.kind }),
        h(TraceField, { label: "from", value: graphEdge && (fromNode ? flowNodeDisplayLabel(fromNode) : graphEdge.from) }),
        h(TraceField, { label: "to", value: graphEdge && (toNode ? flowNodeDisplayLabel(toNode) : graphEdge.to) }),
      ),
      h("div", { className: "sx-trace-callout" },
        h("strong", null, "What this means"),
        h("p", null, graphEdge
          ? (graphEdge.projection ? "This dashed edge is only a current-state overlay. It helps locate live work, but it is not claimed as an observed traversal log." : "This solid edge is configured flow. It says what can happen, not proof that this path fired in the current tick.")
          : flowStatusMeaning(status, statusSource)),
      ),
      h("div", { className: "sx-ref-row sx-trace-counts" },
        h(Pill, { band: upstreamCount ? "violet" : undefined }, "upstream " + upstreamCount),
        h(Pill, { band: influenceCount ? "yellow" : undefined }, "influences " + influenceCount),
        h(Pill, { band: downstreamCount ? "green" : undefined }, "downstream " + downstreamCount),
        h(Pill, { band: evidenceCount ? "green" : undefined }, "evidence " + evidenceCount),
      ),
      h(Section, { title: "Where it came from", subtitle: "upstream" }, h(TraceRefList, { items: data.upstream, empty: "No upstream refs derivable yet." })),
      h(Section, { title: "What influenced it", subtitle: "influences" }, h(TraceRefList, { items: data.influences, empty: "No influence refs for this subject." })),
      h(Section, { title: "Where it leads", subtitle: "downstream" }, h(TraceRefList, { items: data.downstream, empty: "No downstream effects recorded yet." })),
      h(Section, { title: "Config refs", subtitle: "sanitized source/kind/version" }, h(TraceRefList, { items: (data.config_refs || []).map(function (c) { return (c.kind || "config") + ": " + compactId(c.ref || "", 34) + (c.version ? " @ " + c.version.slice(0, 18) : ""); }), empty: "Not a configured-topology subject." })),
      h(Section, { title: "Evidence refs" }, h(TraceRefList, { items: (data.evidence_refs || []).map(function (e) { return (e.type || "evidence") + ": " + compactId(e.ref || "", 34); }), empty: "No evidence refs for this subject." })),
      h("div", { className: "sx-meta" },
        Object.keys(data.timestamps || {}).filter(function (k) { return data.timestamps[k]; }).map(function (k) {
          return h("span", { key: k }, k, ": ", timeText(data.timestamps[k]));
        }),
      ),
      limitations.length ? h("p", { className: "sx-muted sx-trace-limitations" }, "limitations: ", limitations.join(" ")) : null,
    ));
  }

  function FlowDagView() {
    const topo = usePluginJson("/api/plugins/agent-sensorium/topology", 60 * 1000);
    const runtime = usePluginJson("/api/plugins/agent-sensorium/runtime-status", 15 * 1000);
    const attention = usePluginJson("/api/plugins/agent-sensorium/attention?surface=discord", 15 * 1000);
    const [selected, setSelected] = useState(null);
    const graph = buildFlowDagGraph(topo.data, runtime.data, attention.data);
    const traceState = useTrace(selected);

    return h(Section, { title: "Flow DAG", subtitle: "Config-derived processing graph plus current open candidates/threads/actions as a live overlay. Select a node or edge for /trace provenance." },
      (topo.error || runtime.error || attention.error) ? h(Card, { className: "sx-card sx-error" }, h(CardContent, null, "Flow DAG fetch failed: ", topo.error || runtime.error || attention.error)) : null,
      h("div", { className: "sx-flow-dag-layout" },
        h(FlowDagCanvas, { graph: graph, selected: selected, onSelect: setSelected }),
        h(TraceDetailRail, { selected: selected, trace: traceState, graph: graph }),
      ),
      h("p", { className: "sx-muted" }, "Solid edges are configured topology. Dashed projection edges attach the currently visible attention inbox/runtime items to the configured organs that surface or prepare them; they are a current-state overlay, not a fabricated traversal log. Runtime-status edges still have no honest traversal source yet."),
    );
  }

  function classifySignalLane(signal) {
    const hay = [signal.sensor, signal.kind, signal.source].join(" ").toLowerCase();
    if (hay.indexOf("temporal") >= 0 || hay.indexOf("trend") >= 0 || hay.indexOf("slope") >= 0 || hay.indexOf("recurrence") >= 0) return "temporal";
    return "sensors";
  }

  function graphBand(node) {
    if (!node) return "neutral";
    if (node.band) return node.band;
    if (node.status === "blocked" || node.kind === "blocker") return "red";
    if (node.status === "candidate" || node.status === "held" || node.kind === "dampener") return "yellow";
    if (node.kind === "receipt" || node.kind === "action" || node.kind === "feedback") return "green";
    if (node.kind === "loop") return "violet";
    return "neutral";
  }

  function makeNodeId(prefix, value) {
    return prefix + ":" + String(value || prefix + "-unknown");
  }

  function addGraphNode(map, node) {
    if (!node || !node.id || map.has(node.id)) return;
    map.set(node.id, Object.assign({ band: graphBand(node), refs: [] }, node));
  }

  function addGraphEdge(edges, seen, edge) {
    if (!edge || !edge.from || !edge.to) return;
    const id = edge.id || (edge.from + "->" + edge.to + ":" + (edge.kind || "edge"));
    if (seen.has(id)) return;
    seen.add(id);
    edges.push(Object.assign({ id: id, kind: "relates" }, edge));
  }

  function passesInnerLifeFilters(node, filters) {
    if (!node) return false;
    if (filters.trendOnly && node.lane !== "temporal") return false;
    if (filters.blockedOnly && !(node.status === "blocked" || node.band === "red" || (node.flags || []).indexOf("blocked") >= 0 || node.kind === "blocker")) return false;
    if (filters.wrongOnly && !(node.band === "red" || node.band === "yellow" || (node.flags || []).length)) return false;
    if (filters.openReview && !(node.lane === "reviews" || node.status === "candidate" || node.status === "accepted" || node.status === "prepared" || node.kind === "receipt")) return false;
    if (!filters.includeLoops && node.lane === "loops") return false;
    return true;
  }

  function buildInnerLifeGraph(data, graphData, filters) {
    const nodes = new Map();
    const edges = [];
    const seenEdges = new Set();
    const traces = (data && data.perception_traces) || [];
    const signals = (data && data.recent_signals) || [];
    const candidates = (data && data.top_candidates) || [];
    const actions = (data && data.actions) || [];
    const decisions = (data && data.decisions) || [];
    const graphNodes = (graphData && graphData.nodes) || [];
    const graphEdges = (graphData && graphData.edges) || [];

    signals.forEach(function (s) {
      const id = makeNodeId("signal", s.id || s.sensor || s.ts);
      addGraphNode(nodes, {
        id: id,
        kind: "signal",
        lane: classifySignalLane(s),
        label: s.kind || s.sensor || "signal",
        summary: s.summary || "Compact sensor signal.",
        status: s.pressure_level || s.transition || "observed",
        refs: (s.correlation_keys || []).slice(0, 4),
        raw: s,
      });
    });

    candidates.forEach(function (c) {
      addGraphNode(nodes, {
        id: makeNodeId("candidate", c.id),
        kind: "candidate",
        lane: "candidates",
        label: c.kind || "candidate",
        summary: c.summary || "Candidate pressure.",
        status: c.status || "candidate",
        pressure: c.pressure,
        refs: (c.allowed_surfaces || []).slice(0, 4),
        raw: c,
      });
    });

    traces.forEach(function (t) {
      const candNode = makeNodeId("candidate", t.candidate_id);
      addGraphNode(nodes, {
        id: candNode,
        kind: "candidate",
        lane: "candidates",
        label: t.kind || "candidate trace",
        summary: t.summary || "Perception trace candidate.",
        status: t.status || "trace",
        pressure: t.pressure,
        band: t.band || "neutral",
        flags: t.flags || [],
        refs: (t.correlation_keys || []).slice(0, 4),
        raw: t,
      });
      (t.signals || []).forEach(function (s) {
        const sid = makeNodeId("signal", s.id || s.sensor);
        addGraphNode(nodes, {
          id: sid,
          kind: "signal",
          lane: classifySignalLane(s),
          label: s.kind || s.sensor || "signal",
          summary: s.summary || "Trace source signal.",
          status: s.pressure_level || s.transition || "observed",
          raw: s,
        });
        addGraphEdge(edges, seenEdges, { from: sid, to: candNode, kind: "promotes_to", label: "signal → candidate" });
      });
      (t.events || []).forEach(function (e) {
        const eid = makeNodeId("event", e.id);
        addGraphNode(nodes, {
          id: eid,
          kind: "event",
          lane: "sensors",
          label: e.kind || "event",
          summary: e.summary || "Trace event.",
          status: "event",
          raw: e,
        });
        (e.signal_ids || []).forEach(function (sid) {
          addGraphEdge(edges, seenEdges, { from: makeNodeId("signal", sid), to: eid, kind: "emits", label: "signal → event" });
        });
        addGraphEdge(edges, seenEdges, { from: eid, to: candNode, kind: "promotes_to", label: "event → candidate" });
      });
      if (t.settlement) {
        const receiptNode = makeNodeId("settlement", (t.settlement.decision || "settled") + ":" + t.candidate_id);
        addGraphNode(nodes, {
          id: receiptNode,
          kind: "receipt",
          lane: "receipts",
          label: t.settlement.decision || "settlement",
          summary: t.settlement.reason || "Settlement receipt.",
          status: t.settlement.unresolved ? "unresolved" : "settled",
          band: t.settlement.unresolved ? "red" : "green",
          refs: [t.settlement.intake_task_id, t.settlement.review_task_id, t.settlement.conscious_task_id].filter(Boolean),
          raw: t.settlement,
        });
        addGraphEdge(edges, seenEdges, { from: candNode, to: receiptNode, kind: "settles", label: "candidate → receipt" });
      }
    });

    graphNodes.forEach(function (n) {
      addGraphNode(nodes, {
        id: n.id,
        kind: n.kind || "graph_node",
        lane: n.kind === "receipt" ? "receipts" : "candidates",
        label: n.kind === "receipt" ? (n.decision || n.receipt_type || "receipt") : (n.candidate_kind || "candidate"),
        summary: n.kind === "receipt" ? "Normalized receipt projection." : "Receipt-linked candidate projection.",
        status: n.outcome || n.status || n.receipt_kind || "projected",
        refs: (n.evidence_refs || []).map(function (r) { return r.type + ":" + r.ref; }).slice(0, 5),
        raw: n,
      });
    });
    graphEdges.forEach(function (e) {
      addGraphEdge(edges, seenEdges, { id: e.id, from: e.from, to: e.to, kind: e.kind || "settles", label: e.kind || "edge", raw: e });
    });

    actions.forEach(function (a) {
      const id = makeNodeId("action", a.id);
      addGraphNode(nodes, {
        id: id,
        kind: "action",
        lane: "reviews",
        label: a.title || a.intent || "thread action",
        summary: a.result_summary || a.summary || "Action/review node.",
        status: a.status || a.outcome || "action",
        refs: [a.origin_thread_id, a.origin_candidate_id].filter(Boolean),
        raw: a,
      });
      if (a.origin_candidate_id) addGraphEdge(edges, seenEdges, { from: makeNodeId("candidate", a.origin_candidate_id), to: id, kind: "opens_review", label: "candidate → action" });
    });

    decisions.filter(function (d) { return String(d.type || "").indexOf("feedback") >= 0; }).slice(0, 12).forEach(function (d, i) {
      const id = makeNodeId("feedback", d.feedback_signal_id || d.action_id || d.thread_id || i);
      addGraphNode(nodes, {
        id: id,
        kind: "feedback",
        lane: "feedback",
        label: d.type || "feedback",
        summary: d.reason || d.outcome || "Feedback receipt.",
        status: d.outcome || "feedback",
        refs: [d.thread_id, d.action_id, d.worker_request_id].filter(Boolean),
        raw: d,
      });
    });

    if (filters.includeLoops) {
      const derived = get(data || {}, "metrics.latest.derived", {});
      addGraphNode(nodes, { id: "loop:cooldown", kind: "loop", lane: "loops", label: "Loop policy", status: "bounded", summary: "Cooldown / idempotency / max-pass policies stay below autonomous mutation.", refs: ["read-only dashboard", "no outbound side effects"], raw: derived });
    }

    const allNodes = Array.from(nodes.values()).filter(function (n) { return passesInnerLifeFilters(n, filters); }).slice(0, 90);
    const visible = new Set(allNodes.map(function (n) { return n.id; }));
    return {
      nodes: allNodes,
      edges: edges.filter(function (e) { return visible.has(e.from) && visible.has(e.to); }).slice(0, 140),
      lanes: ["sensors", "temporal", "candidates", "reviews", "receipts", "feedback", "loops"],
    };
  }

  function FilterToggle(props) {
    return h("label", { className: "sx-filter-toggle" },
      h("input", { type: "checkbox", checked: !!props.checked, onChange: function (ev) { props.onChange(ev.target.checked); } }),
      h("span", null, props.label),
    );
  }

  function InnerLifeFilters(props) {
    const f = props.filters;
    return h(Card, { className: "sx-card sx-filter-card" }, h(CardContent, { className: "sx-card-content" },
      h("div", { className: "sx-row sx-between" },
        h("div", null, h("div", { className: "sx-title" }, "Inner-life filters"), h("div", { className: "sx-muted" }, "Client-side visibility filters; no dashboard mutation route is called.")),
        h(Pill, { band: "green" }, "GET only"),
      ),
      h("div", { className: "sx-filter-row" },
        h(FilterToggle, { label: "wrong turns", checked: f.wrongOnly, onChange: function (v) { props.onChange(Object.assign({}, f, { wrongOnly: v })); } }),
        h(FilterToggle, { label: "trend only", checked: f.trendOnly, onChange: function (v) { props.onChange(Object.assign({}, f, { trendOnly: v })); } }),
        h(FilterToggle, { label: "blocked only", checked: f.blockedOnly, onChange: function (v) { props.onChange(Object.assign({}, f, { blockedOnly: v })); } }),
        h(FilterToggle, { label: "open review", checked: f.openReview, onChange: function (v) { props.onChange(Object.assign({}, f, { openReview: v })); } }),
        h(FilterToggle, { label: "include loops", checked: f.includeLoops, onChange: function (v) { props.onChange(Object.assign({}, f, { includeLoops: v })); } }),
      ),
    ));
  }

  function LaneNodeCard(props) {
    const node = props.node;
    const active = props.selected && props.selected.id === node.id;
    return h("button", {
      type: "button",
      className: "sx-lane-node" + (active ? " sx-lane-node-active" : ""),
      style: { borderColor: bandColor(node.band || graphBand(node)) },
      onClick: function () { props.onSelect(node); },
    },
      h("span", { className: "sx-lane-node-kind" }, node.kind || node.lane),
      h("span", { className: "sx-lane-node-label" }, node.label || compactId(node.id)),
      h("span", { className: "sx-id" }, compactId(node.id)),
      node.status ? h("span", { className: "sx-muted" }, node.status) : null,
    );
  }

  function EdgeRow(props) {
    const e = props.edge;
    const active = props.selected && props.selected.id === e.id;
    return h("button", {
      type: "button",
      className: "sx-edge-row" + (active ? " sx-edge-row-active" : ""),
      onClick: function () { props.onSelect(Object.assign({ lane: "edge", summary: e.label || e.kind }, e)); },
    },
      h("span", null, e.kind || "edge"),
      h("span", { className: "sx-id" }, compactId(e.from), " → ", compactId(e.to)),
    );
  }

  function DetailRail(props) {
    const item = props.item;
    if (!item) {
      return h(Card, { className: "sx-card sx-detail-rail" }, h(CardContent, { className: "sx-card-content" },
        h("div", { className: "sx-title" }, "Selection detail"),
        h("p", { className: "sx-summary" }, "Select a node or edge to inspect compact refs, status, and projection evidence."),
      ));
    }
    const refs = item.refs || [];
    return h(Card, { className: "sx-card sx-detail-rail", style: { borderColor: bandColor(item.band || graphBand(item)) } }, h(CardContent, { className: "sx-card-content" },
      h("div", { className: "sx-row sx-between" },
        h("div", null, h("div", { className: "sx-title" }, item.label || item.kind || "selected"), h("div", { className: "sx-id" }, item.id || "edge")),
        h(Pill, { band: item.band || graphBand(item) }, item.lane || item.kind || "detail"),
      ),
      h("p", { className: "sx-summary" }, item.summary || "Compact projection only."),
      h("div", { className: "sx-meta" },
        item.status ? h("span", null, "status: ", item.status) : null,
        item.kind ? h("span", null, "kind: ", item.kind) : null,
        item.from ? h("span", null, "from: ", compactId(item.from)) : null,
        item.to ? h("span", null, "to: ", compactId(item.to)) : null,
      ),
      refs.length ? h("div", { className: "sx-ref-row" }, refs.slice(0, 8).map(function (r, i) { return h(Pill, { key: String(i) + r }, r); })) : h("p", { className: "sx-muted" }, "No compact refs exposed for this item."),
    ));
  }

  function LaneGraphPanel(props) {
    const graph = props.graph;
    const selected = props.selected;
    const lanes = graph.lanes || [];
    const byLane = {};
    (graph.nodes || []).forEach(function (n) {
      const lane = n.lane || "other";
      byLane[lane] = byLane[lane] || [];
      byLane[lane].push(n);
    });
    return h("div", { className: "sx-inner-life-layout" },
      h("div", { className: "sx-lane-graph" },
        lanes.map(function (lane) {
          const laneNodes = byLane[lane] || [];
          if (!laneNodes.length) return null;
          return h("section", { key: lane, className: "sx-lane-column" },
            h("div", { className: "sx-lane-head" }, h("span", null, lane.replace("_", " ")), h(Pill, null, laneNodes.length)),
            laneNodes.map(function (n) { return h(LaneNodeCard, { key: n.id, node: n, selected: selected, onSelect: props.onSelect }); }),
          );
        }),
        (graph.nodes || []).length ? null : h(Empty, { text: "No inner-life graph nodes match the current filters." }),
        h("section", { className: "sx-lane-column sx-edge-column" },
          h("div", { className: "sx-lane-head" }, h("span", null, "edges"), h(Pill, null, (graph.edges || []).length)),
          (graph.edges || []).slice(0, 48).map(function (e) { return h(EdgeRow, { key: e.id, edge: e, selected: selected, onSelect: props.onSelect }); }),
        ),
      ),
      h(DetailRail, { item: selected }),
    );
  }

  function WarningCard(props) {
    const w = props.warning;
    return h(Card, { className: "sx-card sx-warning-card", style: { borderColor: bandColor(w.band) } }, h(CardContent, { className: "sx-card-content" },
      h("div", { className: "sx-row sx-between" },
        h("div", null, h("div", { className: "sx-title" }, w.label || w.kind || "warning"), h("div", { className: "sx-id" }, w.id || "—")),
        h(Pill, { band: w.band }, w.kind || "lifecycle"),
      ),
      h("p", { className: "sx-summary" }, w.detail || "No detail."),
    ));
  }

  function DecisionRow(props) {
    const d = props.decision;
    return h("div", { className: "sx-decision-row" },
      h("span", { className: "sx-decision-type" }, d.type || "decision"),
      h("span", { className: "sx-decision-time" }, timeText(d.ts)),
      h("span", { className: "sx-decision-ref" }, d.thread_id || d.candidate_id || d.action_id || d.artifact_id || d.outbox_id || "—"),
      d.reason ? h("span", { className: "sx-muted" }, d.reason) : null,
    );
  }

  function StatusBreakdown(props) {
    const sb = props.breakdown || {};
    return h(Card, { className: "sx-card" }, h(CardContent, { className: "sx-card-content" },
      Object.keys(sb).sort().map(function (key) {
        return h("div", { key: key, className: "sx-breakdown-row" }, h("span", null, key), h("span", null, countsText(sb[key])));
      }),
    ));
  }

  function SensoriumPage() {
    const state = useSnapshot(60 * 1000);
    const graphState = usePluginJson("/api/plugins/agent-sensorium/graph", 60 * 1000);
    const topoNavState = usePluginJson("/api/plugins/agent-sensorium/topology", 60 * 1000);
    const data = state.data;
    const counts = data && data.counts ? data.counts : {};
    const footprint = data && data.attention_footprint ? data.attention_footprint : {};
    const health = data && data.health ? data.health : {};
    const metrics = data && data.metrics ? data.metrics : {};
    const latest = metrics.latest || {};
    const latestCounts = latest.counts || {};
    const recent = latest.recent_24h || {};
    const open = latest.open || {};
    const derived = latest.derived || {};
    const series = metrics.series || [];
    const [activeView, setActiveView] = useState("flow_dag");
    const [innerFilters, setInnerFilters] = useState({ wrongOnly: false, trendOnly: false, blockedOnly: false, openReview: false, includeLoops: false });
    const [selectedInnerItem, setSelectedInnerItem] = useState(null);
    const openConscious = open.open_conscious_review_tasks ?? 0;
    const openSubconscious = open.open_subconscious_review_tasks ?? 0;
    const openIntake = open.open_intake_tasks ?? 0;
    const duplicateHints = recent.duplicate_or_no_candidate_match_hints ?? 0;
    const warnings = data && data.lifecycle_warnings ? data.lifecycle_warnings : [];
    const topCandidates = data && data.top_candidates ? data.top_candidates : [];
    const recentSignals = data && data.recent_signals ? data.recent_signals : [];
    const perceptionTraces = data && data.perception_traces ? data.perception_traces : [];
    const wrongTurns = counts.perception_wrong_turns || 0;
    const flowDagNavCount = (topoNavState.data && topoNavState.data.meta && topoNavState.data.meta.node_count) || 0;
    const viewCards = [{ id: "flow_dag", label: "Flow DAG", summary: "Configured topology + runtime status, with /trace provenance on selection.", count: flowDagNavCount, band: "violet" }]
      .concat((data && data.views) ? data.views : [])
      .concat([{ id: "inner_life", label: "Inner-life (debug)", summary: "Older /snapshot-shaped lane graph; kept as fallback/debug, not primary.", count: ((graphState.data && graphState.data.meta && graphState.data.meta.node_count) || 0) + (perceptionTraces.length || 0), band: wrongTurns ? "yellow" : "neutral" }]);
    const innerGraph = buildInnerLifeGraph(data, graphState.data, innerFilters);
    const liveItems = footprint.live_items ?? ((counts.lifecycle_warnings || 0) + (counts.active_candidates || 0) + openConscious + openSubconscious + openIntake);
    const residueItems = footprint.residue_items ?? ((counts.held_artifacts || 0) + (counts.historical_outbox || 0) + (counts.closed_actions || 0));
    const quiet = liveItems === 0;

    return h("div", { className: "sx-page" },
      h("div", { className: "sx-hero" },
        h("div", null,
          h("p", { className: "sx-kicker" }, "Agent Sensorium"),
          h("h1", null, "Signal economy"),
          h("p", null, "High-level attention first: footprint, escalation, durability, and only then drill-down into candidates, actions, artifacts, outbox, and receipts."),
          h("div", { className: "sx-hero-pills" },
            h(Pill, { band: quiet ? "green" : "yellow", strong: true }, quiet ? "live quiet" : "live " + plural(liveItems, "item") + (liveItems === 1 ? " needs attention" : " need attention")),
            h(Pill, { band: residueItems ? "neutral" : "green" }, "residue " + residueItems),
            h(Pill, { band: "violet" }, "metrics samples " + (metrics.series_count || 0)),
            h(Pill, null, "fresh " + timeText(data && data.state_mtime)),
            wrongTurns > 0 ? h(Pill, { band: "yellow" }, plural(wrongTurns, "wrong turn")) : null,
          ),
        ),
        h("div", { className: "sx-actions" },
          health.status ? h(Badge, { variant: "outline", className: "sx-status", style: { borderColor: bandColor(health.band), color: bandColor(health.band) } }, "health: " + health.status) : null,
          h(Button, { variant: "outline", size: "sm", onClick: state.reload, disabled: state.loading }, state.loading ? "Refreshing…" : "Refresh"),
        ),
      ),
      state.error ? h(Card, { className: "sx-card sx-error" }, h(CardContent, null, state.error)) : null,
      !data ? h(Empty, { text: state.loading ? "Loading Sensorium snapshot…" : "No Sensorium snapshot yet." }) : h(React.Fragment, null,
        h("div", { className: "sx-metrics sx-priority-metrics" },
          h(MetricCard, { label: "Live pressure", value: liveItems, hint: "now: candidates + reviews + actions", hot: liveItems > 5 }),
          h(MetricCard, { label: "Historical residue", value: residueItems, hint: "held artifacts + historical pointers", hot: false }),
          h(MetricCard, { label: "Conscious / signal", value: fmtRatio(derived.conscious_reviews_per_signal_24h), hint: "24h escalation pressure", hot: (derived.conscious_reviews_per_signal_24h || 0) > 0.25 }),
          h(MetricCard, { label: "Durable / review", value: fmtRatio(derived.durable_decisions_per_conscious_review_24h), hint: "24h impact density" }),
          h(MetricCard, { label: "Duplicate hints", value: duplicateHints, hint: "24h duplicate/no-match smell", hot: duplicateHints > 0 }),
          h(MetricCard, { label: "Open conscious", value: openConscious, hint: "reviews waiting now", hot: openConscious > 0 }),
          h(MetricCard, { label: "Active candidates", value: counts.active_candidates ?? latestCounts.active_candidates ?? 0, hint: "attention inbox" }),
        ),
        h(ViewNav, { views: viewCards, active: activeView, onChange: setActiveView }),
        activeView === "flow_dag" ? h(FlowDagView, null) : null,
        activeView === "overview" ? h(React.Fragment, null,
          h("div", { className: "sx-focus-grid" },
            h(Section, { title: "What matters now", subtitle: "Live pressure only. Residue is counted separately so old artifacts do not look like work." },
              warnings.length ? warnings.slice(0, 4).map(function (w, i) { return h(WarningCard, { key: String(i) + (w.id || ""), warning: w }); }) : null,
              topCandidates.length ? topCandidates.slice(0, 3).map(function (c) { return h(CandidateCard, { key: c.id, candidate: c }); }) : null,
              !warnings.length && !topCandidates.length && !openConscious && !openSubconscious && !openIntake ? h(Empty, { text: "No immediate attention pressure." }) : null,
              h("div", { className: "sx-ref-row sx-open-pills" },
                h(Pill, { band: openIntake ? "yellow" : "green" }, "intake open " + openIntake),
                h(Pill, { band: openSubconscious ? "yellow" : "green" }, "subconscious open " + openSubconscious),
                h(Pill, { band: openConscious ? "yellow" : "green" }, "conscious open " + openConscious),
              ),
            ),
            h(Section, { title: "Efficiency trend", subtitle: "Whether awareness is getting cheaper, rarer, and more consequential." },
              h("div", { className: "sx-trend-grid" },
                h(MiniTrend, { title: "Open footprint", subtitle: "lower is better", series: series, path: "derived.footprint_open_items", lowerIsBetter: true, color: "#22c55e" }),
                h(MiniTrend, { title: "Conscious / signal", subtitle: "lower if quality holds", series: series, path: "derived.conscious_reviews_per_signal_24h", lowerIsBetter: true, color: "#828fff" }),
                h(MiniTrend, { title: "Durable / review", subtitle: "higher is better", series: series, path: "derived.durable_decisions_per_conscious_review_24h", lowerIsBetter: false, color: "#eab308" }),
              ),
            ),
          ),
        ) : null,
        activeView === "perception" ? h(React.Fragment, null,
          h(Section, { title: "Perception trace", subtitle: "Recent candidate lifecycle: what was noticed, what Subconscious decided, and where it landed." },
            perceptionTraces.length ? h("div", { className: "sx-trace-grid" },
              perceptionTraces.slice().sort(function (a, b) {
                const aBad = a.band === "red" ? 0 : a.band === "yellow" ? 1 : 2;
                const bBad = b.band === "red" ? 0 : b.band === "yellow" ? 1 : 2;
                return aBad - bBad;
              }).map(function (t) { return h(PerceptionTraceCard, { key: t.candidate_id || String(Math.random()), trace: t }); }),
            ) : h(Empty, { text: "No perception traces yet." }),
          ),
          h(Section, { title: "Recent salience residue", subtitle: "Compact live-session signals retained for later review; not duplicate foreground work." },
            h("div", { className: "sx-grid" }, recentSignals.length ? recentSignals.map(function (s, i) { return h(SignalCard, { key: (s.id || String(i)) + (s.ts || ""), signal: s }); }) : h(Empty, { text: "No recent signals." })),
          ),
        ) : null,
        activeView === "substrate" ? h("div", { className: "sx-drill-stack" },
          h(DrillDown, { title: "Lifecycle warnings", badge: String(counts.lifecycle_warnings ?? 0), band: counts.lifecycle_warnings ? "yellow" : "green", open: true },
            warnings.length ? warnings.map(function (w, i) { return h(WarningCard, { key: String(i) + (w.id || ""), warning: w }); }) : h(Empty, { text: "No lifecycle gaps detected." }),
          ),
          h(DrillDown, { title: "Candidates and threads", badge: (counts.active_candidates ?? 0) + " / " + (counts.active_threads ?? 0), open: true },
            h("div", { className: "sx-grid" },
              h(Section, { title: "Top candidates", subtitle: "Pressure sorted." }, topCandidates.length ? topCandidates.map(function (c) { return h(CandidateCard, { key: c.id, candidate: c }); }) : h(Empty, { text: "No active candidates." })),
              h(Section, { title: "Visible threads", subtitle: "Dormant / held continuity units." }, data.threads && data.threads.length ? data.threads.map(function (t) { return h(ThreadCard, { key: t.id, thread: t }); }) : h(Empty, { text: "No dormant or held threads visible." })),
            ),
          ),
          h(DrillDown, { title: "Receipts and breakdowns", badge: String(counts.decisions ?? 0), open: true },
            h("div", { className: "sx-grid" },
              h(Section, { title: "Recent receipts", subtitle: "Decision log tail." }, data.decisions && data.decisions.length ? h(Card, { className: "sx-card" }, h(CardContent, { className: "sx-decision-list" }, data.decisions.map(function (d, i) { return h(DecisionRow, { key: String(i) + (d.ts || ""), decision: d }); }))) : h(Empty, { text: "No decisions yet." })),
              h(Section, { title: "Status breakdown", subtitle: "Counts by lifecycle state." }, h(StatusBreakdown, { breakdown: data.status_breakdown })),
            ),
          ),
        ) : null,
        activeView === "inner_life" ? h(React.Fragment, null,
          h(Section, { title: "Inner-life lane graph", subtitle: "Read-only projection of sensors, temporal movement, candidates, reviews, receipts, feedback, and compact refs." },
            h(InnerLifeFilters, { filters: innerFilters, onChange: setInnerFilters }),
            graphState.error ? h(Card, { className: "sx-card sx-error" }, h(CardContent, null, "Graph fetch failed: " + graphState.error)) : null,
            h("div", { className: "sx-ref-row" },
              h(Pill, { band: "green" }, "nodes " + innerGraph.nodes.length),
              h(Pill, { band: "green" }, "edges " + innerGraph.edges.length),
              h(Pill, null, "receipt graph " + (((graphState.data || {}).meta || {}).privacy || "compact")),
              h(Pill, null, graphState.loading ? "graph refreshing" : "graph fresh"),
            ),
            h(LaneGraphPanel, { graph: innerGraph, selected: selectedInnerItem, onSelect: setSelectedInnerItem }),
          ),
        ) : null,
        activeView === "actuators" ? h(DrillDown, { title: "Actions, outbox, artifacts, and residue", badge: (counts.open_actions ?? 0) + " open actions · " + residueItems + " residue", open: true },
          h("div", { className: "sx-grid" },
            h(Section, { title: "Thread actions", subtitle: "Open motor plans first; completed actions are residue/evidence." }, data.actions && data.actions.length ? data.actions.map(function (a) { return h(ActionCard, { key: a.id, action: a }); }) : h(Empty, { text: "No thread actions." })),
            h(Section, { title: "Outbox", subtitle: "Actionable requests vs historical pointers with safety labels." }, data.outbox && data.outbox.length ? data.outbox.map(function (o) { return h(OutboxCard, { key: o.id, request: o }); }) : h(Empty, { text: "No outbox requests." })),
            h(Section, { title: "Artifact groups", subtitle: "Historical residue and held/private artifacts; raw bodies stay hidden." }, data.artifact_groups && data.artifact_groups.length ? data.artifact_groups.map(function (g) { return h(ArtifactGroupCard, { key: g.id, group: g }); }) : h(Empty, { text: "No artifacts recorded." })),
          ),
        ) : null,
        h("div", { className: "sx-footer" },
          h("span", null, "state: ", data.state_dir),
          h("span", null, "metrics: ", metrics.timeseries_path || "—"),
          h("span", null, "generated: ", timeText(data.generated_at)),
          h("span", null, "metrics fresh: ", timeText(metrics.latest_mtime)),
          h("span", null, "graph: ", graphState.error ? "error" : (((graphState.data || {}).meta || {}).privacy || "pending")),
        ),
      ),
    );
  }

  PLUGINS.register("agent-sensorium", SensoriumPage);
})();
