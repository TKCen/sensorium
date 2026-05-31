(function () {
  "use strict";

  const SDK = window.__HERMES_PLUGIN_SDK__;
  const PLUGINS = window.__HERMES_PLUGINS__;
  if (!SDK || !PLUGINS) return;

  const { React } = SDK;
  const { useEffect, useState } = SDK.hooks;
  const { Card, CardContent, Badge, Button } = SDK.components;

  function bandColor(band) {
    if (band === "green") return "#22c55e";
    if (band === "yellow") return "#eab308";
    if (band === "red") return "#ef4444";
    return "#94a3b8";
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

  function MetricCard(props) {
    return React.createElement(Card, { className: "sx-card sx-metric-card" },
      React.createElement(CardContent, { className: "sx-metric-content" },
        React.createElement("div", { className: "sx-metric-label" }, props.label),
        React.createElement("div", { className: "sx-metric-value" }, String(props.value ?? "—")),
        props.hint ? React.createElement("div", { className: "sx-muted" }, props.hint) : null,
      ),
    );
  }

  function Section(props) {
    return React.createElement("section", { className: "sx-section" },
      React.createElement("div", { className: "sx-section-head" },
        React.createElement("h3", null, props.title),
        props.subtitle ? React.createElement("p", null, props.subtitle) : null,
      ),
      props.children,
    );
  }

  function Empty(props) {
    return React.createElement(Card, { className: "sx-card" },
      React.createElement(CardContent, { className: "sx-empty" }, props.text || "Nothing visible."),
    );
  }

  function Pill(props) {
    return React.createElement("span", {
      className: "sx-pill",
      style: props.band ? { borderColor: bandColor(props.band), color: bandColor(props.band) } : undefined,
    }, props.children);
  }

  function ThreadCard(props) {
    const t = props.thread;
    return React.createElement(Card, { className: "sx-card sx-thread-card" },
      React.createElement(CardContent, { className: "sx-card-content" },
        React.createElement("div", { className: "sx-row sx-between" },
          React.createElement("div", null,
            React.createElement("div", { className: "sx-title" }, t.title || t.id),
            React.createElement("div", { className: "sx-id" }, t.id),
          ),
          React.createElement(Badge, { variant: "outline", className: "sx-badge" }, t.status),
        ),
        React.createElement("div", { className: "sx-meta" },
          React.createElement("span", null, "origin: ", t.origin_candidate_id || "—"),
          React.createElement("span", null, "surfaces: ", listText(t.allowed_surfaces || [])),
          React.createElement("span", null, "expires: ", timeText(t.expires_at)),
          t.dirty ? React.createElement("span", { className: "sx-warn" }, "dirty") : null,
          t.pinned ? React.createElement("span", null, "pinned") : null,
        ),
      ),
    );
  }

  function CandidateCard(props) {
    const c = props.candidate;
    const pressure = c.pressure === null || c.pressure === undefined ? "—" : Number(c.pressure).toFixed(2);
    return React.createElement(Card, { className: "sx-card" },
      React.createElement(CardContent, { className: "sx-card-content" },
        React.createElement("div", { className: "sx-row sx-between" },
          React.createElement("div", null,
            React.createElement("div", { className: "sx-title" }, c.kind || "candidate"),
            React.createElement("div", { className: "sx-id" }, c.id),
          ),
          React.createElement(Badge, { variant: "outline", className: "sx-badge sx-pressure" }, pressure),
        ),
        React.createElement("p", { className: "sx-summary" }, c.summary || "No summary."),
        React.createElement("div", { className: "sx-meta" },
          React.createElement("span", null, "status: ", c.status || "—"),
          React.createElement("span", null, "surfaces: ", listText(c.allowed_surfaces || [])),
        ),
      ),
    );
  }

  function ActionCard(props) {
    const a = props.action;
    return React.createElement(Card, { className: "sx-card sx-action-card" },
      React.createElement(CardContent, { className: "sx-card-content" },
        React.createElement("div", { className: "sx-row sx-between" },
          React.createElement("div", null,
            React.createElement("div", { className: "sx-title" }, a.title || a.intent || "thread action"),
            React.createElement("div", { className: "sx-id" }, a.id),
          ),
          React.createElement(Badge, { variant: "outline", className: "sx-badge" }, (a.status || "unknown") + (a.outcome ? " / " + a.outcome : "")),
        ),
        a.summary ? React.createElement("p", { className: "sx-summary" }, a.summary) : null,
        a.result_summary ? React.createElement("p", { className: "sx-summary sx-result" }, a.result_summary) : null,
        React.createElement("div", { className: "sx-meta" },
          React.createElement("span", null, "thread: ", a.origin_thread_id || "—"),
          React.createElement("span", null, "candidate: ", a.origin_candidate_id || "—"),
          React.createElement("span", null, "updated: ", timeText(a.updated_at)),
        ),
        React.createElement("div", { className: "sx-ref-row" },
          React.createElement(Pill, null, "attachments ", a.attachment_count || 0),
          (a.artifact_refs || []).slice(0, 4).map(function (id) { return React.createElement(Pill, { key: "art" + id }, "artifact ", id); }),
          (a.outbox_refs || []).slice(0, 4).map(function (id) { return React.createElement(Pill, { key: "obx" + id }, "outbox ", id); }),
        ),
      ),
    );
  }

  function ArtifactCard(props) {
    const a = props.artifact;
    return React.createElement(Card, { className: "sx-card" },
      React.createElement(CardContent, { className: "sx-card-content" },
        React.createElement("div", { className: "sx-row sx-between" },
          React.createElement("div", null,
            React.createElement("div", { className: "sx-title" }, (a.kind || "artifact") + (a.ref_name ? ": " + a.ref_name : "")),
            React.createElement("div", { className: "sx-id" }, a.id),
          ),
          React.createElement(Badge, { variant: "outline", className: "sx-badge" }, a.delivery_state || a.status || "recorded"),
        ),
        a.why_created ? React.createElement("p", { className: "sx-summary" }, a.why_created) : null,
        React.createElement("div", { className: "sx-meta" },
          React.createElement("span", null, "thread: ", a.thread_id || "—"),
          React.createElement("span", null, "action: ", a.action_id || "—"),
          React.createElement("span", null, "mode: ", a.handoff_mode || "—"),
          React.createElement("span", null, "updated: ", timeText(a.updated_at)),
        ),
      ),
    );
  }

  function OutboxCard(props) {
    const o = props.request;
    const safety = o.safety || {};
    return React.createElement(Card, { className: "sx-card" },
      React.createElement(CardContent, { className: "sx-card-content" },
        React.createElement("div", { className: "sx-row sx-between" },
          React.createElement("div", null,
            React.createElement("div", { className: "sx-title" }, o.title || o.request_type || "outbox request"),
            React.createElement("div", { className: "sx-id" }, o.id),
          ),
          React.createElement(Badge, { variant: "outline", className: "sx-badge", style: { borderColor: bandColor(safety.band), color: bandColor(safety.band) } }, safety.label || o.status || "unknown"),
        ),
        o.message_preview ? React.createElement("p", { className: "sx-summary" }, o.message_preview) : null,
        React.createElement("p", { className: "sx-summary sx-safety" }, safety.detail || "No safety detail."),
        React.createElement("div", { className: "sx-meta" },
          React.createElement("span", null, o.surface || "—", " / ", o.delivery_mode || "—"),
          React.createElement("span", null, "thread: ", o.origin_thread_id || "—", " (", safety.origin_thread_status || "—", ")"),
          React.createElement("span", null, "action: ", safety.attached_action_id || "—", safety.attached_action_status ? " (" + safety.attached_action_status + ")" : ""),
          React.createElement("span", null, "updated: ", timeText(o.updated_at || o.created_at)),
        ),
        React.createElement("div", { className: "sx-ref-row" },
          React.createElement(Pill, { band: safety.outbound_delivery ? "yellow" : "green" }, safety.outbound_delivery ? "outbound mode" : "no direct delivery"),
          React.createElement(Pill, null, safety.dispatch_requires_execute ? "requires execute=True" : "no execute pending"),
          (o.media_refs || []).slice(0, 4).map(function (id) { return React.createElement(Pill, { key: id }, "media ", id); }),
        ),
      ),
    );
  }

  function WarningCard(props) {
    const w = props.warning;
    return React.createElement(Card, { className: "sx-card sx-warning-card", style: { borderColor: bandColor(w.band) } },
      React.createElement(CardContent, { className: "sx-card-content" },
        React.createElement("div", { className: "sx-row sx-between" },
          React.createElement("div", null,
            React.createElement("div", { className: "sx-title" }, w.label || w.kind || "warning"),
            React.createElement("div", { className: "sx-id" }, w.id || "—"),
          ),
          React.createElement(Pill, { band: w.band }, w.kind || "lifecycle"),
        ),
        React.createElement("p", { className: "sx-summary" }, w.detail || "No detail."),
      ),
    );
  }

  function DecisionRow(props) {
    const d = props.decision;
    return React.createElement("div", { className: "sx-decision-row" },
      React.createElement("span", { className: "sx-decision-type" }, d.type || "decision"),
      React.createElement("span", { className: "sx-decision-time" }, timeText(d.ts)),
      React.createElement("span", { className: "sx-decision-ref" }, d.thread_id || d.candidate_id || d.action_id || d.artifact_id || d.outbox_id || "—"),
      d.reason ? React.createElement("span", { className: "sx-muted" }, d.reason) : null,
    );
  }

  function SensoriumPage() {
    const state = useSnapshot(60 * 1000);
    const data = state.data;
    const counts = data && data.counts ? data.counts : {};
    const health = data && data.health ? data.health : {};

    return React.createElement("div", { className: "sx-page" },
      React.createElement("div", { className: "sx-hero" },
        React.createElement("div", null,
          React.createElement("p", { className: "sx-kicker" }, "Agent Sensorium"),
          React.createElement("h1", null, "Inner lifecycle cockpit"),
          React.createElement("p", null, "Candidates, conscious threads, actions, artifacts, outbox requests, and delivery-safety receipts. Read-only. Local state remains source of truth."),
        ),
        React.createElement("div", { className: "sx-actions" },
          health.status ? React.createElement(Badge, { variant: "outline", className: "sx-status", style: { borderColor: bandColor(health.band), color: bandColor(health.band) } }, health.status) : null,
          React.createElement(Button, { variant: "outline", size: "sm", onClick: state.reload, disabled: state.loading }, state.loading ? "Refreshing…" : "Refresh"),
        ),
      ),
      state.error ? React.createElement(Card, { className: "sx-card sx-error" }, React.createElement(CardContent, null, state.error)) : null,
      !data ? React.createElement(Empty, { text: state.loading ? "Loading Sensorium snapshot…" : "No Sensorium snapshot yet." }) : React.createElement(React.Fragment, null,
        React.createElement("div", { className: "sx-metrics" },
          React.createElement(MetricCard, { label: "Active threads", value: counts.active_threads ?? 0, hint: "Dormant / held" }),
          React.createElement(MetricCard, { label: "Candidates", value: counts.active_candidates ?? 0, hint: "Above attention floor" }),
          React.createElement(MetricCard, { label: "Actions", value: counts.open_actions ?? 0, hint: "Open / total " + (counts.actions ?? 0) }),
          React.createElement(MetricCard, { label: "Artifacts", value: counts.held_artifacts ?? 0, hint: "Held / total " + (counts.artifacts ?? 0) }),
          React.createElement(MetricCard, { label: "Outbox", value: counts.actionable_outbox ?? 0, hint: "Actionable / prepared " + (counts.prepared_outbox ?? 0) }),
          React.createElement(MetricCard, { label: "Warnings", value: counts.lifecycle_warnings ?? 0, hint: "Lifecycle gaps" }),
        ),
        React.createElement("div", { className: "sx-grid" },
          React.createElement(Section, { title: "Lifecycle warnings", subtitle: "Dangling, actionable, or inconsistent actuator state." },
            data.lifecycle_warnings && data.lifecycle_warnings.length ? data.lifecycle_warnings.map(function (w, i) { return React.createElement(WarningCard, { key: String(i) + (w.id || ""), warning: w }); }) : React.createElement(Empty, { text: "No lifecycle gaps detected." }),
          ),
          React.createElement(Section, { title: "Thread actions", subtitle: "Prepared/acted motor plans plus attached refs." },
            data.actions && data.actions.length ? data.actions.map(function (a) { return React.createElement(ActionCard, { key: a.id, action: a }); }) : React.createElement(Empty, { text: "No thread actions." }),
          ),
        ),
        React.createElement("div", { className: "sx-grid" },
          React.createElement(Section, { title: "Outbox", subtitle: "Pointers and delivery requests with explicit safety labels." },
            data.outbox && data.outbox.length ? data.outbox.map(function (o) { return React.createElement(OutboxCard, { key: o.id, request: o }); }) : React.createElement(Empty, { text: "No outbox requests." }),
          ),
          React.createElement(Section, { title: "Artifacts", subtitle: "Held media/text refs; no raw private content is displayed." },
            data.artifacts && data.artifacts.length ? data.artifacts.map(function (a) { return React.createElement(ArtifactCard, { key: a.id, artifact: a }); }) : React.createElement(Empty, { text: "No artifacts recorded." }),
          ),
        ),
        React.createElement("div", { className: "sx-grid" },
          React.createElement(Section, { title: "Visible threads", subtitle: "The actual continuity units." },
            data.threads && data.threads.length ? data.threads.map(function (t) { return React.createElement(ThreadCard, { key: t.id, thread: t }); }) : React.createElement(Empty, { text: "No dormant or held threads visible." }),
          ),
          React.createElement(Section, { title: "Top candidates", subtitle: "Pressure sorted, compact summaries only." },
            data.top_candidates && data.top_candidates.length ? data.top_candidates.map(function (c) { return React.createElement(CandidateCard, { key: c.id, candidate: c }); }) : React.createElement(Empty, { text: "No active candidates." }),
          ),
        ),
        React.createElement(Section, { title: "Recent receipts", subtitle: "Decision log tail." },
          data.decisions && data.decisions.length ? React.createElement(Card, { className: "sx-card" }, React.createElement(CardContent, { className: "sx-decision-list" }, data.decisions.map(function (d, i) { return React.createElement(DecisionRow, { key: String(i) + (d.ts || ""), decision: d }); }))) : React.createElement(Empty, { text: "No decisions yet." }),
        ),
        React.createElement("div", { className: "sx-footer" },
          React.createElement("span", null, "state: ", data.state_dir),
          React.createElement("span", null, "generated: ", timeText(data.generated_at)),
          React.createElement("span", null, "freshness: ", timeText(data.state_mtime)),
        ),
      ),
    );
  }

  PLUGINS.register("agent-sensorium", SensoriumPage);
})();
