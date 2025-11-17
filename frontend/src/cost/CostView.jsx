import './CostView.css'
import { useEffect, useState } from "react";

function CostView() {
  const [cost, setCost] = useState(null);

  useEffect(() => {
    fetch("http://127.0.0.1:8000/api/cost/get_cost/")
      .then((res) => res.json())
      .then((data) => setCost(data))
      .catch((err) => console.error("Error:", err));
    
    console.log(cost);
  }, []);


  return (
    <div>
      <h1>Cost</h1>
      <pre>{JSON.stringify(cost, null, 2)}</pre>
    </div>
  );
}

export default CostView
