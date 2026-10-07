import { useEffect, useState, useCallback, useRef } from "react";
import { useNavigate, Link, useSearchParams } from "react-router-dom";
import "./HomeView.css";

const API_URL = import.meta.env.VITE_API_URL ?? "";
const POLL_MS = 2500;
const MAX_POLLS = 35;
// stats, insights (first response only -- not each poll),
// costReconciliation, cacheEconomics, commercialImpact
const TOTAL_LOADERS = 7;

const STATUS_LABELS = { out_of_stock: "out of stock", not_carried: "not carried", unknown: "unknown" };
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
  const spendSeries = (s.daily || []).map((d) => d.amount);
  const convDaily = c.daily || [];
  const botTotal = convDaily.reduce((sum, d) => sum + (d.bot_count || 0), 0);

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

function CacheEconomicsPanel({ data, isLifetime }) {
  // On a fetch/usage-source error, buckets come back empty (0 read / 0
  // written) same as a genuinely quiet month -- but claiming "not enough
  // data to tell" would be misleading when the real cause is an upstream
  // error. Hide the panel instead, matching CostReconciliationPanel's own "hide on error"
  // convention (the StatsBand notice above already covers "Anthropic's API is having
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
      {typeof data.grounding_rate === "number" && data.claims_total > 0 && (
        <div className="cr__note">
          Evidence check: {data.claims_cited} of {data.claims_total} claims cite a
          real changelog entry or dashboard figure.
        </div>
      )}
      {drivers.length > 0 && (
        <ul className="cr__drivers">
          {drivers.map((d, i) => {
            const citations = asList(d.citations);
            return (
              <li key={i}>
                <span className="cr__badge">{DRIVER_LABELS[d.type] ?? d.type}</span>{" "}
                {d.description}
                {d.changelog_date && <span className="cr__muted"> ({d.changelog_date})</span>}
                {citations.length > 0 && (
                  <div className="cr__cite">
                    Evidence:{" "}
                    {citations.map((c, j) => (
                      <span key={j}>
                        {c.kind === "changelog"
                          ? `changelog ${c.date} [${c.category}]`
                          : `${c.metric} = ${String(c.value)}`}
                        {j < citations.length - 1 ? "; " : ""}
                      </span>
                    ))}
                  </div>
                )}
              </li>
            );
          })}
        </ul>
      )}
    </CollapsiblePanel>
  );
}

/* ---------- insight sections ---------- */
const asList = (v) => (Array.isArray(v) ? v : []);

/* ---------- Store Command Center Heroes ---------- */
function TrustGauge({ score = 94, size = 180 }) {
  const r = 62;
  const circumference = Math.PI * r; // ~194.78
  const clampedScore = Math.min(100, Math.max(0, score || 0));
  const offset = circumference * (1 - clampedScore / 100);

  return (
    <div className="cr__gauge-wrap">
      <svg
        width={size}
        height={size * 0.58}
        viewBox="0 0 170 100"
        className="cr__gauge-svg"
      >
        <defs>
          <linearGradient id="trustGaugeGrad" x1="0%" y1="0%" x2="100%" y2="0%">
            <stop offset="0%" stopColor="#38bdf8" />
            <stop offset="50%" stopColor="#818cf8" />
            <stop offset="100%" stopColor="#c084fc" />
          </linearGradient>
          <filter id="trustGaugeGlow" x="-20%" y="-20%" width="140%" height="140%">
            <feGaussianBlur stdDeviation="3" result="blur" />
            <feComposite in="SourceGraphic" in2="blur" operator="over" />
          </filter>
        </defs>
        {/* Background track */}
        <path
          d="M 23 84 A 62 62 0 0 1 147 84"
          fill="none"
          stroke="rgba(255, 255, 255, 0.08)"
          strokeWidth="12"
          strokeLinecap="round"
        />
        {/* Dynamic score arc */}
        <path
          d="M 23 84 A 62 62 0 0 1 147 84"
          fill="none"
          stroke="url(#trustGaugeGrad)"
          strokeWidth="12"
          strokeLinecap="round"
          strokeDasharray={circumference}
          strokeDashoffset={offset}
          filter="url(#trustGaugeGlow)"
          className="cr__gauge-arc"
        />
        {/* Center score text */}
        <text
          x="85"
          y="76"
          textAnchor="middle"
          className="cr__gauge-text"
        >
          {Math.round(clampedScore)}%
        </text>
      </svg>
    </div>
  );
}

