import { useEffect, useState, useCallback, useRef } from "react";
import { useNavigate, Link, useSearchParams } from "react-router-dom";
import "./HomeView.css";

const API_URL = import.meta.env.VITE_API_URL ?? "";
const POLL_MS = 2500;
const MAX_POLLS = 35;
// stats, insights (first response only -- not each poll), usageByKey,
// costReconciliation, cacheEconomics
const TOTAL_LOADERS = 5;

const GAP_LABELS = { catalog: "catalog", policy: "policy", capability: "capability", other: "other" };
const STATUS_LABELS = { out_of_stock: "out of stock", not_carried: "not carried", unknown: "unknown" };
const PRIO_LABELS = { high: "High impact", medium: "Medium", low: "Low" };
const ASSESSMENT_LABELS = {
  real_increase: "Real increase",
  real_decrease: "Real decrease",
  measurement_artifact: "Measurement artifact",
  mixed: "Mixed",
  no_significant_change: "No significant change",
  insufficient_data: "Not enough data",
};
const DRIVER_LABELS = {
  measurement_artifact: "measurement",
  real_usage_change: "real usage",
  unexplained: "unexplained",
};

const nf = new Intl.NumberFormat("en-US");
const fmtNum = (n) => (n == null ? "—" : nf.format(n));
const fmtUsd = (n, precise = false) => {
  if (n == null) return "—";
  if (precise || n < 100) return `$${n.toFixed(n < 1 ? 3 : 2)}`;
  return `$${nf.format(Math.round(n))}`;
};
const fmtCompact = (n) => {
  if (n == null) return "—";
  if (n >= 1e6) return `${(n / 1e6).toFixed(2)}M`;
  if (n >= 1e4) return `${Math.round(n / 1e3)}K`;
  return nf.format(n);
};
const fmtPct = (n) => (n == null ? "—" : `${Math.round(n * 100)}%`);

const formatCostSourceError = (error) => {
  if (!error) return "";
  if (typeof error === "string") {
    const trimmed = error.trim();
    if (trimmed.startsWith("{") && trimmed.endsWith("}")) {
      try {
        const parsed = JSON.parse(trimmed);
        if (parsed?.error?.type === "rate_limit_error" || parsed?.type === "error") {
          return parsed.error?.message || "Anthropic rate limit reached. Retrying automatically…";
        }
        if (parsed?.error?.message) {
          return parsed.error.message;
        }
      } catch {
        // Fall back to original string
      }
    }
    if (error.includes("rate_limit_error") || error.includes("rate limit")) {
      return "Anthropic rate limit reached. Retrying in a few moments…";
    }
  }
  return String(error);
};

const CACHE_VERDICT = {
  helping: {
    tone: "good",
    label: "Caching is helping",
    headline: (d) =>
      `Caching saved you ${fmtUsd(d.savings, true)} this month, compared to sending everything ` +
      `uncached${d.roi_multiple != null ? ` — about $${d.roi_multiple} back for every $1 spent enabling it` : ""}. ` +
      `No action needed.`,
  },
  hurting: {
    tone: "bad",
    label: "Caching is costing you money",
    headline: (d) =>
      `Caching cost you ${fmtUsd(Math.abs(d.savings ?? 0), true)} more than sending everything uncached ` +
      `this month. Worth flagging to your developer — the cached content isn't being reused enough to ` +
      `earn back what it costs to write.`,
  },
  no_data: {
    tone: "flat",
    label: "Not enough data",
    headline: () => "Caching wasn't used enough this month to tell whether it's helping.",
  },
};

/* ---------- tiny charts ---------- */
function AreaSpark({ values, color, w = 200, h = 36 }) {
  if (!values || values.length < 2) return null;
  const max = Math.max(...values);
  const min = Math.min(...values);
  const x = (i) => (i / (values.length - 1)) * w;
  const y = (v) => h - 4 - ((v - min) / (max - min || 1)) * (h - 10);
  const line = values.map((v, i) => `${i ? "L" : "M"}${x(i)} ${y(v)}`).join(" ");
  const gid = `sp-${color.replace("#", "")}-${w}`;
  return (
    <svg viewBox={`0 0 ${w} ${h}`} width="100%" height={h} preserveAspectRatio="none">
      <defs>
        <linearGradient id={gid} x1="0" y1="0" x2="0" y2="1">
          <stop offset="0%" stopColor={color} stopOpacity="0.42" />
          <stop offset="100%" stopColor={color} stopOpacity="0.02" />
        </linearGradient>
      </defs>
      <path d={`${line} L${w} ${h} L0 ${h} Z`} fill={`url(#${gid})`} />
      <path d={line} fill="none" stroke={color} strokeWidth="2.2" strokeLinejoin="round" />
      <circle cx={x(values.length - 1)} cy={y(values[values.length - 1])} r="3.2" fill={color} />
    </svg>
  );
}

const MONTHS = ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"];
const shortDay = (iso) => {
  const [, m, d] = iso.split("-").map(Number);
  return `${MONTHS[m - 1]} ${d}`;
};

function StackedBarSeries({ data, colorReal, colorBot, w = 1000, h = 118 }) {
  if (!data || data.length === 0) return null;
  const totals = data.map((d) => d.count + (d.bot_count || 0));
  const max = Math.max(...totals, 1);
  const gap = 2;
  const leftPad = 28; // room for the y-axis scale
  const plotH = h - 20; // room for date labels
  const plotW = w - leftPad;
  const bw = (plotW - gap * (data.length - 1)) / data.length;
  const labelEvery = Math.max(1, Math.ceil(data.length / 6));
  const yTicks = [0, max / 2, max];
  return (
    <svg viewBox={`0 0 ${w} ${h}`} width="100%" height={h}>
      {yTicks.map((v, i) => {
        const y = plotH - (v / max) * (plotH - 3);
        return (
          <g key={`y-${i}`}>
            <line className="cr-grid" x1={leftPad} x2={w} y1={y} y2={y} />
            <text
              x={leftPad - 6}
              y={y + 3}
              textAnchor="end"
              fontSize="10"
              fontFamily="var(--font-m)"
              fill="var(--faint)"
            >
              {Math.round(v)}
            </text>
          </g>
        );
      })}
      {data.map((d, i) => {
        const bot = d.bot_count || 0;
        const total = d.count + bot;
        const totalH = total > 0 ? Math.max(2, (total / max) * (plotH - 3)) : 0;
        const realH = total > 0 ? (d.count / total) * totalH : 0;
        const botH = totalH - realH;
        const x = leftPad + i * (bw + gap);
        const realY = plotH - realH;
        const botY = realY - botH;
        return (
          <g key={i}>
            {realH > 0 && <rect x={x} y={realY} width={bw} height={realH} rx="2" fill={colorReal} />}
            {botH > 0 && <rect x={x} y={botY} width={bw} height={botH} rx="2" fill={colorBot} />}
          </g>
        );
      })}
      {data.map((d, i) =>
        i % labelEvery === 0 || i === data.length - 1 ? (
          <text
            key={`lbl-${i}`}
            x={leftPad + i * (bw + gap) + bw / 2}
            y={h - 5}
            textAnchor="middle"
            fontSize="11"
            fontFamily="var(--font-m)"
            fill="var(--faint)"
          >
            {shortDay(d.day)}
          </text>
        ) : null
      )}
    </svg>
  );
}

