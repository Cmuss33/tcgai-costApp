import "./CostView.css";
import { useEffect, useState } from "react";
import {
  LineChart,
  Line,
  XAxis,
  YAxis,
  Tooltip,
  CartesianGrid,
  ResponsiveContainer
} from "recharts";
import CostModal from "./costModal/CostModal";
import { useNavigate } from 'react-router-dom';

function CostView() {
  const API_URL = import.meta.env.VITE_API_URL;

  const costPerInput = 1 / 1000000;
  const costPerOutput = 5 / 1000000;
  const [data, setData] = useState(null);
  const [monthOffset, setMonthOffset] = useState(0);
  const [loading, setLoading] = useState(false);
  const [viewMode, setViewMode] = useState("cost");

  // Analytics state
  const [analytics, setAnalytics] = useState({
    avg_eval_score: null,
    avg_tokens_in: null,
    avg_tokens_out: null,
    avg_conversations_per_day: null,
    avg_cost: null
  });
  const [loadingAnalytics, setLoadingAnalytics] = useState(false);
  const [analyticsPeriod, setAnalyticsPeriod] = useState("30_days"); // "daily", "7_days", "30_days"

  const navigate = useNavigate();
  const today = new Date();

  const monthNames = [
    "January", "February", "March", "April", "May", "June",
    "July", "August", "September", "October", "November", "December"
  ];

  const viewedDate = new Date(today.getFullYear(), today.getMonth() + monthOffset);
  const viewedMonth = viewedDate.getMonth();
  const viewedYear = viewedDate.getFullYear();

  // Fetch chart data
  useEffect(() => {
    fetch(`${API_URL}/api/cost/auth-check/`, { credentials: "include" })
      .then(res => res.json())
      .then(data => {
        if (!data.authenticated) navigate("/");
      });

    setLoading(true);
    setData(null);

    const endpoint =
      viewMode === "cost"
        ? `${API_URL}/api/cost/get_cost/?year=${viewedYear}&month=${viewedMonth + 1}`
        : `${API_URL}/api/cost/get_tokens/?year=${viewedYear}&month=${viewedMonth + 1}`;

    fetch(endpoint)
      .then(res => res.json())
      .then(fetchedData => {
        if (viewMode === "tokens" && fetchedData.tokens) {
          fetchedData.tokens = fetchedData.tokens.map(item => ({
            ...item,
            total_tokens: item.input_tokens + item.output_tokens
          }));
        }
        setData(fetchedData);
        setLoading(false);
      })
      .catch(err => {
        console.error("Error fetching chart data:", err);
        setLoading(false);
      });
  }, [monthOffset, viewedMonth, viewedYear, viewMode]);

  // Fetch analytics for selected period
  useEffect(() => {
    const fetchAnalytics = async () => {
      setLoadingAnalytics(true);
      try {
        const [
          avgEvalRes,
          avgInRes,
          avgOutRes,
          avgConvRes
        ] = await Promise.all([
          fetch(`${API_URL}/api/cost/get_avg_eval_score/?period=${analyticsPeriod}`, { credentials: "include" }),
          fetch(`${API_URL}/api/cost/get_avg_tokens_in/?period=${analyticsPeriod}`, { credentials: "include" }),
          fetch(`${API_URL}/api/cost/get_avg_tokens_out/?period=${analyticsPeriod}`, { credentials: "include" }),
          fetch(`${API_URL}/api/cost/get_avg_conversations_per_day/?period=${analyticsPeriod}`, { credentials: "include" })
        ]);

        const [avgEvalData, avgInData, avgOutData, avgConvData] = await Promise.all([
          avgEvalRes.json(),
          avgInRes.json(),
          avgOutRes.json(),
          avgConvRes.json()
        ]);

        setAnalytics({
          avg_eval_score: avgEvalData.average_eval_score,
          avg_tokens_in: avgInData.average_tokens_in,
          avg_tokens_out: avgOutData.average_tokens_out,
          avg_conversations_per_day: avgConvData.average_conversations_per_day,
          avg_cost: avgInData.average_tokens_in * costPerInput + avgOutData.average_tokens_out * costPerOutput
        });
      } catch (err) {
        console.error("Error fetching analytics:", err);
      } finally {
        setLoadingAnalytics(false);
      }
    };

    fetchAnalytics();
  }, [analyticsPeriod, API_URL]);

  let chartData = [];
  if (data) {
    chartData = viewMode === "cost" ? data.costs || [] : data.tokens || [];
  }

  return (
    <div className="cost-container">
      {/* Header with toggle */}
      <div className="header-toggle">
        <h1>
          <span
            className={`toggle-option ${viewMode === "cost" ? "active" : ""}`}
            onClick={() => setViewMode("cost")}
          >
            Costs
          </span>
          {" | "}
          <span
            className={`toggle-option ${viewMode === "tokens" ? "active" : ""}`}
            onClick={() => setViewMode("tokens")}
          >
            Tokens
          </span>
        </h1>
      </div>

      {/* Month Navigation */}
      <div className="month-navigation">
        <button onClick={() => setMonthOffset(monthOffset - 1)}>&larr;</button>
        <span className="current-month">
          {monthNames[viewedMonth]} {viewedYear}
        </span>
        <button onClick={() => setMonthOffset(monthOffset + 1)}>&rarr;</button>
      </div>

      {loading && (
        <div className="loading-container">
          <div className="spinner"></div>
        </div>
      )}

      {!loading && chartData.length > 0 && (
        <ResponsiveContainer width="100%" height={300}>
          <LineChart data={chartData} margin={{ bottom: 80, left: 20, right: 20 }}>
            <CartesianGrid strokeDasharray="3 3" />
            <XAxis
              dataKey="day"
              interval={0}
              angle={-45}
              textAnchor="end"
              tickFormatter={(day) => {
                const [year, month, date] = day.split("-").map(Number);
                const jsDate = new Date(year, month - 1, date);
                return jsDate.toLocaleDateString("en-US", { month: "short", day: "numeric" });
              }}
            />
            <YAxis tickFormatter={(value) => viewMode === "cost" ? `$${value}` : value} />
            <Tooltip content={<CostModal viewMode={viewMode} />} />
            <Line
              type="monotone"
              dataKey={viewMode === "cost" ? "total_cost" : "total_tokens"}
              stroke="#8884d8"
              strokeWidth={3}
            />
          </LineChart>
        </ResponsiveContainer>
      )}

      <h2 className="total-cost-header">Total Month's Cost: ${chartData?.reduce((sum, item) => sum + (item.total_cost ?? 0), 0)?.toFixed(2) ?? "0.00"}</h2>

      {/* Analytics Section */}
      <div className="analytics-section">
        <h2 className="analytics-header">Current Month's Analytics</h2>

        {/* Period selector */}
        <div className="analytics-period-selector">
          <button className={analyticsPeriod === "30_days" ? "active" : ""} onClick={() => setAnalyticsPeriod("30_days")}>Last 30 Days</button>
          <button className={analyticsPeriod === "7_days" ? "active" : ""} onClick={() => setAnalyticsPeriod("7_days")}>Last 7 Days</button>
          <button className={analyticsPeriod === "daily" ? "active" : ""} onClick={() => setAnalyticsPeriod("daily")}>Today</button>
        </div>

        {loadingAnalytics ? (
          <div className="loading-container">
            <div className="spinner"></div>
          </div>
        ) : (
          <div className="analytics-grid">
            <div>
              <strong>Avg Eval Score:</strong>
              <br />
              {analytics.avg_eval_score}
            </div>
            <div>
              <strong>Avg Tokens In / Conversation:</strong>
              <br />
              {analytics.avg_tokens_in}
            </div>
            <div>
              <strong>Avg Tokens Out / Conversation:</strong>
              <br />
              {analytics.avg_tokens_out}
            </div>
            <div>
              <strong>Avg Conversations / Day:</strong>
              <br />
              {analytics.avg_conversations_per_day != null ? analytics.avg_cost : 0}
            </div>
            <div>
              <strong>Avg Cost / Conversation:</strong>
              <br />
              ${analytics.avg_cost != null ? analytics.avg_cost.toPrecision(2) : "0.00"}
            </div>
          </div>
        )}
      </div>
    </div>
  );
}

export default CostView;