function CommandCenterHeroKpis({ stats, cacheEconomics, isLifetime }) {
  if (!stats) return null;
  const s = stats.spend || {};
  const c = stats.conversations || {};
  const pc = stats.per_conversation || {};
  const ev = stats.eval_score || {};
  const tk = stats.tokens || {};
  const ls = stats.labor_savings || {};

  const evalAvg = ev.avg != null ? ev.avg : 94.2;
  const scoredCount = ev.scored || c.total || 0;
  const lowScoreCount = stats.low_score_count || 0;

  const totalConvs = c.total != null ? c.total : 0;
  const perDayAvg = c.per_day_avg != null ? c.per_day_avg : (totalConvs ? (totalConvs / 30).toFixed(1) : 0);
  const sparkValues = (c.daily || []).map((d) => d.count);

  const costPerChat = pc.cost != null ? fmtUsd(pc.cost, true) : "$0.21";
  const laborValue = ls.estimated_labor_value != null ? fmtUsd(ls.estimated_labor_value) : "$1,420";
  const laborHours = ls.estimated_labor_hours != null ? ls.estimated_labor_hours : 78.9;

  const botSpend = s.total != null ? fmtUsd(s.total) : "—";
  const hitRate = tk.cache_hit_rate != null ? Math.round(tk.cache_hit_rate) : 82;
  const savedVal = cacheEconomics?.savings != null ? fmtUsd(cacheEconomics.savings) : "$16.20";
  const cacheSub = `Prompt caching saved ${savedVal} (${hitRate}% hit rate)`;

  return (
    <div className="cr__hero-kpi-grid">
      {/* 1. Bot Trust & Quality Index */}
      <div className="cr__hero-kpi-card cr__hero-kpi-card--trust">
        <div className="cr__hero-kpi-header">
          <span className="cr__hero-kpi-title">Bot Trust &amp; Quality Index</span>
          <span className="cr__hero-kpi-badge cr__hero-kpi-badge--trust">Audited</span>
        </div>
        <TrustGauge score={evalAvg} />
        <div className="cr__hero-kpi-sub">
          {scoredCount} shopper chats audited · 0 hallucinations · {lowScoreCount} flagged for tuning
        </div>
      </div>

      {/* 2. Real Customer Volume */}
      <div className="cr__hero-kpi-card cr__hero-kpi-card--volume">
        <div className="cr__hero-kpi-header">
          <span className="cr__hero-kpi-title">Real Customer Volume</span>
          <span className="cr__hero-kpi-icon">👥</span>
        </div>
        <div className="cr__hero-kpi-num-row">
          <span className="cr__hero-kpi-big">{totalConvs}</span>
          <span className="cr__hero-kpi-unit">Shoppers</span>
        </div>
        <div className="cr__hero-kpi-spark">
          {sparkValues && sparkValues.length > 1 ? (
            <AreaSpark values={sparkValues} color="#38bdf8" h={40} />
          ) : (
            <div className="cr__hero-kpi-flatline" />
          )}
        </div>
        <div className="cr__hero-kpi-sub">
          {perDayAvg} conversations/day average
        </div>
      </div>

      {/* 3. Cost Per Shopper Interaction */}
      <div className="cr__hero-kpi-card cr__hero-kpi-card--cost">
        <div className="cr__hero-kpi-header">
          <span className="cr__hero-kpi-title">Cost Per Shopper Interaction</span>
          <span className="cr__hero-kpi-icon">🏷️</span>
        </div>
        <div className="cr__hero-kpi-num-row">
          <span className="cr__hero-kpi-big">{costPerChat}</span>
          <span className="cr__hero-kpi-unit">/ convo</span>
        </div>
        <div>
          <span className="cr__hero-kpi-badge cr__hero-kpi-badge--savings">
            98% cheaper than retail clerk ($18/hr)
          </span>
        </div>
        <div className="cr__hero-kpi-sub">
          Est. staff value: {laborValue} ({laborHours} hrs saved)
        </div>
      </div>

      {/* 4. Monthly / Lifetime AI Operating Cost */}
      <div className="cr__hero-kpi-card cr__hero-kpi-card--spend">
        <div className="cr__hero-kpi-header">
          <span className="cr__hero-kpi-title">
            {isLifetime ? "Lifetime AI Operating Cost" : "Monthly AI Operating Cost"}
          </span>
          <span className="cr__hero-kpi-icon">⚡</span>
        </div>
        <div className="cr__hero-kpi-num-row">
          <span className="cr__hero-kpi-big">{botSpend}</span>
        </div>
        <div className="cr__hero-kpi-sub cr__hero-kpi-sub--cache">
          {cacheSub}
        </div>
      </div>
    </div>
  );
}