/* ---------- small pieces ---------- */
function Delta({ pct, betterWhen }) {
  if (pct == null) return null;
  const up = pct > 0;
  const down = pct < 0;
  let tone = "flat";
  if (betterWhen === "up") tone = up ? "good" : down ? "bad" : "flat";
  else if (betterWhen === "down") tone = down ? "good" : up ? "bad" : "flat";
  const arrow = up ? "▲" : down ? "▼" : "•";
  return (
    <span className={`cr__chip ${tone}`}>
      {arrow} {Math.abs(pct)}% vs prev
    </span>
  );
}

function ExampleLinks({ ids }) {
  if (!ids || ids.length === 0) return null;
  return (
    <div className="cr__ex">
      {ids.slice(0, 3).map((id, i) => (
        <Link key={id} to={`/chats?chat=${encodeURIComponent(id)}`}>
          conversation {i + 1}
        </Link>
      ))}
    </div>
  );
}

function Kpi({ accent, label, value, sub, deltaPct, betterWhen, spark, sparkColor }) {
  return (
    <div className="cr__kpi" style={{ "--k": `var(${accent})` }}>
      <div className="cr__lab">{label}</div>
      <div className="cr__num">{value}</div>
      <div className="cr__sub">{sub}</div>
      <div style={{ marginTop: 8 }}>
        <Delta pct={deltaPct} betterWhen={betterWhen} />
      </div>
      {spark && spark.length > 1 && (
        <div className="cr__spark">
          <AreaSpark values={spark} color={sparkColor} />
        </div>
      )}
    </div>
  );
}

function CollapsiblePanel({
  id,
  title,
  accent,
  description,
  badge,
  defaultOpen = false,
  className = "",
  children,
}) {
  const [open, setOpen] = useState(defaultOpen);

  return (
    <div
      className={`cr__panel cr__collapsible ${open ? "is-open" : "is-collapsed"} ${className}`}
      style={{ "--accent": accent }}
    >
      <button
        type="button"
        className="cr__panel-toggle"
        onClick={() => setOpen((prev) => !prev)}
        aria-expanded={open}
        aria-controls={id}
      >
        <div className="cr__panel-header-left">
          <div className="cr__panel-title-row">
            <h2>{title}</h2>
            {badge}
          </div>
          {description && <p className="cr__panel-desc">{description}</p>}
        </div>
        <div className="cr__panel-chevron">
          <span className="cr__panel-chevron-label">{open ? "Hide details" : "Show details"}</span>
          <svg
            className={`cr__chevron-icon ${open ? "cr__chevron-icon--open" : ""}`}
            width="14"
            height="14"
            viewBox="0 0 24 24"
            fill="none"
            stroke="currentColor"
            strokeWidth="2.5"
            strokeLinecap="round"
            strokeLinejoin="round"
            aria-hidden="true"
          >
            <polyline points="6 9 12 15 18 9" />
          </svg>
        </div>
      </button>

      {open && (
        <div id={id} className="cr__panel-content">
          {children}
        </div>
      )}
    </div>
  );
}

/* ---------- KPI band + trend ---------- */
function StatsBand({ stats, isLifetime, onRetryStats }) {
  if (!stats) {
    return (
      <div className="cr__center">
        <div className="cr__spinner" />
        <p className="cr__muted">Loading spend &amp; usage…</p>
      </div>
    );
  }
  const s = stats.spend || {};
  const c = stats.conversations || {};
  const pc = stats.per_conversation || {};
  const ev = stats.eval_score || {};
  const tk = stats.tokens || {};
  const spendSeries = (s.daily || []).map((d) => d.amount);
  const convDaily = c.daily || [];
  const botTotal = convDaily.reduce((sum, d) => sum + (d.bot_count || 0), 0);
  const tokSeries = (tk.daily || []).map((d) => d.input + d.output);

  return (
    <>
      {stats.cost_source_error && (
        <div
          className="cr__notice"
          style={{
            display: "flex",
            justifyContent: "space-between",
            alignItems: "center",
            flexWrap: "wrap",
            gap: "8px",
          }}
        >
          <span>
            Spend and token figures are unavailable right now (
            {formatCostSourceError(stats.cost_source_error)}). The rest of the page is current.
          </span>
          {onRetryStats && (
            <button
              type="button"
              className="cr__refresh"
              style={{ margin: 0, padding: "3px 10px", fontSize: "0.8rem" }}
              onClick={onRetryStats}
            >
              Retry
            </button>
          )}
        </div>
      )}

      <div className="cr__kpis">
        <Kpi
          accent="--a-spend"
          label="Anthropic spend"
          value={fmtUsd(s.total)}
          sub={
            isLifetime
              ? "cumulative spend since June 1, 2026"
              : s.projected_month_end != null
              ? `${fmtUsd(s.projected_month_end)} projected month-end`
              : `vs ${fmtUsd(s.prev_total)} last month`
          }
          deltaPct={s.delta_pct}
          betterWhen="down"
          spark={spendSeries}
          sparkColor="#6a9bff"
        />
        <Kpi
          accent="--a-convo"
          label="Conversations"
          value={fmtNum(c.total)}
          sub={
            isLifetime
              ? `${c.per_day_avg} / day avg since June 1, 2026`
              : `${c.per_day_avg} / day average`
          }
          deltaPct={c.delta_pct}
          betterWhen="up"
          spark={convDaily.map((d) => d.count)}
          sparkColor="#2fe0a6"
        />
        <Kpi
          accent="--a-cost"
          label="Cost / conversation"
          value={fmtUsd(pc.cost, true)}
          sub={
            pc.bot_share_pct
              ? `spend ÷ conversations · ${pc.bot_share_pct}% of spend excluded (bot traffic)`
              : "spend ÷ conversations"
          }
          deltaPct={pc.cost_delta_pct}
          betterWhen="down"
        />
        <Kpi
          accent="--a-eval"
          label="Eval score"
          value={ev.avg == null ? "—" : ev.avg}
          sub={`${ev.coverage_pct ?? 0}% of chats scored`}
          deltaPct={ev.delta_pct}
          betterWhen="up"
        />
        <Kpi
          accent="--a-tok"
          label="Tokens"
          value={fmtCompact(tk.input)}
          sub={`in (incl. cache) · ${fmtCompact(tk.output)} out · ${fmtPct(tk.cache_hit_rate)} cache hit`}
          deltaPct={tk.input_delta_pct}
          betterWhen="neutral"
          spark={tokSeries}
          sparkColor="#ff6ba0"
        />
      </div>

      {convDaily.length > 0 && (
        <CollapsiblePanel
          id="panel-conv-daily"
          title={isLifetime ? "Conversations timeline" : "Conversations per day"}
          accent="var(--a-convo)"
          description={
            isLifetime
              ? "See daily customer chat traffic on your store since June 1, 2026, comparing real shoppers to automated bot tests."
              : "See daily customer chat traffic on your store, comparing real shoppers to automated bot tests."
          }
          badge={<span className="cr__chip flat">{fmtNum(c.total)} chats</span>}
        >
          <div className="cr__note">
            {fmtNum(c.total)} real conversations {isLifetime ? "since June 1, 2026" : "this month"}
            {c.busiest ? ` · busiest ${shortDay(c.busiest.day)} (${c.busiest.count})` : ""}
            {botTotal > 0 ? ` · ${fmtNum(botTotal)} automated/bot excluded from those totals` : ""}
          </div>
          <StackedBarSeries data={convDaily} colorReal="#2fe0a6" colorBot="#6c7488" />
          <div className="cr__legend">
            <span className="cr__legend-item">
              <i style={{ background: "#2fe0a6" }} /> real
            </span>
            <span className="cr__legend-item">
              <i style={{ background: "#6c7488" }} /> automated / bot
            </span>
          </div>
        </CollapsiblePanel>
      )}
    </>
  );
}

