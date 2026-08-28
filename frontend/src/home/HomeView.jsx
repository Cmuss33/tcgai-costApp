import { useEffect, useState, useCallback, useRef } from "react";
import { useNavigate, Link } from "react-router-dom";
import "./HomeView.css";

const API_URL = import.meta.env.VITE_API_URL;
const POLL_MS = 4000;

const GAP_LABELS = {
  catalog: "catalog gap",
  policy: "policy gap",
  capability: "can't do it",
  other: "other",
};

const STATUS_LABELS = {
  out_of_stock: "out of stock",
  not_carried: "not carried",
  unknown: "unknown",
};

function ExampleLinks({ ids }) {
  if (!ids || ids.length === 0) return null;
  return (
    <div className="ex">
      {ids.slice(0, 3).map((id, i) => (
        <Link key={id} to={`/chats?chat=${encodeURIComponent(id)}`} className="ex__link">
          conversation {i + 1}
        </Link>
      ))}
    </div>
  );
}

function Findings({ view, isCurrent }) {
  const requests = view.top_requests ?? [];
  const gaps = view.unmet_needs ?? [];
  const demand = view.product_demand ?? [];
  const maxReqCount = Math.max(1, ...requests.map((r) => r.count || 0));

  return (
    <>
      {view.headline && <p className="insights__headline">{view.headline}</p>}

      {requests.length > 0 && (
        <section className="insights__section">
          <h2>Top requests</h2>
          <ol className="reqs">
            {requests.map((r) => (
              <li className="req" key={r.topic}>
                <div className="req__row">
                  <span className="req__name">{r.topic}</span>
                  <span className="req__count">
                    {r.count}
                    {r.share_pct != null && <span className="req__share"> · {r.share_pct}%</span>}
                  </span>
                </div>
                <div className="req__track">
                  <div
                    className="req__fill"
                    style={{ width: `${Math.round(((r.count || 0) / maxReqCount) * 100)}%` }}
                  />
                </div>
                <ExampleLinks ids={r.examples} />
              </li>
            ))}
          </ol>
        </section>
      )}

      {gaps.length > 0 && (
        <section className="insights__section">
          <h2>Where the bot falls short</h2>
          <div className="gaps">
            {gaps.map((n) => (
              <article className="gap" data-type={n.gap_type} key={n.gap}>
                <div className="gap__head">
                  <h3 className="gap__title">{n.gap}</h3>
                  <span className="gap__badge">{GAP_LABELS[n.gap_type] ?? n.gap_type}</span>
                </div>
                <div className="gap__meta">{n.count} conversations</div>
                <p className="gap__text">{n.summary}</p>
                <ExampleLinks ids={n.examples} />
              </article>
            ))}
          </div>
        </section>
      )}

      {demand.length > 0 && (
        <section className="insights__section">
          <h2>Unmet product demand</h2>
          <ul className="demand">
            {demand.map((p) => (
              <li className="demand__row" key={p.product}>
                <span className="demand__name">{p.product}</span>
                <span className="demand__right">
                  <span className="demand__count">{p.count}&times;</span>
                  <span className="demand__badge" data-status={p.status}>
                    {STATUS_LABELS[p.status] ?? p.status}
                  </span>
                </span>
                <ExampleLinks ids={p.examples} />
              </li>
            ))}
          </ul>
          {view.product_demand_one_offs > 0 && (
            <p className="demand__more">
              + {view.product_demand_one_offs} more products requested once each
            </p>
          )}
        </section>
      )}

      <p className="insights__footer">
        Based on {view.conversations_analyzed} conversations from {view.month}
        {view.conversations_with_customer_text != null
          ? ` — ${view.conversations_with_customer_text} included the customer's own messages`
          : ""}
        . Generated {view.generated_at ? new Date(view.generated_at).toLocaleString() : "—"}.
        {isCurrent ? " Updates through the month." : ""}
        {view.sampled ? " Newest 200 conversations sampled." : ""}
      </p>
    </>
  );
}

function HomeView() {
  const navigate = useNavigate();
  const [payload, setPayload] = useState(null);
  const [firstLoad, setFirstLoad] = useState(true);
  const [netError, setNetError] = useState(false);
  const [selectedMonth, setSelectedMonth] = useState(null);
  const pollRef = useRef(null);

  const load = useCallback(
    async ({ month, refresh } = {}) => {
      setNetError(false);
      try {
        const params = new URLSearchParams();
        if (month) params.set("month", month);
        if (refresh) params.set("refresh", "1");
        const qs = params.toString();
        const res = await fetch(
          `${API_URL}/api/cost/insights_summary/${qs ? `?${qs}` : ""}`,
          { credentials: "include" }
        );
        if (res.status === 401 || res.status === 403) {
          navigate("/");
          return;
        }
        const data = await res.json();
        setPayload(data);
        if (data.month) setSelectedMonth(data.month);

        clearTimeout(pollRef.current);
        if (data.generating || data.regenerating) {
          pollRef.current = setTimeout(() => load({ month }), POLL_MS);
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
        if (!data.authenticated) navigate("/");
        else load();
      })
      .catch(() => {
        setNetError(true);
        setFirstLoad(false);
      });
    return () => clearTimeout(pollRef.current);
  }, [navigate, load]);

  const months = payload?.available_months ?? [];
  const currentMonthValue = months.find((m) => m.is_current)?.value;
  const isCurrent = !selectedMonth || selectedMonth === currentMonthValue;

  const onPickMonth = (e) => {
    const value = e.target.value;
    setSelectedMonth(value);
    load({ month: value === currentMonthValue ? undefined : value });
  };

  if (firstLoad) {
    return (
      <div className="insights">
        <div className="insights__center">
          <div className="spinner" />
        </div>
      </div>
    );
  }

  if (netError) {
    return (
      <div className="insights">
        <p className="insights__notice">Couldn&rsquo;t reach the server. Try again in a moment.</p>
      </div>
    );
  }

  const generatingFresh = payload?.generating;
  const view = payload?.error ? payload.stale : payload;
  const showFindings =
    view && !generatingFresh && !payload.insufficient_data;

  return (
    <div className="insights">
      <header className="insights__bar">
        <h1>Chatbot insights</h1>
        <div className="insights__controls">
          {payload?.regenerating && <span className="insights__chip">refreshing&hellip;</span>}
          {months.length > 0 && (
            <select
              className="insights__select"
              value={selectedMonth ?? currentMonthValue ?? ""}
              onChange={onPickMonth}
            >
              {months.map((m) => (
                <option key={m.value} value={m.value}>
                  {m.is_current ? `${m.label} — in progress` : m.label}
                </option>
              ))}
            </select>
          )}
          {isCurrent && (
            <button
              className="insights__refresh"
              onClick={() => load({ refresh: true })}
              disabled={!!payload?.regenerating || !!generatingFresh}
            >
              Refresh
            </button>
          )}
        </div>
      </header>

      {generatingFresh && (
        <div className="insights__center insights__center--tall">
          <div className="spinner" />
          <p>Analyzing this month&rsquo;s conversations&hellip;</p>
          <p className="insights__muted">This runs once a month and can take a minute.</p>
        </div>
      )}

      {payload?.error && (
        <p className="insights__notice">
          Couldn&rsquo;t generate fresh insights ({payload.error}).
          {payload.stale ? " Showing the last saved result." : ""}
        </p>
      )}

      {payload?.insufficient_data && (
        <p className="insights__notice">
          Not enough conversations yet for this month ({payload.conversations_analyzed} so far).
        </p>
      )}

      {showFindings && <Findings view={view} isCurrent={isCurrent} />}
    </div>
  );
}

export default HomeView;