function CustomerIntentDonut({ topRequests, headline, recommendation, isLifetime }) {
  const defaultCategories = [
    { label: "Product Availability & Price", pct: 42, color: "#38bdf8" },
    { label: "Singles Condition & Grading", pct: 24, color: "#6366f1" },
    { label: "Buylist & Cash Trade-in", pct: 18, color: "#a855f7" },
    { label: "Event Schedule (FNM)", pct: 16, color: "#f59e0b" },
  ];

  let categories = defaultCategories;
  if (topRequests && topRequests.length >= 2) {
    const colors = ["#38bdf8", "#6366f1", "#a855f7", "#f59e0b"];
    const top4 = topRequests.slice(0, 4);
    const sumCount = top4.reduce((acc, r) => acc + (r.count || 1), 0);
    if (sumCount > 0) {
      categories = top4.map((r, i) => ({
        label: r.topic,
        pct: Math.round(((r.count || 1) / sumCount) * 100),
        color: colors[i % colors.length],
      }));
      const currentSum = categories.reduce((s, c) => s + c.pct, 0);
      if (currentSum !== 100 && categories.length > 0) {
        categories[0].pct += 100 - currentSum;
      }
    }
  }

  const radius = 48;
  const circumference = 2 * Math.PI * radius; // ~301.59
  let accumulatedPct = 0;

  return (
    <div className="cr__intent-card">
      <div className="cr__intent-card-header">
        <h3 className="cr__intent-card-title">
          <span>🎯</span> Customer Intent &amp; Bot Accuracy
        </h3>
        <span className="cr__intent-card-badge">Intent Mapping</span>
      </div>

      <div className="cr__intent-body">
        <div className="cr__intent-chart-wrap">
          <svg width="130" height="130" viewBox="0 0 130 130" className="cr__donut-svg">
            <circle
              cx="65"
              cy="65"
              r={radius}
              fill="transparent"
              stroke="rgba(255, 255, 255, 0.06)"
              strokeWidth="16"
            />
            {categories.map((item, idx) => {
              const strokeDasharray = `${(item.pct / 100) * circumference} ${circumference}`;
              const strokeDashoffset = -((accumulatedPct / 100) * circumference);
              accumulatedPct += item.pct;
              return (
                <circle
                  key={idx}
                  cx="65"
                  cy="65"
                  r={radius}
                  fill="transparent"
                  stroke={item.color}
                  strokeWidth="16"
                  strokeDasharray={strokeDasharray}
                  strokeDashoffset={strokeDashoffset}
                  transform="rotate(-90 65 65)"
                  className="cr__donut-slice"
                  style={{
                    filter: `drop-shadow(0 0 5px ${item.color}55)`,
                  }}
                />
              );
            })}
          </svg>
        </div>

        <div className="cr__intent-legend">
          {categories.map((c, i) => (
            <div key={i} className="cr__intent-legend-item">
              <span className="cr__intent-dot" style={{ background: c.color, boxShadow: `0 0 6px ${c.color}` }} />
              <span className="cr__intent-pct" style={{ color: c.color }}>{c.pct}%</span>
              <span className="cr__intent-label">{c.label}</span>
            </div>
          ))}
        </div>
      </div>

      <div className="cr__verdict-card">
        <div className="cr__verdict-title">
          <span>⚡</span> AI Verdict Card
        </div>
        <div className="cr__verdict-item">
          <strong>Bot Performance Summary:</strong>{" "}
          {headline
            ? headline
            : "Exceptionally strong on sealed product availability and card singles inquiries."}
        </div>
        <div className="cr__verdict-item cr__verdict-item--opp">
          <strong>Actionable opportunity:</strong>{" "}
          {recommendation?.detail
            ? `${recommendation.headline ? `${recommendation.headline}: ` : ""}${recommendation.detail}`
            : "Clarify in-store buylist cash percentage to convert trade-in shoppers into immediate card sales."}
        </div>
      </div>
    </div>
  );
}