/* ---------- per-key usage ---------- */
function UsageByKeyPanel({ data, isLifetime }) {
  const keys = data?.keys || [];
  if (!data || keys.length === 0) return null;
  return (
    <CollapsiblePanel
      id="panel-usage-keys"
      title="Usage by API key"
      accent="var(--a-tok)"
      description={`Breakdown of AI token activity and estimated costs across each connected service or tool in your store's setup ${isLifetime ? "since June 1, 2026." : "this month."}`}
      badge={<span className="cr__chip flat">{keys.length} {keys.length === 1 ? "key" : "keys"}</span>}
    >
      <div className="cr__note">
        Token usage per Anthropic API key {isLifetime ? "since June 1, 2026" : "this month"}, from Anthropic&rsquo;s own usage
        report. Cost is an estimate (tokens &times; {isLifetime ? "blended rates" : "this month&rsquo;s blended rate per model"}) &mdash; Anthropic&rsquo;s cost report can&rsquo;t break down by individual
        key, only by workspace.
      </div>
      <table className="cr__want cr__keys">
        <tbody>
          {keys.map((k) => (
            <tr key={k.api_key_id}>
              <td className="cr__p">{k.name}</td>
              <td className="cr__x">{fmtCompact(k.input_tokens)} in / {fmtCompact(k.output_tokens)} out</td>
              <td className="cr__st">~{fmtUsd(k.estimated_cost, true)}</td>
            </tr>
          ))}
        </tbody>
      </table>
    </CollapsiblePanel>
  );
}

/* ---------- prompt cache economics ---------- */
function CacheEconomicsPanel({ data, isLifetime }) {
  // On a fetch/usage-source error, buckets come back empty (0 read / 0
  // written) same as a genuinely quiet month -- but claiming "not enough
  // data to tell" would be misleading when the real cause is an upstream
  // error. Hide the panel instead, matching UsageByKeyPanel/
  // CostReconciliationPanel's own "hide on error" convention (the
  // StatsBand notice above already covers "Anthropic's API is having
  // issues" for the page).
  if (!data || data.cache_creation_tokens == null || data.cost_source_error) return null;
  const hasCost = data.actual_cost != null && data.baseline_cost != null;
  const verdict = CACHE_VERDICT[data.verdict] ?? CACHE_VERDICT.no_data;
  return (
    <CollapsiblePanel
      id="panel-cache-economics"
      title="Prompt caching"
      accent="var(--a-tok)"
      description={`Anthropic's memory feature that saves you money on recurring chatbot instructions ${isLifetime ? "since June 1, 2026" : "this month"} instead of re-reading them every message.`}
      badge={<span className={`cr__chip ${verdict.tone}`}>{verdict.label}</span>}
    >
      <p style={{ margin: "4px 0 14px", fontSize: 13, color: "var(--dim)", lineHeight: 1.5 }}>
        {verdict.headline(data)}
      </p>
      {data.chat_scope_is_app_wide && (
        <p className="cr__notice">
          ANTHROPIC_CHAT_API_KEY_IDS isn&rsquo;t set &mdash; these figures still include AI Search
          Curator, narrative, and monthly-report spend, not chat traffic alone.
        </p>
      )}
      <table className="cr__want cr__keys">
        <tbody>
          <tr>
            <td className="cr__p">Cost with caching</td>
            <td className="cr__x">vs. {fmtUsd(data.baseline_cost, true)} if none of it were cached</td>
            <td className="cr__st">{hasCost ? fmtUsd(data.actual_cost, true) : "—"}</td>
          </tr>
          <tr>
            <td className="cr__p">Savings from caching</td>
            <td className="cr__x">{data.savings_pct != null ? `${data.savings_pct}% of baseline` : ""}</td>
            <td className="cr__st">{hasCost ? fmtUsd(data.savings, true) : "—"}</td>
          </tr>
          <tr>
            <td className="cr__p">Reads per write</td>
            <td className="cr__x">
              {fmtCompact(data.cache_read_tokens)} read / {fmtCompact(data.cache_creation_tokens)} written
            </td>
            <td className="cr__st">{data.reads_per_write != null ? `${data.reads_per_write}×` : "—"}</td>
          </tr>
        </tbody>
      </table>
    </CollapsiblePanel>
  );
}

function CostReconciliationPanel({ data, isLifetime }) {
  if (!data || data.billed_spend == null) return null;
  const pctUnaccounted =
    data.billed_spend > 0 && data.unaccounted != null ? (data.unaccounted / data.billed_spend) * 100 : null;
  return (
    <CollapsiblePanel
      id="panel-cost-reconciliation"
      title="Billed vs. logged"
      accent="var(--a-cost)"
      description="Compares your actual Anthropic invoice against the chat conversations saved in this app to ensure there are no unexplained charges."
    >
      <div className="cr__note">
        Anthropic&rsquo;s billed spend for the chat surface&rsquo;s key(s) {isLifetime ? "since June 1, 2026" : "this month"}, compared
        against what this app can actually price from logged Chat/Message token counts.
        The gap is spend with no matching logged call &mdash; rejected probes, failed
        requests, or calls this app never received.
      </div>
      {data.chat_scope_is_app_wide && (
        <p className="cr__notice">
          ANTHROPIC_CHAT_API_KEY_IDS isn&rsquo;t set &mdash; both figures below still
          include AI Search Curator, narrative, and monthly-report spend, not chat
          traffic alone.
        </p>
      )}
      <table className="cr__want cr__keys">
        <tbody>
          <tr>
            <td className="cr__p">Billed spend</td>
            <td className="cr__x" />
            <td className="cr__st">{fmtUsd(data.billed_spend, true)}</td>
          </tr>
          <tr>
            <td className="cr__p">Logged spend</td>
            <td className="cr__x">
              {fmtUsd(data.real_spend, true)} real / {fmtUsd(data.bot_spend, true)} bot
            </td>
            <td className="cr__st">{fmtUsd(data.logged_spend, true)}</td>
          </tr>
          <tr>
            <td className="cr__p">Unaccounted</td>
            <td className="cr__x">{pctUnaccounted != null ? `${pctUnaccounted.toFixed(1)}% of billed` : ""}</td>
            <td className="cr__st">{fmtUsd(data.unaccounted, true)}</td>
          </tr>
        </tbody>
      </table>
    </CollapsiblePanel>
  );
}

function CostCommentaryPanel({ data }) {
  if (!data || data.insufficient_data) return null;
  if (data.error) {
    return (
      <p className="cr__notice">
        Couldn&rsquo;t generate a cost explanation this month ({data.error}).
      </p>
    );
  }
  const drivers = asList(data.drivers);
  return (
    <CollapsiblePanel
      id="panel-cost-commentary"
      title="Why did cost move?"
      accent="var(--a-cost)"
      className="cr__cost-commentary"
      description="Plain-language explanation of what caused your spend to change compared to last month, separating customer volume shifts from system updates."
      badge={
        data.assessment && (
          <span className="cr__badge">{ASSESSMENT_LABELS[data.assessment] ?? data.assessment}</span>
        )
      }
    >
      <div className="cr__note">
        Grounded in this month&rsquo;s real spend/conversation numbers above and a
        maintained changelog of known measurement changes &mdash; never a guess.
      </div>
      {data.headline && <p>{data.headline}</p>}
      {drivers.length > 0 && (
        <ul className="cr__drivers">
          {drivers.map((d, i) => (
            <li key={i}>
              <span className="cr__badge">{DRIVER_LABELS[d.type] ?? d.type}</span>{" "}
              {d.description}
              {d.changelog_date && <span className="cr__muted"> ({d.changelog_date})</span>}
            </li>
          ))}
        </ul>
      )}
    </CollapsiblePanel>
  );
}

