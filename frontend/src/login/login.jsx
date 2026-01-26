import { useState, useEffect } from 'react'
import './login.css'
import { useNavigate } from 'react-router-dom';

function Login() {
  const [username, setUsername] = useState('');
  const [password, setPassword] = useState('');

  const navigate = useNavigate();

  useEffect(() => {
    fetch("http://localhost:8000/api/cost/auth-check/", {
      credentials: "include",
      })
        .then(res => res.json())
        .then(data => {
          if (data.authenticated) {
            navigate("/cost");
          }
    });
  });

  const loginClicked = async () => {
    const response = await fetch("http://localhost:8000/api/cost/login/", {
      method: "POST",
      credentials: "include",
      headers: {
        "Content-Type": "application/json",
      },
      body: JSON.stringify({ username, password }),
    });

    if (response.ok) {
      navigate("/cost");
    } else {
      alert("Invalid credentials");
    }
  };

  const handleUsernameChange = (event) => {
    setUsername(event.target.value);
  }

  const handlePasswordChange = (event) => {
      setPassword(event.target.value);
  }

  return (
    <>
      <div className="login-screen">
        <div className="login-container">
          <div className="login-title">
            <div>Login</div>
          </div>
          <div className='input-header'>Username</div>
          <input type="text" className="input-field" onChange={handleUsernameChange} placeholder="Username"/>
          <div className='input-header'>Password</div>
          <input type="password" className="input-field" onChange={handlePasswordChange} placeholder="Password"/>
          <div className="login-btn-container">
            <button className="login-btn" onClick={loginClicked}>Login</button>
          </div>
        </div>
      </div>
    </>
  )
}

export default Login