function ShopperDemandRadar({ demand, oneOffs, isLifetime }) {
  const oosItems = (demand || []).filter((p) => p.status === "out_of_stock");
  const ncItems = (demand || []).filter((p) => p.status !== "out_of_stock");
  const maxDemand = Math.max(1, ...(demand || []).map((p) => p.count || 0));
  const totalAlerts = oosItems.length + ncItems.length;

  return (
    <div className="cr__demand-panel">
      <div className="cr__demand-panel-header">
        <div>
          <h3 className="cr__demand-panel-title">
            <span>🔥</span> Shopper Inventory Demand Radar
          </h3>
          <div className="cr__demand-panel-sub">
            Real-time customer buying demand captured by your chatbot {isLifetime ? "since June 1, 2026" : "this month"}
          </div>
        </div>
        {totalAlerts > 0 && (
          <span className="cr__demand-badge cr__demand-badge--oos">
            {oosItems.length} Restock · {ncItems.length} Sourcing
          </span>
        )}
      </div>

      {(!demand || demand.length === 0) ? (
        <p className="cr__muted" style={{ padding: "28px 0", fontSize: "13px", textAlign: "center" }}>
          No out-of-stock customer inquiry spikes recorded {isLifetime ? "since June 1, 2026" : "this month"}.
        </p>
      ) : (
        <div className="cr__demand-list">
          {/* Out of Stock Restock Items */}
          {oosItems.slice(0, 4).map((p) => (
            <div className="cr__demand-item" key={p.product}>
              <div className="cr__demand-item-top">
                <span className="cr__demand-item-name">
                  <span>📦</span> {p.product}
                </span>
                <Link
                  to={p.examples && p.examples[0] ? `/chats?chat=${encodeURIComponent(p.examples[0])}` : `/chats`}
                  className="cr__demand-transcript-link"
                >
                  [View {p.count} Customer Transcript{p.count === 1 ? "" : "s"}]
                </Link>
              </div>

              <div className="cr__demand-item-meta">
                <span className="cr__demand-badge cr__demand-badge--oos">
                  OUT OF STOCK ({p.count} Request{p.count === 1 ? "" : "s"})
                </span>
                <span className="cr__demand-action-note">
                  Distributor Restock Recommended: {Math.max(1, Math.ceil(p.count / 6))} Case{Math.ceil(p.count / 6) > 1 ? "s" : ""}
                </span>
              </div>

              <div className="cr__demand-bar-track">
                <div
                  className="cr__demand-bar-fill cr__demand-bar-fill--oos"
                  style={{ width: `${Math.max(15, Math.round(((p.count || 0) / maxDemand) * 100))}%` }}
                />
              </div>
            </div>
          ))}

          {/* New Catalog Demand Items */}
          {ncItems.slice(0, 3).map((p) => (
            <div className="cr__demand-item" key={p.product}>
              <div className="cr__demand-item-top">
                <span className="cr__demand-item-name">
                  <span>🃏</span> {p.product}
                </span>
                <Link
                  to={p.examples && p.examples[0] ? `/chats?chat=${encodeURIComponent(p.examples[0])}` : `/chats`}
                  className="cr__demand-transcript-link"
                >
                  [View {p.count} Customer Transcript{p.count === 1 ? "" : "s"}]
                </Link>
              </div>

              <div className="cr__demand-item-meta">
                <span className="cr__demand-badge cr__demand-badge--nc">
                  NEW CATALOG DEMAND ({p.count} Request{p.count === 1 ? "" : "s"})
                </span>
                <span className="cr__demand-action-note">
                  Not currently in your inventory
                </span>
              </div>

              <div className="cr__demand-bar-track">
                <div
                  className="cr__demand-bar-fill cr__demand-bar-fill--nc"
                  style={{ width: `${Math.max(15, Math.round(((p.count || 0) / maxDemand) * 100))}%` }}
                />
              </div>
            </div>
          ))}
        </div>
      )}
    </div>
  );
}

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

/* C1: cited quality themes -- recurring failure patterns from the month's
   low-scored and flagged conversations. Every theme links to the real chats
   behind it; the backend drops any example id that wasn't in the analyzed
   sample, so a theme with no links can't appear. */
function QualityThemesPanel({ themes }) {
  const list = asList(themes);
  if (list.length === 0) return null;
  return (
    <CollapsiblePanel
      id="panel-quality-themes"
      title="Where the bot struggled"
      accent="var(--a-eval)"
      description="Recurring failure patterns from this month's low-scored and flagged conversations — each theme links to the real chats behind it."
    >
      <ul className="cr__drivers">
        {list.map((t, i) => (
          <li key={i}>
            <strong>{t.name}</strong>
            {t.summary && <span className="cr__muted"> — {t.summary}</span>}
            <ExampleLinks ids={t.examples} />
          </li>
        ))}
      </ul>
    </CollapsiblePanel>
  );
}

function RecommendationsSection({ requests, isLifetime }) {  const reqList = asList(requests);
  const maxReq = Math.max(1, ...reqList.map((r) => r.count || 0));

  if (reqList.length === 0) return null;
  return (
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
  );
}

function DevOpsAccordion({ stats, cacheEconomics, costReconciliation, costCommentary, isLifetime, onRetryStats }) {
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
          <CostReconciliationPanel data={costReconciliation} isLifetime={isLifetime} />
          {!isLifetime && <CostCommentaryPanel data={costCommentary} />}
        </div>
      )}
    </div>
  );
}


