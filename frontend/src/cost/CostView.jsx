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
    fetch("http://127.0.0.1:8000/api/cost/get_cost/")
      .then((res) => res.json())
      .then((data) => setCost(data))
      .catch((err) => console.error("Error:", err));
  }, []);

  if (!cost) return <div>Loading...</div>;

  // Filter and sort data for the viewed month
  const filteredData = cost.costs
    .filter(item => {
      const [year, month, date] = item.day.split("-").map(Number);
      return year === viewedYear && month - 1 === viewedMonth;
    })
    .sort((a, b) => new Date(a.day) - new Date(b.day));

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

      <ResponsiveContainer width="100%" height={300}>
        <LineChart data={filteredData} margin={{ bottom: 80, left: 20, right: 20 }}>
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

      <h3>Raw Data</h3>
      <pre>{JSON.stringify(filteredData, null, 2)}</pre>
    </div>
  );
}

export default CostView;
