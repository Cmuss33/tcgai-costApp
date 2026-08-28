import { useEffect, useState, useCallback } from "react";
import { useNavigate, Link } from "react-router-dom";
import "./HomeView.css";

const API_URL = import.meta.env.VITE_API_URL;

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
    <span className="example-links">
      {ids.slice(0, 3).map((id, i) => (
        <Link key={id} to={`/chats?chat=${encodeURIComponent(id)}`} className="example-link">
          example {i + 1}
        </Link>
      ))}
    </span>
  );
}

function HomeView() {
  const navigate = useNavigate();
  const [payload, setPayload] = useState(null);
  const [loading, setLoading] = useState(true);
  const [netError, setNetError] = useState(false);
  const [selectedMonth, setSelectedMonth] = useState(null); // null = current month

  const load = useCallback(
    async ({ month, refresh } = {}) => {
      setLoading(true);
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
      } catch {
        setNetError(true);
      } finally {
        setLoading(false);
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
      .catch(() => setNetError(true));
  }, [navigate, load]);

  const months = payload?.available_months ?? [];
  const currentMonthValue = months.find((m) => m.is_current)?.value;
  const isCurrent = !selectedMonth || selectedMonth === currentMonthValue;

  const onPickMonth = (e) => {
    const value = e.target.value;
    setSelectedMonth(value);
    load({ month: value === currentMonthValue ? undefined : value });
  };

  return (
    <div className="home-container">
      <div className="home-toolbar">
        <h1>Chatbot insights</h1>
        <div className="home-controls">
          {months.length > 0 && (
            <select value={selectedMonth ?? currentMonthValue ?? ""} onChange={onPickMonth}>
              {months.map((m) => (
                <option key={m.value} value={m.value}>
                  {m.is_current ? `${m.label} (in progress)` : m.label}
                </option>
              ))}
            </select>
          )}
          {isCurrent && (
            <button
              className="home-refresh"
              onClick={() => load({ refresh: true })}
              disabled={loading}
            >
              Refresh
            </button>
          )}
        </div>
      </div>

      {loading && (
        <div className="loading-container">
          <div className="spinner" />
        </div>
      )}

      {!loading && netError && (
        <p className="home-notice">Couldn't reach the server. Try again in a moment.</p>
      )}

      {!loading && !netError && payload && (
        <>
          {payload.error && (
            <p className="home-notice">
              Couldn't generate insights ({payload.error}).
              {payload.stale ? " Showing the last saved result." : ""}
            </p>
          )}
          {payload.insufficient_data && (
            <p className="home-notice">
              Not enough conversations yet for this month
              ({payload.conversations_analyzed} so far).
            </p>
          )}

          {(payload.stale || (!payload.error && !payload.insufficient_data)) &&
            (() => {
              const view = payload.error ? payload.stale : payload;
              return (
                <>
                  {view.headline && <p className="home-headline">{view.headline}</p>}

                  <section className="home-section">
                    <h2>Top requests</h2>
                    {(view.top_requests ?? []).map((r) => (
                      <div className="rank-row" key={r.topic}>
                        <div className="rank-main">
                          <span className="rank-label">{r.topic}</span>
                          <span className="rank-count">
                            {r.count}
                            {r.share_pct != null ? ` · ${r.share_pct}%` : ""}
                          </span>
                        </div>
                        <div className="rank-bar-track">
                          <div
                            className="rank-bar-fill"
                            style={{ width: `${Math.min(100, r.share_pct ?? 0)}%` }}
                          />
                        </div>
                        <ExampleLinks ids={r.examples} />
                      </div>
                    ))}
                  </section>

                  <section className="home-section">
                    <h2>Where the bot falls short</h2>
                    <div className="card-grid">
                      {(view.unmet_needs ?? []).map((n) => (
                        <div className="gap-card" key={n.gap}>
                          <div className="gap-head">
                            <span className="gap-title">{n.gap}</span>
                            <span className="badge">{GAP_LABELS[n.gap_type] ?? n.gap_type}</span>
                          </div>
                          <div className="gap-count">{n.count} conversations</div>
                          <p className="gap-summary">{n.summary}</p>
                          <ExampleLinks ids={n.examples} />
                        </div>
                      ))}
                    </div>
                  </section>

                  <section className="home-section">
                    <h2>Unmet product demand</h2>
                    {(view.product_demand ?? []).map((p) => (
                      <div className="rank-row" key={p.product}>
                        <div className="rank-main">
                          <span className="rank-label">{p.product}</span>
                          <span className="rank-count">{p.count}</span>
                          <span className="badge">{STATUS_LABELS[p.status] ?? p.status}</span>
                        </div>
                        <ExampleLinks ids={p.examples} />
                      </div>
                    ))}
                  </section>

                  <p className="home-footer">
                    Based on {view.conversations_analyzed} conversations from {view.month}
                    {view.conversations_with_customer_text != null
                      ? ` (${view.conversations_with_customer_text} had the customer's own messages)`
                      : ""}
                    . Generated{" "}
                    {view.generated_at ? new Date(view.generated_at).toLocaleString() : "—"}.
                    {isCurrent ? " Updates through the month." : ""}
                    {view.sampled ? " Newest 200 conversations sampled." : ""}
                  </p>
                </>
              );
            })()}
        </>
      )}
    </div>
  );
}

export default HomeView;