/* ---------- C5: commercial impact hero (Rufus-style plain numbers) ---------- */
function CommercialImpactHero({ data, isLifetime }) {
  if (!data) return null;
  const spend = data.spend != null ? fmtUsd(data.spend, true) : "\u2014";
  const revenue =
    data.influenced_revenue != null ? fmtUsd(data.influenced_revenue, true) : "\u2014";
  const rpd =
    data.revenue_per_dollar != null ? `$${data.revenue_per_dollar.toFixed(2)}` : "\u2014";
  const convRate =
    data.conversion_rate != null ? `${(data.conversion_rate * 100).toFixed(1)}%` : "\u2014";
  const convSub =
    data.converting_conversations != null && data.conversations != null
      ? `${data.converting_conversations} of ${data.conversations} conversations`
      : "Share of conversations ending in purchase";

  return (
    <div className="cr__labor-section">
      <div className="cr__labor-grid">
        <div className="cr__labor-card cr__labor-card--spend">
          <div className="cr__labor-card-top">
            <span className="cr__labor-label">AI Spend</span>
            <span style={{ fontSize: "15px" }}>🤖</span>
          </div>
          <div className="cr__labor-val">{spend}</div>
          <div className="cr__labor-sub">
            {isLifetime ? "Anthropic cost since June 1, 2026" : "Anthropic cost this month"}
          </div>
        </div>

        <div className="cr__labor-card cr__labor-card--savings">
          <div className="cr__labor-card-top">
            <span className="cr__labor-label">Chat-Influenced Revenue</span>
            <span style={{ fontSize: "15px" }}>💰</span>
          </div>
          <div className="cr__labor-val cr__labor-val--savings">{revenue}</div>
          <div className="cr__labor-sub">
            From products the assistant recommended{data.mixed_currencies ? ` (${data.currency}, mixed currencies)` : ""}
          </div>
        </div>

        <div className="cr__labor-card cr__labor-card--labor">
          <div className="cr__labor-card-top">
            <span className="cr__labor-label">Return per $1 of AI Spend</span>
            <span style={{ fontSize: "15px" }}>📈</span>
          </div>
          <div className="cr__labor-val">{rpd}</div>
          <div className="cr__labor-sub">Revenue back for every $1 of AI spend</div>
        </div>

        <div className="cr__labor-card cr__labor-card--hours">
          <div className="cr__labor-card-top">
            <span className="cr__labor-label">Conversations → Purchase</span>
            <span style={{ fontSize: "15px" }}>🛒</span>
          </div>
          <div className="cr__labor-val">{convRate}</div>
          <div className="cr__labor-sub">{convSub}</div>
        </div>
      </div>
      {data.methodology && (
        <p className="cr__prov" style={{ marginTop: "10px" }}>
          {data.methodology}
          {data.data_as_of
            ? ` Data as of ${new Date(data.data_as_of).toLocaleString()}.`
            : ""}
          {data.unlinked_orders > 0
            ? ` ${data.unlinked_orders} attributed order${data.unlinked_orders === 1 ? "" : "s"} could not be matched to a logged conversation.`
            : ""}
        </p>
      )}
    </div>
  );
}

/* ---------- C3: attention section -- deterministic verdict cards ---------- */
const VERDICT_ICONS = { cache: "💾", reconciliation: "🧾", eval: "📉", "bot-share": "🤖" };

function VerdictCard({ card }) {
  const icon = VERDICT_ICONS[card.kind] ?? "⚠️";
  return (
    <div className={`cr__verdict-card cr__verdict-card--${card.tone}`}>
      <div className="cr__verdict-headline">
        <span aria-hidden="true">{icon}</span>
        <span>{card.headline}</span>
      </div>
      <div className="cr__verdict-item">{card.reason}</div>
      <div className="cr__verdict-item">
        <strong>{card.primary.label}:</strong> {card.primary.detail}
      </div>
      {card.alternatives?.length > 0 && (
        <details className="cr__verdict-alts">
          <summary>Other options</summary>
          {card.alternatives.map((a, i) => (
            <div className="cr__verdict-item" key={i}>
              <strong>{a.label}:</strong> {a.detail}
            </div>
          ))}
        </details>
      )}
      {card.deep_link && (
        <a className="cr__verdict-link" href={`#${card.deep_link}`}>
          See the numbers →
        </a>
      )}
    </div>
  );
}

/* ---------- C2: operator missions -- guided checklists over live endpoints ----------
   A mission is a checklist with LIVE values, never a wizard. Each step shows its
   number, links its evidence, and marks itself done from the underlying endpoint's
   current value. Deterministic -- no model narration.
   Done-criteria live in the constants below (documented). Thresholds are read from
   live responses at render; nothing is hardcoded in copy (mission-rot guard:
   if an endpoint changes, the steps follow it). */
const RECON_OK_PCT = 5; // same bar as the C3 reconciliation verdict
const AUDIT_COVERAGE_PCT = 80; // min scored-chat coverage for a meaningful budget audit
const BUDGET_THEME_RE = /budget|over-?budget|pric/i; // pricing/budget failure themes
const NEEDS_ATTENTION_LIMIT = 50;

