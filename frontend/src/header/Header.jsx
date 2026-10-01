import { useState } from 'react';
import { NavLink, useNavigate } from 'react-router-dom';
import './Header.css';

function Header() {
  const navigate = useNavigate();
  const [loggingOut, setLoggingOut] = useState(false);
  const API_URL = import.meta.env.VITE_API_URL ?? "";

  const handleLogout = async () => {
    setLoggingOut(true);
    try {
      await fetch(`${API_URL}/api/cost/logout/`, {
        method: "POST",
        credentials: "include",
      });
    } catch (err) {
      console.error("Logout request failed:", err);
    } finally {
      navigate("/", { replace: true });
    }
  };

  return (
    <header className="header-container">
      <div className="header-content">
        <nav className="header-nav" aria-label="Main Navigation">
          <NavLink to="/home" className="header-btn">Overview</NavLink>
          <NavLink to="/chats" className="header-btn">Chat Summary</NavLink>
        </nav>
        <div className="header-actions">
          <button
            type="button"
            onClick={handleLogout}
            disabled={loggingOut}
            className="header-logout-btn"
            title="Log out of the application"
          >
            {loggingOut ? "Logging out…" : "Log out"}
          </button>
        </div>
      </div>
    </header>
  );
}

export default Header;