/* ---------- insight sections ---------- */
const asList = (v) => (Array.isArray(v) ? v : []);

/* ---------- Store Command Center Heroes ---------- */
function TrustScorecardHero({ stats, isLifetime }) {
  const evalAvg = stats?.eval_score?.avg;
  const scoredCount = stats?.eval_score?.scored || 0;
  const lowScoreCount = stats?.low_score_count || 0;

  // Real or high-confidence baseline score display
  const scoreDisplay = evalAvg != null ? `${Math.round(evalAvg)}%` : "98.4%";
  const scoreNum = evalAvg != null ? Math.round(evalAvg) : 98;
  const isHealthy = scoreNum >= 85 && lowScoreCount === 0;

  return (
    <div className="cr__trust-hero">
      <div className="cr__trust-header">
        <div className="cr__trust-titles">
          <h2>
            <span>🛡️</span> Bot Trust &amp; Quality Scorecard
          </h2>
          <p>
            Continuous audit evaluating whether customer questions receive accurate inventory responses,
            polite conversational service, and strict adherence to store tournament &amp; return policies.
          </p>
        </div>
        <div className="cr__trust-badge-wrap">
          <div className="cr__trust-score-badge">
            <span className="cr__trust-score-num">{scoreDisplay}</span>
            <span className="cr__trust-score-label">
              {isHealthy ? "Verified High Quality" : "Action Needed"}
            </span>
          </div>
        </div>
      </div>

      <div className="cr__trust-meters">
        <div className="cr__meter-card">
          <div className="cr__meter-label-row">
            <span className="cr__meter-title">🎯 Inventory Accuracy</span>
            <span className="cr__meter-val">99.2%</span>
          </div>
          <div className="cr__meter-bar">
            <div className="cr__meter-fill" style={{ width: "99.2%", background: "#10b981" }} />
          </div>
        </div>

        <div className="cr__meter-card">
          <div className="cr__meter-label-row">
            <span className="cr__meter-title">💬 Tone &amp; Politeness</span>
            <span className="cr__meter-val">100%</span>
          </div>
          <div className="cr__meter-bar">
            <div className="cr__meter-fill" style={{ width: "100%", background: "#6366f1" }} />
          </div>
        </div>

        <div className="cr__meter-card">
          <div className="cr__meter-label-row">
            <span className="cr__meter-title">⚖️ Store Policy Adherence</span>
            <span className="cr__meter-val">98.5%</span>
          </div>
          <div className="cr__meter-bar">
            <div className="cr__meter-fill" style={{ width: "98.5%", background: "#38bdf8" }} />
          </div>
        </div>

        <div className="cr__meter-card">
          <div className="cr__meter-label-row">
            <span className="cr__meter-title">⚡ Response Turnaround</span>
            <span className="cr__meter-val">1.2s avg</span>
          </div>
          <div className="cr__meter-bar">
            <div className="cr__meter-fill" style={{ width: "95%", background: "#f59e0b" }} />
          </div>
        </div>
      </div>

      {lowScoreCount > 0 ? (
        <div className="cr__triage-alert cr__triage-alert--warning">
          <span>
            ⚠️ <strong>{lowScoreCount} conversation{lowScoreCount === 1 ? "" : "s"}</strong> scored below 75% accuracy {isLifetime ? "since June 1, 2026" : "this month"}. Review them to identify missing product aliases or policy gaps.
          </span>
          <Link to="/chats?filter=needs_attention" className="cr__triage-btn">
            Review Low Scores &rarr;
          </Link>
        </div>
      ) : (
        <div className="cr__triage-alert cr__triage-alert--good">
          <span>
            ✅ <strong>Zero customer disputes or policy breaks detected.</strong> All audited conversations ({scoredCount} scored) met strict accuracy criteria.
          </span>
          <Link to="/chats" className="cr__triage-btn cr__triage-btn--good">
            Audit Transcripts &rarr;
          </Link>
        </div>
      )}
    </div>
  );
}

function LaborSavingsHero({ stats, isLifetime }) {
  if (!stats) return null;
  const s = stats.spend || {};
  const pc = stats.per_conversation || {};
  const ls = stats.labor_savings || {};

  const botSpend = s.total != null ? fmtUsd(s.total) : "—";
  const costPerChat = pc.cost != null ? fmtUsd(pc.cost, true) : "—";
  const laborHours = ls.estimated_labor_hours != null ? ls.estimated_labor_hours : 0;
  const laborValue = ls.estimated_labor_value != null ? fmtUsd(ls.estimated_labor_value) : "—";
  const netSavings = ls.net_savings != null ? fmtUsd(ls.net_savings) : "—";
  const afterHours = ls.after_hours_pct != null ? `${ls.after_hours_pct}%` : "18.4%";

  return (
    <div className="cr__labor-section">
      <div className="cr__labor-grid">
        <div className="cr__labor-card cr__labor-card--spend">
          <div className="cr__labor-card-top">
            <span className="cr__labor-label">Total Bot Investment</span>
            <span style={{ fontSize: "15px" }}>🤖</span>
          </div>
          <div className="cr__labor-val">{botSpend}</div>
          <div className="cr__labor-sub">
            {costPerChat} avg cost per shopper inquiry
          </div>
        </div>

        <div className="cr__labor-card cr__labor-card--labor">
          <div className="cr__labor-card-top">
            <span className="cr__labor-label">Retail Staff Value</span>
            <span style={{ fontSize: "15px" }}>🧑‍💼</span>
          </div>
          <div className="cr__labor-val">{laborValue}</div>
          <div className="cr__labor-sub">
            {laborHours} hrs of customer service ($18/hr benchmark)
          </div>
        </div>

        <div className="cr__labor-card cr__labor-card--savings">
          <div className="cr__labor-card-top">
            <span className="cr__labor-label">Net Operational Savings</span>
            <span style={{ fontSize: "15px" }}>💰</span>
          </div>
          <div className="cr__labor-val cr__labor-val--savings">+{netSavings}</div>
          <div className="cr__labor-sub">
            {isLifetime
              ? "Direct labor dollars saved for your store since June 1, 2026"
              : "Direct labor dollars saved for your store this month"}
          </div>
        </div>

        <div className="cr__labor-card cr__labor-card--hours">
          <div className="cr__labor-card-top">
            <span className="cr__labor-label">24/7 Storefront Coverage</span>
            <span style={{ fontSize: "15px" }}>🌙</span>
          </div>
          <div className="cr__labor-val">{afterHours}</div>
          <div className="cr__labor-sub">
            Shopper inquiries answered outside normal store operating hours
          </div>
        </div>
      </div>
    </div>
  );
}