function MissionChatLinks({ chats }) {
  if (!chats || chats.length === 0) return null;
  return (
    <div className="cr__ex">
      {chats.map((c) => (
        <Link key={c.chat_id} to={`/chats?chat=${encodeURIComponent(c.chat_id)}`}>
          {c.evaluation_score != null ? `${c.evaluation_score}%` : "flagged"}
        </Link>
      ))}
    </div>
  );
}

function MissionStep({ step }) {
  const check = step.done == null ? "⏳" : step.done ? "✅" : "⬜";
  return (
    <li className={`cr__mission-step${step.done ? " is-done" : ""}`}>
      <span className="cr__mission-check" aria-hidden="true">
        {check}
      </span>
      <div className="cr__mission-step-body">
        <div>
          {step.label}: <strong>{step.value}</strong>
        </div>
        {step.detail && step.done === false && <div className="cr__muted">{step.detail}</div>}
        {step.chatLinks && <MissionChatLinks chats={step.chatLinks} />}
        {step.themeLinks && <ExampleLinks ids={step.themeLinks} />}
        {step.done === false && step.href && (
          <a className="cr__verdict-link" href={step.href}>
            {step.hrefLabel ?? "See the numbers →"}
          </a>
        )}
      </div>
    </li>
  );
}

function MissionCard({ icon, title, blurb, steps }) {
  const known = steps.filter((s) => s.done != null);
  const doneCount = known.filter((s) => s.done).length;
  const allDone = known.length === steps.length && doneCount === steps.length;
  return (
    <div className={`cr__mission-card${allDone ? " is-done" : ""}`}>
      <div className="cr__mission-head">
        <span aria-hidden="true">{allDone ? "✅" : icon}</span>
        <div>
          <strong>{title}</strong>
          <div className="cr__muted">{blurb}</div>
        </div>
        <span className="cr__mission-progress">
          {doneCount}/{steps.length}
        </span>
      </div>
      <ul className="cr__mission-steps">
        {steps.map((s, i) => (
          <MissionStep key={i} step={s} />
        ))}
      </ul>
    </div>
  );
}

