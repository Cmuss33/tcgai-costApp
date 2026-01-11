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

function CostView() {
  const [data, setData] = useState(null);
  const [monthOffset, setMonthOffset] = useState(0); // 0 = current month
  const [loading, setLoading] = useState(false);
  const [viewMode, setViewMode] = useState("cost"); // "cost" or "tokens"

  const today = new Date();

  const monthNames = [
    "January", "February", "March", "April", "May", "June",
    "July", "August", "September", "October", "November", "December"
  ];

  // Calculate current viewed month/year based on offset
  const viewedDate = new Date(today.getFullYear(), today.getMonth() + monthOffset);
  const viewedMonth = viewedDate.getMonth();
  const viewedYear = viewedDate.getFullYear();

  useEffect(() => {
    setLoading(true);
    setData(null);

    const endpoint =
      viewMode === "cost"
        ? `https://tcgai-costapp.onrender.com/api/cost/get_cost/?year=${viewedYear}&month=${viewedMonth + 1}`
        : `https://tcgai-costapp.onrender.com/api/cost/get_tokens/?year=${viewedYear}&month=${viewedMonth + 1}`;

    fetch(endpoint)
      .then((res) => res.json())
      .then((fetchedData) => {
        // If tokens, calculate total_tokens = input_tokens + output_tokens
        if (viewMode === "tokens" && fetchedData.tokens) {
          fetchedData.tokens = fetchedData.tokens.map(item => ({
            ...item,
            total_tokens: item.input_tokens + item.output_tokens
          }));
        }
        setData(fetchedData);
        setLoading(false);
      })
      .catch((err) => {
        console.error("Error:", err);
        setLoading(false);
      });
  }, [monthOffset, viewedMonth, viewedYear, viewMode]);

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
            <YAxis
              tickFormatter={(value) =>
                viewMode === "cost" ? `$${value}` : value
              }
            />
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

      <h3>Raw Data</h3>
      <pre>{JSON.stringify(chartData, null, 2)}</pre>
    </div>
  );
}

export default CostView;
