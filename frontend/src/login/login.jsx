import { useState, useEffect } from "react";
import "./login.css";
import { useNavigate } from "react-router-dom";

function Login() {
  const API_URL = import.meta.env.VITE_API_URL;

  const [username, setUsername] = useState("");
  const [password, setPassword] = useState("");
  const [loading, setLoading] = useState(false);

  const navigate = useNavigate();

  useEffect(() => {
    fetch(`${API_URL}/api/cost/auth-check/`, {
      credentials: "include",
    })
      .then((res) => res.json())
      .then((data) => {
        if (data.authenticated) {
          navigate("/home");
        }
      });
  }, [navigate]);

  const loginClicked = async () => {
    setLoading(true);

    try {
      const response = await fetch(
        `${API_URL}/api/cost/login/`,
        {
          method: "POST",
          credentials: "include",
          headers: {
            "Content-Type": "application/json",
          },
          body: JSON.stringify({
            username,
            password,
          }),
        }
      );

      if (response.ok) {
        navigate("/home");
      } else {
        alert("Invalid credentials");
      }
    } catch (err) {
      alert("Login failed");
    } finally {
      setLoading(false);
    }
  };

  return (
    <div className="login-screen">
      <div className="login-card">
        <div className="login-title">
          Welcome Back!
        </div>

        <div className="input-group">
          <label>Username</label>
          <input
            type="text"
            onChange={(e) =>
              setUsername(e.target.value)
            }
            placeholder="Enter username"
            disabled={loading}
          />
        </div>

        <div className="input-group">
          <label>Password</label>
          <input
            type="password"
            onChange={(e) =>
              setPassword(e.target.value)
            }
            placeholder="Enter password"
            disabled={loading}
          />
        </div>

        <button
          className="login-btn"
          onClick={loginClicked}
          disabled={loading}
        >
          {loading ? (
            <span className="login-spinner" />
          ) : (
            "Login"
          )}
        </button>
      </div>
    </div>
  );
}

export default Login;