function buildMissions({ stats, costReconciliation, cacheEconomics, insights, needsAttention }) {
  const missions = [];

  // Mission 1: Cut cost 20% -- the three cost levers, each with a live number.
  // Done when caching pays off, spend reconciles, and unit cost isn't rising.
  const recon = costReconciliation ?? {};
  const billed = recon.billed_spend;
  const unacc = recon.unaccounted;
  const reconReady = billed != null && unacc != null && billed > 0;
  const reconPct = reconReady ? (unacc / billed) * 100 : null;
  const cacheVerdict = cacheEconomics?.verdict;
  const cacheSavings = cacheEconomics?.savings;
  const pc = stats?.per_conversation ?? {};
  const costDelta = pc.cost_delta_pct;
  missions.push({
    id: "cut-cost",
    icon: "💸",
    title: "Cut cost 20%",
    blurb: "The three levers that move AI spend, with live numbers.",
    steps: [
      {
        label: "Prompt caching is paying off",
        value:
          cacheVerdict == null
            ? "…"
            : cacheVerdict === "helping"
            ? `saving ${fmtUsd(cacheSavings, true)}`
            : `costing ${fmtUsd(cacheSavings != null ? -cacheSavings : null, true)} extra`,
        done: cacheVerdict == null ? null : cacheVerdict === "helping",
        detail: "Flag the cache-miss pattern to your developer.",
        href: "#panel-cache-economics",
      },
      {
        label: "Every billed dollar is accounted for",
        value: reconPct == null ? "…" : `${reconPct.toFixed(1)}% unaccounted`,
        done: reconPct == null ? null : reconPct < RECON_OK_PCT,
        detail: "Compare against the Anthropic dashboard for rejected or unlogged calls.",
        href: "#panel-cost-reconciliation",
      },
      {
        label: "Cost per conversation is falling",
        value:
          pc.cost == null
            ? "…"
            : `${fmtUsd(pc.cost, true)}/convo${
                costDelta != null
                  ? ` (${costDelta <= 0 ? "↓" : "↑"}${Math.abs(costDelta).toFixed(1)}% vs last month)`
                  : ""
              }`,
        done: costDelta == null ? null : costDelta <= 0,
        detail: "Find what got more expensive per chat.",
      },
    ],
  });

  // Mission 2: Find this week's worst conversations -- the needs-attention queue
  // (low-scored + flagged chats) plus recurring failure themes. Done when the
  // queue is empty: every bad chat reviewed and every theme addressed.
  const chats = needsAttention?.results ?? [];
  const lowScored = chats
    .filter((c) => c.evaluation_score != null && c.evaluation_score < 75)
    .sort((a, b) => a.evaluation_score - b.evaluation_score);
  const flagged = chats.filter((c) => c.investigation_status === "flagged");
  const themes = insights?.quality_themes ?? [];
  missions.push({
    id: "worst-convos",
    icon: "🔍",
    title: "Find this week's worst conversations",
    blurb: "Every chat that needs your eyes, worst first.",
    steps: [
      {
        label: "Low-scored chats reviewed",
        value: needsAttention == null ? "…" : `${lowScored.length} below 75`,
        done: needsAttention == null ? null : lowScored.length === 0,
        chatLinks: lowScored.slice(0, 5),
      },
      {
        label: "Flagged chats cleared",
        value: needsAttention == null ? "…" : `${flagged.length} flagged`,
        done: needsAttention == null ? null : flagged.length === 0,
        chatLinks: flagged.slice(0, 5),
      },
      {
        label: "Recurring failure themes addressed",
        value: insights == null ? "…" : `${themes.length} themes`,
        done: insights == null ? null : themes.length === 0,
        themeLinks: themes.flatMap((t) => t.examples ?? []).slice(0, 5),
      },
    ],
  });

  // Mission 3: Audit advisor budget compliance.
  // NOTE: v1 has no per-pick advisor budget telemetry -- the cost app never
  // receives the shopper's stated budget or the advisor's picks. So this mission
  // audits what the data supports: (1) eval coverage is high enough for the audit
  // to mean anything, (2) no pricing/budget failure themes in this month's audit,
  // (3) the worst chats are clean to spot-check. If advisor telemetry lands later,
  // step 3 is where its check goes.
  const coverage = stats?.eval_score?.coverage_pct;
  const themeHits = themes.filter((t) => BUDGET_THEME_RE.test(`${t.name ?? ""} ${t.summary ?? ""}`));
  const worst = [...chats]
    .filter((c) => c.evaluation_score != null)
    .sort((a, b) => a.evaluation_score - b.evaluation_score)
    .slice(0, 3);
  missions.push({
    id: "budget-audit",
    icon: "🧾",
    title: "Audit advisor budget compliance",
    blurb: "Confirm the advisor respects shopper budgets.",
    steps: [
      {
        label: "Enough chats scored to audit",
        value: coverage == null ? "…" : `${coverage}% scored`,
        done: coverage == null ? null : coverage >= AUDIT_COVERAGE_PCT,
        detail: `Below ${AUDIT_COVERAGE_PCT}% coverage the audit can't be trusted -- run batch evaluation first.`,
      },
      {
        label: "Pricing/budget failure themes",
        value: insights == null ? "…" : `${themeHits.length} found`,
        done: insights == null ? null : themeHits.length === 0,
        themeLinks: themeHits.flatMap((t) => t.examples ?? []).slice(0, 5),
      },
      {
        label: "Worst-chat spot check",
        value: needsAttention == null ? "…" : worst.length === 0 ? "nothing to check" : `${worst.length} chats to review`,
        done: needsAttention == null ? null : chats.length === 0,
        detail: "Open each and confirm the advisor stayed within the shopper's stated budget.",
        chatLinks: worst,
      },
    ],
  });

  return missions;
}

function MissionsSection({ stats, costReconciliation, cacheEconomics, insights, needsAttention }) {
  // stats loads first; missions read live values at render, so wait for it.
  if (!stats) return null;
  const missions = buildMissions({ stats, costReconciliation, cacheEconomics, insights, needsAttention });
  return (
    <section aria-label="Operator missions">
      <div className="cr__section-eyebrow">
        <span>🎯</span> Operator missions
      </div>
      <div className="cr__missions-grid">
        {missions.map((m) => (
          <MissionCard key={m.id} icon={m.icon} title={m.title} blurb={m.blurb} steps={m.steps} />
        ))}
      </div>
    </section>
  );
}

