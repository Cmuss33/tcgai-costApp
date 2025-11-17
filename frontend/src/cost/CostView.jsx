import "./CostView.css";
import { useEffect, useState } from "react";
import { LineChart, Line, XAxis, YAxis, Tooltip, CartesianGrid, ResponsiveContainer } from "recharts";
import CostModal from "./costModal/CostModal";

function CostView() {
  const [cost, setCost] = useState(null);

  useEffect(() => {
    fetch("http://127.0.0.1:8000/api/cost/get_cost/")
      .then((res) => res.json())
      .then((data) => setCost(data))
      .catch((err) => console.error("Error:", err));
  }, []);

  if (!cost) return <div>Loading...</div>;

  const october = cost.costs;

  return (
    <div className="cost-container">
      <h1>October Costs</h1>
      <ResponsiveContainer width="100%" height={300}>
        <LineChart data={october} margin={{ bottom: 80 }}>
          <CartesianGrid strokeDasharray="3 3" />
          <XAxis dataKey="day" interval={3} angle={-45} textAnchor="end" tickFormatter={(day) => {
            const [year, month, date] = day.split("-").map(Number);
            const jsDate = new Date(year, month - 1, date);
            return jsDate.toLocaleDateString('en-US', { month: 'long', day: 'numeric'});
          }}
          />
          <YAxis tickFormatter={(value) => `$${value}`} />
          <Tooltip content={<CostModal />} />
          <Line type="monotone" dataKey="total_cost" stroke="#8884d8" strokeWidth={3} />
        </LineChart>
      </ResponsiveContainer>

      <h3>Raw Data</h3>
      <pre>{JSON.stringify(october, null, 2)}</pre>
    </div>
  );
}

export default CostView
