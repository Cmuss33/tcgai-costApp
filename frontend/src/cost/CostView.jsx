import './CostView.css'
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


function CostView() {
  const [cost, setCost] = useState(null);

  useEffect(() => {
    fetch("http://127.0.0.1:8000/api/cost/get_cost/")
      .then((res) => res.json())
      .then((data) => setCost(data))
      .catch((err) => console.error("Error:", err));
  }, []);

  if (!cost) return <div>Loading...</div>;

  // Your costs now come from cost.costs
  const october = cost.costs || [];

  // Summary values
  const total = october.reduce((sum, d) => sum + d.total_cost, 0).toFixed(3);
  const lastDayCost = october[october.length - 1]?.total_cost?.toFixed(3) ?? "0.000";

  return (
    <div className="cost-container">
      <h1>October Costs</h1>

      {/* Summary Cards */}
      <div className="summary-grid">
        <div className="summary-card">
          Last Recorded Day: <strong>${lastDayCost}</strong>
        </div>
        <div className="summary-card">
          October Total: <strong>${total}</strong>
        </div>
        <div className="summary-card">
          Days Counted: <strong>{october.length}</strong>
        </div>
      </div>

      {/* Line Chart */}
      <div className="chart-box">
        <ResponsiveContainer width="100%" height={300}>
          <LineChart data={october}>
            <CartesianGrid strokeDasharray="3 3" />
            <XAxis dataKey="day" />
            <YAxis />
            <Tooltip />
            <Line
              type="monotone"
              dataKey="total_cost"
              stroke="#06007fff"
              strokeWidth={3}
            />
          </LineChart>
        </ResponsiveContainer>
      </div>

      {/* Raw JSON - TODO: DELETE - is for testing*/}
      <h3>Raw Data</h3>
      <pre>{JSON.stringify(october, null, 2)}</pre>
    </div>
  );
}

export default CostView;
