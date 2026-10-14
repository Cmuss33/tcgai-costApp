import { useEffect } from "react";
import { useNavigate } from "react-router-dom";
import AlertsSection from "./AlertsSection";
import "../home/HomeView.css";

const API_URL = import.meta.env.VITE_API_URL ?? "";

function AlertsView() {
  const navigate = useNavigate();

  useEffect(() => {
    fetch(`${API_URL}/api/cost/auth-check/`, { credentials: "include" })
      .then((res) => res.json())
      .then((data) => {
        if (!data.authenticated) navigate("/");
      })
      .catch(() => {
        // AlertsSection surfaces its own load errors.
      });
  }, [navigate]);

  return (
    <div className="cr">
      <div className="cr__wrap">
        <AlertsSection />
      </div>
    </div>
  );
}

export default AlertsView;