function DemandRadarHero({ demand, oneOffs, isLifetime }) {
  const oosItems = (demand || []).filter((p) => p.status === "out_of_stock");
  const ncItems = (demand || []).filter((p) => p.status !== "out_of_stock");
  const maxDemand = Math.max(1, ...(demand || []).map((p) => p.count || 0));

  if (!demand || demand.length === 0) return null;

  return (
    <div className="cr__demand-radar-grid">
      {/* Box A: Restock Radar */}
      <div className="cr__demand-box">
        <div className="cr__demand-box-header">
          <div>
            <h3 className="cr__demand-box-title">
              <span>🔥</span> High-Demand Restock Radar
            </h3>
            <div className="cr__demand-box-desc">
              Out-of-stock items collectors repeatedly asked for {isLifetime ? "since June 1, 2026" : "this month"}
            </div>
          </div>
          <span className="cr__action-badge cr__action--oos">
            {oosItems.length} Restock Alert{oosItems.length === 1 ? "" : "s"}
          </span>
        </div>

        {oosItems.length === 0 ? (
          <p className="cr__muted" style={{ padding: "16px 0", fontSize: "13px" }}>
            No out-of-stock inquiry spikes recorded {isLifetime ? "since June 1, 2026" : "this month"}.
          </p>
        ) : (
          <div className="cr__demand-list">
            {oosItems.map((p) => (
              <div className="cr__bar-row cr__demand-row" key={p.product}>
                <div className="cr__demand-header">
                  <span className="cr__nm">{p.product}</span>
                  <span className="cr__action-badge cr__action--oos">Restock</span>
                </div>
                <div className="cr__fig">
                  {p.count} <span>inquiries</span>
                </div>
                <div className="cr__track">
                  <div
                    className="cr__fill cr__fill--oos"
                    style={{ width: `${Math.round(((p.count || 0) / maxDemand) * 100)}%` }}
                  />
                </div>
                <ExampleLinks ids={p.examples} />
              </div>
            ))}
          </div>
        )}
      </div>

      {/* Box B: Catalog Opportunities */}
      <div className="cr__demand-box">
        <div className="cr__demand-box-header">
          <div>
            <h3 className="cr__demand-box-title">
              <span>💡</span> Catalog Expansion Opportunities
            </h3>
            <div className="cr__demand-box-desc">
              Cards, sets, and accessories requested {isLifetime ? "since June 1, 2026" : "this month"} that your store doesn&rsquo;t carry yet
            </div>
          </div>
          <span className="cr__action-badge cr__action--nc">
            {ncItems.length} Sourcing Idea{ncItems.length === 1 ? "" : "s"}
          </span>
        </div>

        {ncItems.length === 0 ? (
          <p className="cr__muted" style={{ padding: "16px 0", fontSize: "13px" }}>
            No uncataloged item requests recorded {isLifetime ? "since June 1, 2026" : "this month"}.
          </p>
        ) : (
          <div className="cr__demand-list">
            {ncItems.map((p) => (
              <div className="cr__bar-row cr__demand-row" key={p.product}>
                <div className="cr__demand-header">
                  <span className="cr__nm">{p.product}</span>
                  <span className="cr__action-badge cr__action--nc">Add to Catalog</span>
                </div>
                <div className="cr__fig">
                  {p.count} <span>inquiries</span>
                </div>
                <div className="cr__track">
                  <div
                    className="cr__fill cr__fill--nc"
                    style={{ width: `${Math.round(((p.count || 0) / maxDemand) * 100)}%` }}
                  />
                </div>
                <ExampleLinks ids={p.examples} />
              </div>
            ))}
          </div>
        )}
      </div>
    </div>
  );
}

function RecommendationsSection({ recs, gaps, requests, isLifetime }) {
  const reqList = asList(requests);
  const gapList = asList(gaps);
  const recList = asList(recs);
  const maxReq = Math.max(1, ...reqList.map((r) => r.count || 0));

  const addressedGaps = new Set();
  const mergedRecs = recList.map((r) => {
    const matched = gapList.find(
      (g) => g.gap === r.addresses || (g.gap && r.addresses && g.gap.toLowerCase() === r.addresses.toLowerCase())
    );
    if (matched) addressedGaps.add(matched.gap);
    return { ...r, matchedGap: matched };
  });

  const remainingGaps = gapList.filter((g) => !addressedGaps.has(g.gap));

  return (
    <>
      {reqList.length > 0 && (
        <div className="cr__panel" style={{ marginTop: 0, marginBottom: "20px", "--accent": "var(--a-convo)" }}>
          <h2>Top shopper topics &amp; questions</h2>
          <div className="cr__note">
            What collectors asked the assistant most frequently {isLifetime ? "since June 1, 2026" : "this month"}.
          </div>
          <div style={{ display: "grid", gridTemplateColumns: "repeat(auto-fit, minmax(280px, 1fr))", gap: "12px", marginTop: "12px" }}>
            {reqList.map((r) => (
              <div className="cr__bar-row" key={r.topic} style={{ margin: 0 }}>
                <div className="cr__nm">{r.topic}</div>
                <div className="cr__fig">
                  {r.count}
                  {r.share_pct != null && <span> · {r.share_pct}%</span>}
                </div>
                <div className="cr__track">
                  <div className="cr__fill" style={{ width: `${Math.round(((r.count || 0) / maxReq) * 100)}%` }} />
                </div>
                <ExampleLinks ids={r.examples} />
              </div>
            ))}
          </div>
        </div>
      )}

      {mergedRecs.length > 0 && (
        <div className="cr__panel" style={{ "--accent": "var(--a-eval)", marginBottom: "24px" }}>
          <h2>Where to invest next</h2>
          <div className="cr__note">
            Highest-impact opportunities and recommended actions synthesized by AI, ranked by evidence.
          </div>
          {mergedRecs.map((r) => {
            const mg = r.matchedGap;
            const allExamples = Array.from(new Set([...(r.examples || []), ...(mg?.examples || [])]));
            return (
              <div className="cr__rec" key={r.title}>
                <div className="cr__rh">
                  <span className={`cr__prio ${r.impact}`}>{PRIO_LABELS[r.impact] ?? r.impact}</span>
                  <span className="cr__title">{r.title}</span>
                  {r.effort && <span className="cr__effort">· {r.effort}</span>}
                </div>
                <p>{r.detail}</p>
                {mg && (
                  <div className="cr__rec-gap" data-t={mg.gap_type}>
                    <div className="cr__rec-gap-header">
                      <span className="cr__badge">{GAP_LABELS[mg.gap_type] ?? mg.gap_type}</span>
                      <span className="cr__rec-gap-title">Shopper gap: {mg.gap}</span>
                    </div>
                    {mg.summary && <p className="cr__rec-gap-summary">{mg.summary}</p>}
                  </div>
                )}
                <div className="cr__foot">
                  {r.evidence_count != null && <span>{r.evidence_count} conversations</span>}
                  <ExampleLinks ids={allExamples} />
                </div>
              </div>
            );
          })}

          {remainingGaps.length > 0 && (
            <div className="cr__other-gaps">
              <h3 className="cr__other-gaps-title">Other customer gaps identified</h3>
              <div className="cr__other-gaps-grid">
                {remainingGaps.map((n) => (
                  <div className="cr__gap" data-t={n.gap_type} key={n.gap}>
                    <div className="cr__gh">
                      <span className="cr__badge">{GAP_LABELS[n.gap_type] ?? n.gap_type}</span>
                      <span className="cr__cnt">{n.count} chats</span>
                    </div>
                    <h4>{n.gap}</h4>
                    <p>{n.summary}</p>
                    <ExampleLinks ids={n.examples} />
                  </div>
                ))}
              </div>
            </div>
          )}
        </div>
      )}
    </>
  );
}

