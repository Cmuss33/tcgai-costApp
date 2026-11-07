import { useEffect, useState } from "react";
import { Link, useNavigate, useSearchParams } from "react-router-dom";
import "../home/HomeView.css";
import "./RevenueOrdersView.css";

const API_URL = import.meta.env.VITE_API_URL ?? "";

const nf = new Intl.NumberFormat("en-US");
const fmtMoney = (n) => {
  if (n == null) return "—";
  if (n < 100) return `$${n.toFixed(n < 1 ? 3 : 2)}`;
  return `$${nf.format(Math.round(n))}`;
};

const MONTH_NAMES = [
  "January", "February", "March", "April", "May", "June",
  "July", "August", "September", "October", "November", "December",
];

function windowLabel(data) {
  if (!data) return "";
  if (data.is_lifetime) return "Lifetime to date — since June 1, 2026";
  const m = /^(\d{4})-(\d{2})$/.exec(data.month || "");
  if (m) return `${MONTH_NAMES[parseInt(m[2], 10) - 1]} ${m[1]}`;
  return data.month || "";
}

function fmtDate(iso) {
  if (!iso) return "—";
  const d = new Date(iso);
  if (Number.isNaN(d.getTime())) return "—";
  return d.toLocaleDateString("en-US", {
    year: "numeric",
    month: "short",
    day: "numeric",
  });
}

function shortChatId(chatId) {
  return chatId.length > 10 ? `${chatId.slice(0, 8)}…` : chatId;
}

/* Drill-down behind the Command Center's Chat-Influenced Revenue card:
   the orders in the same filtered set as the headline figure (see
   /api/cost/commercial_impact/orders/), each linking to its chat's page
   (/chats?chat=<id> -- the same deep link AOP's Command Center uses).
   Orders with no linked chat are a real state, shown as plain text. */
function RevenueOrdersView() {
  const navigate = useNavigate();
  const [searchParams] = useSearchParams();
  const [data, setData] = useState(null);
  const [error, setError] = useState(null);

  const month = searchParams.get("month");
  const shops = searchParams.getAll("shop");

  useEffect(() => {
    const params = new URLSearchParams();
    if (month) params.set("month", month);
    shops.forEach((s) => params.append("shop", s));
    const qs = params.toString();
    let cancelled = false;
    fetch(`${API_URL}/api/cost/commercial_impact/orders/${qs ? `?${qs}` : ""}`, {
      credentials: "include",
    })
      .then(async (res) => {
        if (res.status === 401 || res.status === 403) {
          navigate("/");
          return;
        }
        if (!res.ok) throw new Error(`orders ${res.status}`);
        const body = await res.json();
        if (!cancelled) setData(body);
      })
      .catch(() => {
        if (!cancelled) {
          setError(
            "Couldn't load the orders behind this figure. Head back to the Command Center and try again."
          );
        }
      });
    return () => {
      cancelled = true;
    };
    // shops is a fresh array per render; its contents are the dependency.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [month, shops.join("|"), navigate]);

  const backHref = month ? `/home?month=${encodeURIComponent(month)}` : "/home";

  return (
    <div className="cr">
      <div className="ro__wrap">
        <Link className="ro__back" to={backHref}>
          &larr; Command Center
        </Link>
        <div className="cr__section-eyebrow">
          <span>💰</span> Chat-influenced revenue
        </div>
        <h1 className="ro__title">{windowLabel(data) || "Orders"}</h1>

        {!data && !error && <p className="ro__note">Loading orders…</p>}
        {error && <p className="ro__note">{error}</p>}

        {data && (
          <>
            <div className="ro__total">
              {fmtMoney(data.influenced_revenue)}
              <span>
                {" "}
                from {data.orders_count} order{data.orders_count === 1 ? "" : "s"}
                {data.mixed_currencies ? ` (${data.currency}, mixed currencies)` : ""}
              </span>
            </div>

            {data.unlinked_orders > 0 && (
              <p className="ro__note">
                {data.unlinked_orders} of these orders ha
                {data.unlinked_orders === 1 ? "s" : "ve"} no linked chat — the
                revenue still counts, but there is no conversation to open for{" "}
                {data.unlinked_orders === 1 ? "it" : "them"}.
              </p>
            )}

            {data.orders.length === 0 ? (
              <p className="ro__empty">
                No attributed orders in this window yet. When a chat leads to a
                purchase, the order — and a link to its chat — will appear here.
              </p>
            ) : (
              <table className="ro__table">
                <thead>
                  <tr>
                    <th>Order</th>
                    <th>Date</th>
                    <th className="ro__num">Revenue</th>
                    <th>Chat</th>
                  </tr>
                </thead>
                <tbody>
                  {data.orders.map((row) => (
                    <tr key={`${row.shop}:${row.order_id}`}>
                      <td>
                        <span className="ro__order">{row.order_id}</span>
                        {row.shop && <span className="ro__shop">{row.shop}</span>}
                      </td>
                      <td>{fmtDate(row.order_date)}</td>
                      <td className="ro__num">
                        {fmtMoney(row.influenced_revenue)}
                        {data.mixed_currencies ? ` ${row.currency}` : ""}
                      </td>
                      <td>
                        {row.linked ? (
                          <Link
                            className="ro__chatlink"
                            to={`/chats?chat=${encodeURIComponent(row.chat_id)}`}
                            title={row.chat_id}
                          >
                            {shortChatId(row.chat_id)} →
                          </Link>
                        ) : (
                          <span className="ro__nolink">No linked chat</span>
                        )}
                      </td>
                    </tr>
                  ))}
                </tbody>
              </table>
            )}

            <p className="ro__foot">
              Same set as the headline figure: orders linked to test (probe)
              chats are excluded; orders with no linked chat are included.
            </p>
          </>
        )}
      </div>
    </div>
  );
}

export default RevenueOrdersView;
