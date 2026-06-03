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
    const data = state.data;
    const counts = data && data.counts ? data.counts : {};
    const health = data && data.health ? data.health : {};
    const metrics = data && data.metrics ? data.metrics : {};
    const latest = metrics.latest || {};
    const latestCounts = latest.counts || {};
    const recent = latest.recent_24h || {};
    const open = latest.open || {};
    const derived = latest.derived || {};
    const series = metrics.series || [];
    const openConscious = open.open_conscious_review_tasks ?? 0;
    const openSubconscious = open.open_subconscious_review_tasks ?? 0;
    const openIntake = open.open_intake_tasks ?? 0;
    const duplicateHints = recent.duplicate_or_no_candidate_match_hints ?? 0;
    const warnings = data && data.lifecycle_warnings ? data.lifecycle_warnings : [];
    const topCandidates = data && data.top_candidates ? data.top_candidates : [];
    const recentSignals = data && data.recent_signals ? data.recent_signals : [];
    const importantCount = (counts.lifecycle_warnings || 0) + (counts.active_candidates || 0) + openConscious + openSubconscious + openIntake;
    const quiet = importantCount === 0;

    return h("div", { className: "sx-page" },
      h("div", { className: "sx-hero" },
        h("div", null,
          h("p", { className: "sx-kicker" }, "Agent Sensorium"),
          h("h1", null, "Signal economy"),
          h("p", null, "High-level attention first: footprint, escalation, durability, and only then drill-down into candidates, actions, artifacts, outbox, and receipts."),
          h("div", { className: "sx-hero-pills" },
            h(Pill, { band: quiet ? "green" : "yellow", strong: true }, quiet ? "quiet" : plural(importantCount, "item") + (importantCount === 1 ? " needs attention" : " need attention")),
            h(Pill, { band: "violet" }, "metrics samples " + (metrics.series_count || 0)),
            h(Pill, null, "fresh " + timeText(data && data.state_mtime)),
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
          h(MetricCard, { label: "Open footprint", value: derived.footprint_open_items ?? "—", hint: "active candidates + open substrate", hot: (derived.footprint_open_items || 0) > 5 }),
          h(MetricCard, { label: "Conscious / signal", value: fmtRatio(derived.conscious_reviews_per_signal_24h), hint: "24h escalation pressure", hot: (derived.conscious_reviews_per_signal_24h || 0) > 0.25 }),
          h(MetricCard, { label: "Durable / review", value: fmtRatio(derived.durable_decisions_per_conscious_review_24h), hint: "24h impact density" }),
          h(MetricCard, { label: "Duplicate hints", value: duplicateHints, hint: "24h duplicate/no-match smell", hot: duplicateHints > 0 }),
          h(MetricCard, { label: "Open conscious", value: openConscious, hint: "reviews waiting now", hot: openConscious > 0 }),
          h(MetricCard, { label: "Active candidates", value: counts.active_candidates ?? latestCounts.active_candidates ?? 0, hint: "attention inbox" }),
        ),
        h("div", { className: "sx-focus-grid" },
          h(Section, { title: "What matters now", subtitle: "Only open pressure and actionable gaps; detailed substrate is collapsed below." },
            warnings.length ? warnings.slice(0, 4).map(function (w, i) { return h(WarningCard, { key: String(i) + (w.id || ""), warning: w }); }) : null,
            topCandidates.length ? topCandidates.slice(0, 3).map(function (c) { return h(CandidateCard, { key: c.id, candidate: c }); }) : null,
            !warnings.length && !topCandidates.length && !openConscious && !openSubconscious && !openIntake ? h(Empty, { text: "No immediate attention pressure." }) : null,
            h("div", { className: "sx-ref-row sx-open-pills" },
              h(Pill, { band: openIntake ? "yellow" : "green" }, "intake open " + openIntake),
              h(Pill, { band: openSubconscious ? "yellow" : "green" }, "subconscious open " + openSubconscious),
              h(Pill, { band: openConscious ? "yellow" : "green" }, "conscious open " + openConscious),
            ),
          ),
          h(Section, { title: "Efficiency trend", subtitle: "The dashboard now watches whether awareness gets cheaper, rarer, and more consequential." },
            h("div", { className: "sx-trend-grid" },
              h(MiniTrend, { title: "Open footprint", subtitle: "lower is better", series: series, path: "derived.footprint_open_items", lowerIsBetter: true, color: "#22c55e" }),
              h(MiniTrend, { title: "Conscious / signal", subtitle: "lower if quality holds", series: series, path: "derived.conscious_reviews_per_signal_24h", lowerIsBetter: true, color: "#828fff" }),
              h(MiniTrend, { title: "Durable / review", subtitle: "higher is better", series: series, path: "derived.durable_decisions_per_conscious_review_24h", lowerIsBetter: false, color: "#eab308" }),
            ),
          ),
        ),
        h("div", { className: "sx-drill-stack" },
          h(DrillDown, { title: "Lifecycle warnings", badge: String(counts.lifecycle_warnings ?? 0), band: counts.lifecycle_warnings ? "yellow" : "green" },
            warnings.length ? warnings.map(function (w, i) { return h(WarningCard, { key: String(i) + (w.id || ""), warning: w }); }) : h(Empty, { text: "No lifecycle gaps detected." }),
          ),
          h(DrillDown, { title: "Candidates and threads", badge: (counts.active_candidates ?? 0) + " / " + (counts.active_threads ?? 0) },
            h("div", { className: "sx-grid" },
              h(Section, { title: "Top candidates", subtitle: "Pressure sorted." }, topCandidates.length ? topCandidates.map(function (c) { return h(CandidateCard, { key: c.id, candidate: c }); }) : h(Empty, { text: "No active candidates." })),
              h(Section, { title: "Visible threads", subtitle: "Dormant / held continuity units." }, data.threads && data.threads.length ? data.threads.map(function (t) { return h(ThreadCard, { key: t.id, thread: t }); }) : h(Empty, { text: "No dormant or held threads visible." })),
            ),
          ),
          h(DrillDown, { title: "Recent signals", badge: String(recentSignals.length) },
            h("div", { className: "sx-grid" }, recentSignals.length ? recentSignals.map(function (s, i) { return h(SignalCard, { key: (s.id || String(i)) + (s.ts || ""), signal: s }); }) : h(Empty, { text: "No recent signals." })),
          ),
          h(DrillDown, { title: "Actions, outbox, and artifacts", badge: (counts.open_actions ?? 0) + " open actions" },
            h("div", { className: "sx-grid" },
              h(Section, { title: "Thread actions", subtitle: "Prepared/acted motor plans plus attached refs." }, data.actions && data.actions.length ? data.actions.map(function (a) { return h(ActionCard, { key: a.id, action: a }); }) : h(Empty, { text: "No thread actions." })),
              h(Section, { title: "Outbox", subtitle: "Pointers and delivery requests with safety labels." }, data.outbox && data.outbox.length ? data.outbox.map(function (o) { return h(OutboxCard, { key: o.id, request: o }); }) : h(Empty, { text: "No outbox requests." })),
              h(Section, { title: "Artifact groups", subtitle: "Grouped; raw private content stays hidden." }, data.artifact_groups && data.artifact_groups.length ? data.artifact_groups.map(function (g) { return h(ArtifactGroupCard, { key: g.id, group: g }); }) : h(Empty, { text: "No artifacts recorded." })),
            ),
          ),
          h(DrillDown, { title: "Receipts and breakdowns", badge: String(counts.decisions ?? 0) },
            h("div", { className: "sx-grid" },
              h(Section, { title: "Recent receipts", subtitle: "Decision log tail." }, data.decisions && data.decisions.length ? h(Card, { className: "sx-card" }, h(CardContent, { className: "sx-decision-list" }, data.decisions.map(function (d, i) { return h(DecisionRow, { key: String(i) + (d.ts || ""), decision: d }); }))) : h(Empty, { text: "No decisions yet." })),
              h(Section, { title: "Status breakdown", subtitle: "Counts by lifecycle state." }, h(StatusBreakdown, { breakdown: data.status_breakdown })),
            ),
          ),
        ),
        h("div", { className: "sx-footer" },
          h("span", null, "state: ", data.state_dir),
          h("span", null, "metrics: ", metrics.timeseries_path || "—"),
          h("span", null, "generated: ", timeText(data.generated_at)),
          h("span", null, "metrics fresh: ", timeText(metrics.latest_mtime)),
        ),
      ),
    );
  }

  PLUGINS.register("agent-sensorium", SensoriumPage);
})();