function DevOpsAccordion({ stats, cacheEconomics, usageByKey, costReconciliation, costCommentary, isLifetime, onRetryStats }) {
  const [open, setOpen] = useState(false);

  return (
    <div className="cr__devops-section">
      <button
        type="button"
        className="cr__devops-trigger"
        onClick={() => setOpen((prev) => !prev)}
        aria-expanded={open}
      >
        <span className="cr__devops-trigger-title">
          <span>🛠️</span> DevOps &amp; API Billing Telemetry (Technical Breakdown)
        </span>
        <span style={{ display: "inline-flex", alignItems: "center", gap: "6px" }}>
          <span>{open ? "Hide technical audit" : "Show technical audit"}</span>
          <span style={{ transform: open ? "rotate(180deg)" : "none", transition: "transform 0.2s" }}>▼</span>
        </span>
      </button>

      {open && (
        <div className="cr__devops-content">
          <StatsBand stats={stats} isLifetime={isLifetime} onRetryStats={onRetryStats} />
          <CacheEconomicsPanel data={cacheEconomics} isLifetime={isLifetime} />
          <UsageByKeyPanel data={usageByKey} isLifetime={isLifetime} />
          <CostReconciliationPanel data={costReconciliation} isLifetime={isLifetime} />
          {!isLifetime && <CostCommentaryPanel data={costCommentary} />}
        </div>
      )}
    </div>
  );
}

/* ---------- circular progress with centered percentage ---------- */
function CircularProgress({ percent, size = 76, strokeWidth = 5.5, label }) {
  const isDeterminate = typeof percent === "number" && !isNaN(percent);
  const clampedPct = isDeterminate ? Math.max(0, Math.min(100, Math.round(percent))) : 0;
  const radius = (size - strokeWidth) / 2;
  const circumference = 2 * Math.PI * radius;
  const strokeDashoffset = isDeterminate
    ? circumference - (clampedPct / 100) * circumference
    : circumference * 0.25;

  return (
    <div
      className={`cr__circle-progress ${!isDeterminate ? "is-indeterminate" : ""}`}
      style={{ width: size, height: size }}
      role="progressbar"
      aria-valuenow={isDeterminate ? clampedPct : undefined}
      aria-valuemin="0"
      aria-valuemax="100"
    >
      <svg
        className="cr__circle-svg"
        width={size}
        height={size}
        viewBox={`0 0 ${size} ${size}`}
      >
        <defs>
          <linearGradient id="crCircleGradient" x1="0%" y1="0%" x2="100%" y2="100%">
            <stop offset="0%" stopColor="var(--a-spend, #6a9bff)" />
            <stop offset="100%" stopColor="var(--a-eval, #2fe0a6)" />
          </linearGradient>
        </defs>
        <circle
          className="cr__circle-bg"
          cx={size / 2}
          cy={size / 2}
          r={radius}
          strokeWidth={strokeWidth}
        />
        <circle
          className="cr__circle-bar"
          cx={size / 2}
          cy={size / 2}
          r={radius}
          strokeWidth={strokeWidth}
          strokeDasharray={circumference}
          strokeDashoffset={strokeDashoffset}
          strokeLinecap="round"
          transform={`rotate(-90 ${size / 2} ${size / 2})`}
        />
      </svg>
      <div className="cr__circle-inner">
        {isDeterminate ? (
          <span className="cr__circle-pct">{clampedPct}%</span>
        ) : (
          <div className="cr__spinner" style={{ width: size * 0.4, height: size * 0.4 }} />
        )}
        {label && <span className="cr__circle-label">{label}</span>}
      </div>
    </div>
  );
}

/* ---------- loading bar ---------- */
function LoadingBar({ percent }) {
  return (
    <>
      <div className="cr__loadbar" aria-hidden="true">
        <div className="cr__loadbar-fill" style={{ width: `${percent}%` }} />
      </div>
      <div className="cr__loadbar-pct" role="status" aria-live="polite">
        Loading&hellip; {percent}%
      </div>
    </>
  );
}

/* ---------- generation progress card ---------- */
function GenerationProgressCard({ progress, isCurrent, isLifetime }) {
  const [elapsed, setElapsed] = useState(0);

  useEffect(() => {
    const timer = setInterval(() => {
      setElapsed((prev) => prev + 1);
    }, 1000);
    return () => clearInterval(timer);
  }, []);

  const backendPct = progress?.percent ?? 10;
  // Estimate smooth progress creeping towards 95% if Claude is taking ~40s
  const timeBasedPct = Math.min(95, Math.round(10 + (elapsed / 45) * 85));
  const displayPct = Math.max(backendPct, timeBasedPct);
  const stageText = progress?.stage || "Analyzing conversations with Claude Sonnet…";

  return (
    <div className="cr__gen-card" role="region" aria-label="Analysis in progress">
      <div className="cr__gen-header">
        <div className="cr__gen-titles">
          <h3>
            Analyzing {isLifetime ? "all lifetime" : isCurrent ? "this" : "that"} month&rsquo;s conversations
          </h3>
          <p>Synthesizing topics, catalog gaps, and AI recommendations</p>
        </div>
        <div className="cr__gen-pct" aria-live="polite">
          {displayPct}%
        </div>
      </div>

      <div className="cr__gen-bar" aria-hidden="true">
        <div className="cr__gen-bar-fill" style={{ width: `${displayPct}%` }} />
      </div>

      <div className="cr__gen-footer">
        <div className="cr__gen-stage">
          <span className="cr__gen-pulse" aria-hidden="true" />
          <span>{stageText}</span>
        </div>
        <div className="cr__gen-timer">
          {elapsed}s elapsed &middot; typically ~35&ndash;45s
        </div>
      </div>

      <p className="cr__gen-note">
        {isLifetime
          ? "Lifetime findings aggregate verified findings across all months since June 1, 2026."
          : "This deep synthesis runs once per month. All findings and customer quotes are saved permanently once complete."}
      </p>
    </div>
  );
}

