import { useEffect, useState, useCallback, useRef } from "react";
import { useNavigate, Link } from "react-router-dom";
import "./HomeView.css";

const API_URL = import.meta.env.VITE_API_URL;
const POLL_MS = 4000;
const MAX_POLLS = 20;

const GAP_LABELS = { catalog: "catalog", policy: "policy", capability: "capability", other: "other" };
const STATUS_LABELS = { out_of_stock: "out of stock", not_carried: "not carried", unknown: "unknown" };
const PRIO_LABELS = { high: "High impact", medium: "Medium", low: "Low" };

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

function BarSeries({ values, color, w = 1000, h = 96 }) {
  if (!values || values.length === 0) return null;
  const max = Math.max(...values, 1);
  const hi = values.indexOf(Math.max(...values));
  const gap = 2;
  const bw = (w - gap * (values.length - 1)) / values.length;
  return (
    <svg viewBox={`0 0 ${w} ${h}`} width="100%" height={h}>
      <line className="cr-grid" x1="0" x2={w} y1={h * 0.5} y2={h * 0.5} />
      {values.map((v, i) => {
        const bh = Math.max(2, (v / max) * (h - 3));
        return (
          <rect
            key={i}
            x={i * (bw + gap)}
            y={h - bh}
            width={bw}
            height={bh}
            rx="2"
            fill={color}
            fillOpacity={i === hi ? 1 : 0.55}
          />
        );
      })}
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

/* ---------- KPI band + trend ---------- */
function StatsBand({ stats }) {
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
  const convSeries = (c.daily || []).map((d) => d.count);
  const tokSeries = (tk.daily || []).map((d) => d.input + d.output);

  return (
    <>
      {stats.cost_source_error && (
        <p className="cr__notice">
          Spend and token figures are unavailable right now ({stats.cost_source_error}).
          The rest of the page is current.
        </p>
      )}

      <div className="cr__kpis">
        <Kpi
          accent="--a-spend"
          label="Spend"
          value={fmtUsd(s.total)}
          sub={
            s.projected_month_end != null
              ? `proj. ${fmtUsd(s.projected_month_end)} by month-end`
              : `prev ${fmtUsd(s.prev_total)}`
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
          sub={`${c.per_day_avg} / day average`}
          deltaPct={c.delta_pct}
          betterWhen="up"
          spark={convSeries}
          sparkColor="#2fe0a6"
        />
        <Kpi
          accent="--a-cost"
          label="Cost / conversation"
          value={fmtUsd(pc.cost, true)}
          sub="spend ÷ conversations"
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
          sub={`in · ${fmtCompact(tk.output)} out · ${Math.round(pc.tokens_in || 0)}/chat`}
          deltaPct={tk.input_delta_pct}
          betterWhen="neutral"
          spark={tokSeries}
          sparkColor="#ff6ba0"
        />
      </div>

      {(spendSeries.length > 1 || convSeries.length > 0) && (
        <div className="cr__panel" style={{ "--accent": "var(--a-spend)" }}>
          <h2>This month at a glance</h2>
          <div className="cr__note">Daily spend and conversation volume on the same timeline.</div>
          {spendSeries.length > 1 && (
            <>
              <div className="cr__trend-sub" style={{ "--c": "var(--a-spend)" }}>
                Spend / day · {fmtUsd(s.total)} total
              </div>
              <AreaSpark values={spendSeries} color="#6a9bff" w={1000} h={88} />
            </>
          )}
          {convSeries.length > 0 && (
            <>
              <div className="cr__trend-sub" style={{ "--c": "var(--a-convo)" }}>
                Conversations / day · {fmtNum(c.total)} total
                {c.busiest ? ` · busiest ${c.busiest.day}` : ""}
              </div>
              <BarSeries values={convSeries} color="#2fe0a6" />
            </>
          )}
        </div>
      )}
    </>
  );
}

/* ---------- insight sections ---------- */
function Findings({ view }) {
  const requests = view.top_requests ?? [];
  const gaps = view.unmet_needs ?? [];
  const demand = view.product_demand ?? [];
  const recs = view.recommendations ?? [];
  const maxReq = Math.max(1, ...requests.map((r) => r.count || 0));

  return (
    <>
      <div className="cr__cols">
        {requests.length > 0 && (
          <div className="cr__panel" style={{ marginTop: 0, "--accent": "var(--a-convo)" }}>
            <h2>Top requests</h2>
            <div className="cr__note">What customers asked the bot for.</div>
            {requests.map((r) => (
              <div className="cr__bar-row" key={r.topic}>
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
        )}

        {gaps.length > 0 && (
          <div className="cr__panel" style={{ marginTop: 0, "--accent": "var(--a-tok)" }}>
            <h2>Where the bot fell short</h2>
            <div className="cr__note">Categories it couldn&rsquo;t handle.</div>
            {gaps.map((n) => (
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
        )}
      </div>

      {demand.length > 0 && (
        <div className="cr__panel" style={{ "--accent": "var(--a-cost)" }}>
          <h2>Want list</h2>
          <div className="cr__note">Products customers wanted that weren&rsquo;t available.</div>
          <table className="cr__want">
            <tbody>
              {demand.map((p) => (
                <tr key={p.product}>
                  <td className="cr__p">{p.product}</td>
                  <td className="cr__x">{p.count}&times;</td>
                  <td className={`cr__st ${p.status === "not_carried" ? "nc" : ""}`}>
                    {STATUS_LABELS[p.status] ?? p.status}
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
          {view.product_demand_one_offs > 0 && (
            <p className="cr__want-more">
              + {view.product_demand_one_offs} more products requested once each
            </p>
          )}
        </div>
      )}

      {recs.length > 0 && (
        <div className="cr__panel" style={{ "--accent": "var(--a-eval)" }}>
          <h2>What to fix next</h2>
          <div className="cr__note">Changes that would close this month&rsquo;s gaps, by impact.</div>
          {recs.map((r) => (
            <div className="cr__rec" key={r.title}>
              <div className="cr__rh">
                <span className={`cr__prio ${r.impact}`}>{PRIO_LABELS[r.impact] ?? r.impact}</span>
                <span className="cr__title">{r.title}</span>
                {r.effort && <span className="cr__effort">· {r.effort}</span>}
              </div>
              <p>{r.detail}</p>
              <div className="cr__foot">
                {r.addresses && <span className="cr__tie">addresses {r.addresses}</span>}
                {r.evidence_count != null && <span>{r.evidence_count} conversations</span>}
                <ExampleLinks ids={r.examples} />
              </div>
            </div>
          ))}
        </div>
      )}
    </>
  );
}

/* ---------- page ---------- */
function HomeView() {
  const navigate = useNavigate();
  const [stats, setStats] = useState(null);
  const [statsError, setStatsError] = useState(false);
  const [insights, setInsights] = useState(null);
  const [firstLoad, setFirstLoad] = useState(true);
  const [netError, setNetError] = useState(false);
  const [pollTimedOut, setPollTimedOut] = useState(false);
  const [selectedMonth, setSelectedMonth] = useState(null);
  const pollRef = useRef(null);

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
        if (data.month) setSelectedMonth(data.month);

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
      }
    },
    [navigate]
  );

  useEffect(() => {
    fetch(`${API_URL}/api/cost/auth-check/`, { credentials: "include" })
      .then((res) => res.json())
      .then((data) => {
        if (!data.authenticated) return navigate("/");
        loadStats();
        loadInsights();
      })
      .catch(() => {
        setNetError(true);
        setFirstLoad(false);
      });
    return () => clearTimeout(pollRef.current);
  }, [navigate, loadStats, loadInsights]);

  const months = insights?.available_months ?? [];
  const curIdx = Math.max(
    0,
    months.findIndex((m) => m.value === (selectedMonth ?? months[0]?.value))
  );
  const shown = months[curIdx];

  const pick = (m) => {
    if (!m) return;
    setSelectedMonth(m.value);
    const arg = m.is_current ? undefined : m.value;
    loadStats(arg);
    loadInsights({ month: arg });
  };
  const refreshCurrent = () => {
    const arg = shown?.is_current ? undefined : shown?.value;
    loadStats(arg, true);
    loadInsights({ month: arg, refresh: true });
  };

  if (firstLoad) {
    return (
      <div className="cr">
        <div className="cr__wrap">
          <div className="cr__center cr__center--tall">
            <div className="cr__spinner" />
          </div>
        </div>
      </div>
    );
  }

  if (netError && !insights) {
    return (
      <div className="cr">
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
      <div className="cr__wrap">
        <div className="cr__top">
          <div className="cr__brand">
            TCG<span>ai</span> chatbot<small>MONTHLY OVERVIEW</small>
          </div>
          <div className="cr__stepper">
            <button
              aria-label="Previous month"
              onClick={() => pick(months[curIdx + 1])}
              disabled={curIdx >= months.length - 1}
            >
              &#9664;
            </button>
            <div>
              <div className="cr__mn">{shown?.label ?? "…"}</div>
              <div className="cr__mm">{shown?.is_current ? "IN PROGRESS" : " "}</div>
            </div>
            <button
              aria-label="Next month"
              onClick={() => pick(months[curIdx - 1])}
              disabled={curIdx <= 0}
            >
              &#9654;
            </button>
          </div>
        </div>

        {iview?.headline && (
          <div className="cr__lede">
            <div className="cr__scope">
              {shown?.label ?? iview.month} &nbsp;·&nbsp; {isCurrent ? "through today" : "final"}
            </div>
            <p>{iview.headline}</p>
          </div>
        )}

        {statsError && !stats && (
          <p className="cr__notice">Couldn&rsquo;t load spend &amp; usage. Try Refresh.</p>
        )}
        <StatsBand stats={stats} />

        {insights?.regenerating && (
          <p className="cr__notice">Refreshing this month&rsquo;s insights in the background…</p>
        )}

        {generatingFresh && !pollTimedOut && (
          <div className="cr__center cr__center--tall">
            <div className="cr__spinner" />
            <p>Analyzing {isCurrent ? "this" : "that"} month&rsquo;s conversations…</p>
            <p className="cr__muted">This runs once per month and can take a minute.</p>
          </div>
        )}

        {pollTimedOut && (
          <p className="cr__notice">
            Still working on it — this is taking longer than usual. Use Refresh in a moment.
          </p>
        )}

        {insights?.error && (
          <p className="cr__notice">
            Couldn&rsquo;t generate fresh insights ({insights.error}).
            {insights.stale ? " Showing the last saved result." : ""}
          </p>
        )}

        {insights?.insufficient_data && (
          <p className="cr__notice">
            Not enough conversations yet for this month ({insights.conversations_analyzed} so far).
          </p>
        )}

        {showFindings && <Findings view={iview} />}

        {showFindings && (
          <p className="cr__prov">
            Insights from {iview.conversations_analyzed} conversations
            {iview.conversations_with_customer_text != null
              ? ` (${iview.conversations_with_customer_text} with the customer's own messages)`
              : ""}
            , generated {iview.generated_at ? new Date(iview.generated_at).toLocaleString() : "—"}
            {iview.sampled ? " · newest 200 sampled" : ""}. Counts are model estimates. Spend &amp;
            usage from the Anthropic console{stats?.currency ? ` (${stats.currency})` : ""}.
            {isCurrent ? " Figures update through the month." : ""}
          </p>
        )}

        {showFindings && (
          <button className="cr__refresh" onClick={refreshCurrent}>
            Refresh
          </button>
        )}
      </div>
    </div>
  );
}

export default HomeView;