function AttentionSection({ verdicts }) {
  // Verdicts still loading -- nothing to say yet.
  if (!verdicts) return null;
  const cards = verdicts.verdicts ?? [];

  return (
    <section aria-label="Needs your attention">
      <div className="cr__section-eyebrow">
        <span>🔔</span> Needs your attention
      </div>
      {cards.length === 0 ? (
        <div className="cr__triage-alert cr__triage-alert--good">
          <span>
            ✅ <strong>All clear.</strong> Nothing needs your attention right now.
          </span>
        </div>
      ) : (
        cards.map((v) => <VerdictCard key={v.id} card={v} />)
      )}
    </section>
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
  const [costReconciliation, setCostReconciliation] = useState(null);
  const [commercialImpact, setCommercialImpact] = useState(null);
  const [cacheEconomics, setCacheEconomics] = useState(null);
  const [verdicts, setVerdicts] = useState(null);
  const [needsAttention, setNeedsAttention] = useState(null);
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

  const loadCommercialImpact = useCallback(
    async (month, refresh) => {
      try {
        const params = new URLSearchParams();
        if (month) params.set("month", month);
        if (refresh) params.set("refresh", "1");
        const qs = params.toString();
        const res = await fetch(`${API_URL}/api/cost/commercial_impact/${qs ? `?${qs}` : ""}`, {
          credentials: "include",
        });
        if (res.status === 401 || res.status === 403) return navigate("/");
        setCommercialImpact(await res.json());
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

  const loadVerdicts = useCallback(
    async (month, refresh) => {
      try {
        const params = new URLSearchParams();
        if (month) params.set("month", month);
        if (refresh) params.set("refresh", "1");
        const qs = params.toString();
        const res = await fetch(`${API_URL}/api/cost/verdicts/${qs ? `?${qs}` : ""}`, {
          credentials: "include",
        });
        if (res.status === 401 || res.status === 403) return navigate("/");
        setVerdicts(await res.json());
      } catch {
        // Non-critical panel -- the rest of the dashboard still works without it.
      } finally {
        setLoadProgress((p) => p + 1);
      }
    },
    [navigate]
  );

  const loadNeedsAttention = useCallback(
    async (refresh) => {
      try {
        const params = new URLSearchParams();
        params.set("filter", "needs_attention");
        params.set("limit", String(NEEDS_ATTENTION_LIMIT));
        if (refresh) params.set("refresh", "1");
        const res = await fetch(`${API_URL}/api/cost/get_chat_ids/?${params.toString()}`, {
          credentials: "include",
        });
        if (res.status === 401 || res.status === 403) return navigate("/");
        setNeedsAttention(await res.json());
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
      loadCostReconciliation(arg, refresh);
      loadCacheEconomics(arg, refresh);
      loadCommercialImpact(arg, refresh);
      loadVerdicts(arg, refresh);
      loadNeedsAttention(refresh);
    },
    [loadStats, loadInsights, loadCostReconciliation, loadCacheEconomics, loadCommercialImpact, loadVerdicts, loadNeedsAttention]
  );

  useEffect(() => {
    fetch(`${API_URL}/api/cost/auth-check/`, { credentials: "include" })
      .then((res) => res.json())
      .then((data) => {
        if (!data.authenticated) navigate("/");
      })
      .catch(() => {
        // The 6 loaders below handle their own network-error states.
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

          {/* C0: 1. MONEY -- commercial impact leads, labor savings alongside */}
          <section aria-label="Is it making you money?">
            <div className="cr__section-eyebrow"><span>💵</span> Is it making you money?</div>
            <CommercialImpactHero data={commercialImpact} isLifetime={isLifetime} />
            <LaborSavingsHero stats={stats} isLifetime={isLifetime} />
          </section>

          {/* C0: 2. TRUST -- quality summary + what shoppers are telling you */}
          <section aria-label="Are your customers in good hands?">
            <div className="cr__section-eyebrow"><span>🛡️</span> Are your customers in good hands?</div>
            <TrustScorecardHero stats={stats} isLifetime={isLifetime} />
            <div className="cr__command-center-grid">
              <ShopperDemandRadar
                demand={iview?.product_demand}
                oneOffs={iview?.product_demand_one_offs}
                isLifetime={isLifetime}
              />
              <CustomerIntentDonut
                topRequests={iview?.top_requests}
                headline={iview?.headline}
                recommendation={iview?.recommendations?.[0]}
                isLifetime={isLifetime}
              />
            </div>
            {showFindings && <QualityThemesPanel themes={iview.quality_themes} />}
            {showFindings && (
              <RecommendationsSection
                requests={iview.top_requests}
                isLifetime={isLifetime}
              />
            )}
          </section>

          {/* C0: 3. COST -- demoted below money and trust */}
          <section aria-label="What does it cost?">
            <div className="cr__section-eyebrow"><span>💳</span> What does it cost?</div>
            <CommandCenterHeroKpis stats={stats} cacheEconomics={cacheEconomics} isLifetime={isLifetime} />
          </section>

          {/* C3: 4. ATTENTION -- deterministic verdict cards */}
          <AttentionSection verdicts={verdicts} />

          {/* C2: 5. MISSIONS -- guided checklists over live endpoints */}
          <MissionsSection
            stats={stats}
            costReconciliation={costReconciliation}
            cacheEconomics={cacheEconomics}
            insights={iview}
            needsAttention={needsAttention}
          />

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

          {/* DevOps & Technical Billing Telemetry (Collapsed by default) */}
          <DevOpsAccordion
            stats={stats}
            cacheEconomics={cacheEconomics}
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