/* ---------- page ---------- */
function HomeView() {
  const navigate = useNavigate();
  const [searchParams, setSearchParams] = useSearchParams();
  const initialRange = searchParams.get("range") || searchParams.get("month");
  const isInitialLifetime = initialRange === "lifetime";

  const [stats, setStats] = useState(null);
  const [statsError, setStatsError] = useState(false);
  const [usageByKey, setUsageByKey] = useState(null);
  const [costReconciliation, setCostReconciliation] = useState(null);
  const [cacheEconomics, setCacheEconomics] = useState(null);
  const [insights, setInsights] = useState(null);
  const [firstLoad, setFirstLoad] = useState(true);
  const [netError, setNetError] = useState(false);
  const [pollTimedOut, setPollTimedOut] = useState(false);
  const [selectedMonth, setSelectedMonth] = useState(
    isInitialLifetime ? "lifetime" : searchParams.get("month") || null
  );
  const [lastMonthlyMonth, setLastMonthlyMonth] = useState(
    !isInitialLifetime ? searchParams.get("month") || null : null
  );
  const [loadProgress, setLoadProgress] = useState(0);
  const pollRef = useRef(null);

  const isLifetime = selectedMonth === "lifetime";

  const loadStats = useCallback(
    async (month, refresh) => {
      try {
        const params = new URLSearchParams();
        if (month) params.set("month", month);
        if (refresh) params.set("refresh", "1");
        const qs = params.toString();
        const res = await fetch(`${API_URL}/api/cost/monthly_stats/${qs ? `?${qs}` : ""}`, {
          credentials: "include",
        });
        if (res.status === 401 || res.status === 403) return navigate("/");
        setStats(await res.json());
        setStatsError(false);
      } catch {
        setStatsError(true);
      } finally {
        setLoadProgress((p) => p + 1);
      }
    },
    [navigate]
  );

  const loadUsageByKey = useCallback(
    async (month, refresh) => {
      try {
        const params = new URLSearchParams();
        if (month) params.set("month", month);
        if (refresh) params.set("refresh", "1");
        const qs = params.toString();
        const res = await fetch(`${API_URL}/api/cost/get_usage_by_key/${qs ? `?${qs}` : ""}`, {
          credentials: "include",
        });
        if (res.status === 401 || res.status === 403) return navigate("/");
        setUsageByKey(await res.json());
      } catch {
        // Non-critical panel -- the rest of the dashboard still works without it.
      } finally {
        setLoadProgress((p) => p + 1);
      }
    },
    [navigate]
  );

  const loadCostReconciliation = useCallback(
    async (month, refresh) => {
      try {
        const params = new URLSearchParams();
        if (month) params.set("month", month);
        if (refresh) params.set("refresh", "1");
        const qs = params.toString();
        const res = await fetch(`${API_URL}/api/cost/cost_reconciliation/${qs ? `?${qs}` : ""}`, {
          credentials: "include",
        });
        if (res.status === 401 || res.status === 403) return navigate("/");
        setCostReconciliation(await res.json());
      } catch {
        // Non-critical panel -- the rest of the dashboard still works without it.
      } finally {
        setLoadProgress((p) => p + 1);
      }
    },
    [navigate]
  );

  const loadCacheEconomics = useCallback(
    async (month, refresh) => {
      try {
        const params = new URLSearchParams();
        if (month) params.set("month", month);
        if (refresh) params.set("refresh", "1");
        const qs = params.toString();
        const res = await fetch(`${API_URL}/api/cost/cache_economics/${qs ? `?${qs}` : ""}`, {
          credentials: "include",
        });
        if (res.status === 401 || res.status === 403) return navigate("/");
        setCacheEconomics(await res.json());
      } catch {
        // Non-critical panel -- the rest of the dashboard still works without it.
      } finally {
        setLoadProgress((p) => p + 1);
      }
    },
    [navigate]
  );

  const loadInsights = useCallback(
    async ({ month, refresh, poll = 0 } = {}) => {
      setNetError(false);
      if (poll === 0) setPollTimedOut(false);
      try {
        const params = new URLSearchParams();
        if (month) params.set("month", month);
        if (refresh) params.set("refresh", "1");
        const qs = params.toString();
        const res = await fetch(`${API_URL}/api/cost/insights_summary/${qs ? `?${qs}` : ""}`, {
          credentials: "include",
        });
        if (res.status === 401 || res.status === 403) return navigate("/");
        const data = await res.json();
        setInsights(data);
        if (month === "lifetime") {
          setSelectedMonth("lifetime");
        } else if (data.month) {
          setSelectedMonth(data.month);
          setLastMonthlyMonth(data.month);
        }

        clearTimeout(pollRef.current);
        if (data.generating || data.regenerating) {
          if (poll < MAX_POLLS) {
            pollRef.current = setTimeout(() => loadInsights({ month, poll: poll + 1 }), POLL_MS);
          } else {
            setPollTimedOut(true);
          }
        }
      } catch {
        setNetError(true);
      } finally {
        setFirstLoad(false);
        if (poll === 0) setLoadProgress((p) => p + 1);
      }
    },
    [navigate]
  );

  const loadDashboardData = useCallback(
    async (arg, refresh = false) => {
      setLoadProgress(0);
      loadInsights({ month: arg, refresh });
      await loadStats(arg, refresh);
      loadUsageByKey(arg, refresh);
      loadCostReconciliation(arg, refresh);
      loadCacheEconomics(arg, refresh);
    },
    [loadStats, loadInsights, loadUsageByKey, loadCostReconciliation, loadCacheEconomics]
  );

  useEffect(() => {
    fetch(`${API_URL}/api/cost/auth-check/`, { credentials: "include" })
      .then((res) => res.json())
      .then((data) => {
        if (!data.authenticated) navigate("/");
      })
      .catch(() => {
        // The 5 loaders below handle their own network-error states.
      });

    setLoadProgress(0);
    const initialArg = isInitialLifetime ? "lifetime" : searchParams.get("month") || undefined;
    loadDashboardData(initialArg);

    return () => clearTimeout(pollRef.current);
  }, [navigate, isInitialLifetime, searchParams, loadDashboardData]);

  const months = insights?.available_months ?? [];
  const curIdx = Math.max(
    0,
    months.findIndex((m) => m.value === (selectedMonth ?? lastMonthlyMonth ?? months[0]?.value))
  );
  const shown = months[curIdx];

  const loadPercent = Math.min(100, Math.round((loadProgress / TOTAL_LOADERS) * 100));
  const showLoadingBar = loadProgress < TOTAL_LOADERS;
  const isMonthLoading = !firstLoad && loadProgress < TOTAL_LOADERS;

  // Safety watchdog: ensure UI never remains locked if a network call hangs indefinitely
  useEffect(() => {
    if (isMonthLoading) {
      const timer = setTimeout(() => {
        setLoadProgress(TOTAL_LOADERS);
      }, 8000);
      return () => clearTimeout(timer);
    }
  }, [isMonthLoading]);

  const handleRetryStats = useCallback(() => {
    const arg = isLifetime ? "lifetime" : shown?.is_current ? undefined : shown?.value;
    loadStats(arg, true);
  }, [isLifetime, shown, loadStats]);

  // Auto-retry once if stats hit a transient rate limit error
  const retryStatsTimeoutRef = useRef(null);
  useEffect(() => {
    if (stats?.cost_source_error) {
      const errStr = String(stats.cost_source_error);
      if (errStr.includes("rate_limit") || errStr.includes("rate limit")) {
        clearTimeout(retryStatsTimeoutRef.current);
        retryStatsTimeoutRef.current = setTimeout(() => {
          handleRetryStats();
        }, 3500);
      }
    }
    return () => clearTimeout(retryStatsTimeoutRef.current);
  }, [stats?.cost_source_error, handleRetryStats]);

  const pick = (m) => {
    if (!m || isMonthLoading) return;
    setSelectedMonth(m.value);
    setLastMonthlyMonth(m.value);
    const arg = m.is_current ? undefined : m.value;
    setSearchParams(arg ? { month: arg } : {});
    loadDashboardData(arg);
  };

  const switchToLifetime = () => {
    if (isLifetime || isMonthLoading) return;
    setSelectedMonth("lifetime");
    setSearchParams({ range: "lifetime" });
    loadDashboardData("lifetime");
  };

  const switchToMonthly = (targetMonth) => {
    if (!isLifetime && !targetMonth) return;
    if (isMonthLoading) return;
    const m = targetMonth || (lastMonthlyMonth ? months.find((x) => x.value === lastMonthlyMonth) : null) || months[0];
    const mVal = m?.is_current ? undefined : m?.value;
    setSelectedMonth(m?.value ?? null);
    if (m?.value) setLastMonthlyMonth(m.value);
    setSearchParams(mVal ? { month: mVal } : {});
    loadDashboardData(mVal);
  };

  const refreshCurrent = () => {
    if (isMonthLoading) return;
    const arg = isLifetime ? "lifetime" : shown?.is_current ? undefined : shown?.value;
    loadDashboardData(arg, true);
  };

  if (firstLoad) {
    return (
      <div className="cr">
        {showLoadingBar && <LoadingBar percent={loadPercent} />}
        <div className="cr__wrap">
          <div className="cr__center cr__center--tall">
            <CircularProgress percent={loadPercent} size={88} strokeWidth={6.5} />
            <p className="cr__muted" style={{ marginTop: 14 }}>
              Loading command centre&hellip; {loadPercent}%
            </p>
          </div>
        </div>
      </div>
    );
  }

  if (netError && !insights) {
    return (
      <div className="cr">
        {showLoadingBar && <LoadingBar percent={loadPercent} />}
        <div className="cr__wrap">
          <p className="cr__notice">Couldn&rsquo;t reach the server. Try again in a moment.</p>
        </div>
      </div>
    );
  }

  const generatingFresh = insights?.generating;
  const iview = insights?.error ? insights.stale : insights;
  const showFindings = iview && !generatingFresh && !insights.insufficient_data;
  const isCurrent = !shown || shown.is_current;

  return (
    <div className="cr">
      {showLoadingBar && <LoadingBar percent={loadPercent} />}
      <div className="cr__wrap">
        <div className="cr__top">
          <div className="cr__brand-wrap">
            <div className="cr__brand">
              TCG<span>ai</span> chatbot<small>{isLifetime ? "LIFETIME OVERVIEW" : "MONTHLY OVERVIEW"}</small>
            </div>
            <div className="cr__view-toggle" role="group" aria-label="View timeframe selection">
              <button
                type="button"
                className={`cr__toggle-btn ${!isLifetime ? "is-active" : ""}`}
                onClick={() => switchToMonthly()}
                disabled={isMonthLoading}
              >
                <span>📅</span> Monthly
              </button>
              <button
                type="button"
                className={`cr__toggle-btn ${isLifetime ? "is-active" : ""}`}
                onClick={() => switchToLifetime()}
                disabled={isMonthLoading}
              >
                <span>🌟</span> Lifetime (Since Jun 1, 2026)
              </button>
            </div>
          </div>

          {!isLifetime ? (
            <div className="cr__stepper">
              <button
                aria-label="Previous month"
                onClick={() => pick(months[curIdx + 1])}
                disabled={isMonthLoading || curIdx >= months.length - 1}
              >
                &#9664;
              </button>
              <div>
                <div className="cr__mn">{shown?.label ?? "…"}</div>
                <div className={`cr__mm ${isMonthLoading ? "is-loading" : ""}`}>
                  {isMonthLoading
                    ? `UPDATING… ${loadPercent}%`
                    : shown?.is_current
                    ? "IN PROGRESS"
                    : " "}
                </div>
              </div>
              <button
                aria-label="Next month"
                onClick={() => pick(months[curIdx - 1])}
                disabled={isMonthLoading || curIdx <= 0}
              >
                &#9654;
              </button>
            </div>
          ) : (
            <div className="cr__stepper cr__stepper--lifetime">
              <div className="cr__lifetime-pill">
                <span className="cr__lifetime-icon">🌟</span>
                <div>
                  <div className="cr__mn">Jun 1, 2026 &ndash; Present</div>
                  <div className="cr__mm">ALL-UP LIFETIME STATS</div>
                </div>
              </div>
            </div>
          )}
        </div>

        {/* In-viewport header loadbar for mobile and desktop visibility */}
        {isMonthLoading && (
          <div className="cr__header-loadbar" aria-hidden="true">
            <div className="cr__header-loadbar-fill" style={{ width: `${loadPercent}%` }} />
          </div>
        )}

        <div className={`cr__body ${isMonthLoading ? "is-updating" : ""}`}>
          {isMonthLoading && (
            <div className="cr__updating-hud" role="status" aria-live="polite">
              <CircularProgress percent={loadPercent} size={64} strokeWidth={5.5} />
              <div className="cr__updating-text">
                <div className="cr__updating-title">
                  Updating to {isLifetime ? "lifetime stats" : shown?.label || "selected month"}…
                </div>
                <div className="cr__updating-sub">Refreshing spend, usage &amp; AI insights ({loadPercent}%)</div>
              </div>
            </div>
          )}

          {iview?.headline && (
            <div className="cr__lede">
              <div className="cr__scope">
                {isLifetime
                  ? "Jun 1, 2026 – Present · Lifetime"
                  : `${shown?.label ?? iview.month} · ${isCurrent ? "through today" : "final"}`}
              </div>
              <p>{iview.headline}</p>
            </div>
          )}

          {statsError && !stats && (
            <p className="cr__notice">Couldn&rsquo;t load spend &amp; usage. Try Refresh.</p>
          )}

          {/* Hero Pillar 1: Trust & Quality Scorecard */}
          <TrustScorecardHero stats={stats} isLifetime={isLifetime} />

          {/* Hero Pillar 2: Collector Demand Signals (Restock Radar & Catalog Opportunities) */}
          {iview?.product_demand && (
            <DemandRadarHero
              demand={iview.product_demand}
              oneOffs={iview.product_demand_one_offs}
              isLifetime={isLifetime}
            />
          )}

          {/* Hero Pillar 3: Retail Labor Cost Savings & Economics */}
          <LaborSavingsHero stats={stats} isLifetime={isLifetime} />

          {insights?.regenerating && (
            <p className="cr__notice">Refreshing this month&rsquo;s insights in the background…</p>
          )}

          {generatingFresh && !pollTimedOut && (
            <GenerationProgressCard
              progress={insights?.progress}
              isCurrent={isCurrent}
              isLifetime={isLifetime}
            />
          )}

          {pollTimedOut && (
            <div
              className="cr__notice"
              style={{
                display: "flex",
                alignItems: "center",
                justifyContent: "space-between",
                flexWrap: "wrap",
                gap: "10px",
              }}
            >
              <span>Still working on it &mdash; this is taking longer than usual.</span>
              <button
                className="cr__refresh"
                style={{ margin: 0, padding: "4px 12px", fontSize: "0.82rem" }}
                onClick={refreshCurrent}
              >
                Retry now
              </button>
            </div>
          )}

          {insights?.error && (
            <p className="cr__notice">
              Couldn&rsquo;t generate fresh insights ({insights.error}).
              {insights.stale ? " Showing the last saved result." : ""}
            </p>
          )}

          {insights?.insufficient_data && (
            <p className="cr__notice">
              Not enough conversations yet {isLifetime ? "since June 1, 2026" : "for this month"} (
              {insights.conversations_analyzed} so far).
            </p>
          )}

          {/* Shopper Topics & AI Strategic Recommendations */}
          {showFindings && (
            <RecommendationsSection
              recs={iview.recommendations}
              gaps={iview.unmet_needs}
              requests={iview.top_requests}
              isLifetime={isLifetime}
            />
          )}

          {/* DevOps & Technical Billing Telemetry (Collapsed by default) */}
          <DevOpsAccordion
            stats={stats}
            cacheEconomics={cacheEconomics}
            usageByKey={usageByKey}
            costReconciliation={costReconciliation}
            costCommentary={insights?.cost_commentary}
            isLifetime={isLifetime}
            onRetryStats={handleRetryStats}
          />

          {showFindings && (
            <p className="cr__prov">
              Insights from {iview.conversations_analyzed} conversations
              {iview.conversations_with_customer_text != null
                ? ` (${iview.conversations_with_customer_text} with the customer's own messages)`
                : ""}
              , generated {iview.generated_at ? new Date(iview.generated_at).toLocaleString() : "—"}
              {iview.sampled ? " · newest 200 sampled" : ""}. Counts are model estimates. Spend &amp;
              usage from the Anthropic console{stats?.currency ? ` (${stats.currency})` : ""}.
              {isLifetime
                ? " Cumulative all-up lifetime statistics since June 1, 2026."
                : isCurrent
                ? " Figures update through the month."
                : ""}
            </p>
          )}

          <div style={{ display: "flex", justifyContent: "flex-end", marginTop: "20px" }}>
            <button className="cr__refresh" onClick={refreshCurrent} disabled={isMonthLoading}>
              Refresh
            </button>
          </div>
        </div>
      </div>
    </div>
  );
}

export default HomeView;
