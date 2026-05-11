import { useState, useEffect } from "react";
import "./login.css";
import { useNavigate } from "react-router-dom";

function Login() {
  const API_URL = import.meta.env.VITE_API_URL;

  const [username, setUsername] = useState("");
  const [password, setPassword] = useState("");

  const navigate = useNavigate();

  useEffect(() => {
    fetch(`${API_URL}/api/cost/auth-check/`, {
      credentials: "include",
    })
      .then((res) => res.json())
      .then((data) => {
        if (data.authenticated) {
          navigate("/cost");
        }
      });
  }, [navigate]);

  const loginClicked = async () => {
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
      navigate("/cost");
    } else {
      alert("Invalid credentials");
    }
  };

  return (
    <div className="login-screen">
      <div className="login-card">
        <div className="login-title">
          Welcome Back
        </div>

        <div className="input-group">
          <label>Username</label>
          <input
            type="text"
            onChange={(e) =>
              setUsername(e.target.value)
            }
            placeholder="Enter username"
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
          />
        </div>

        <button
          className="login-btn"
          onClick={loginClicked}
        >
          Login
        </button>
      </div>
    </div>
  );
}

export default Login;