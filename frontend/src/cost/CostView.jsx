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
  const [cost, setCost] = useState(null);
  const [monthOffset, setMonthOffset] = useState(0); // 0 = current month
  const [loading, setLoading] = useState(false);

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
    // Call backend with year and month query parameters
    fetch(`http://127.0.0.1:8000/api/cost/get_cost/?year=${viewedYear}&month=${viewedMonth + 1}`)
      .then((res) => res.json())
      .then((data) => {
        setCost(data);
        setLoading(false);
      })
      .catch((err) => {
        console.error("Error:", err);
        setLoading(false);
      });
  }, [monthOffset, viewedMonth, viewedYear]);

  return (
    <div className="cost-container">
      <h1>Costs</h1>

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
          Loading...
        </div>
      )}

      {!loading && cost && cost.costs && (
        <ResponsiveContainer width="100%" height={300}>
          <LineChart data={cost.costs} margin={{ bottom: 80, left: 20, right: 20 }}>
            <CartesianGrid strokeDasharray="3 3" />
            <XAxis
              dataKey="day"
              interval={0}
              angle={-45}
              textAnchor="end"
              tickFormatter={(day) => {
                const [year, month, date] = day.split("-").map(Number);
                const jsDate = new Date(year, month - 1, date);
                return jsDate.toLocaleDateString("en-US", { month: "long", day: "numeric" });
              }}
            />
            <YAxis tickFormatter={(value) => `$${value}`} />
            <Tooltip content={<CostModal />} />
            <Line type="monotone" dataKey="total_cost" stroke="#8884d8" strokeWidth={3} />
          </LineChart>
        </ResponsiveContainer>
      )}

      <h3>Raw Data</h3>
      <pre>{JSON.stringify(cost?.costs, null, 2)}</pre>
    </div>
  );
}

export default CostView;
