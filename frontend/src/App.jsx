import './App.css';
import { BrowserRouter as Router, Route, Routes, Navigate, useLocation } from 'react-router-dom';

import Header from './header/Header';
import ChatSummaryView from './chatSummary/ChatSummaryView';
import HomeView from './home/HomeView';
import Login from './login/login';

function Layout({ children }) {
  const location = useLocation();

  // hide header only on login page
  const hideHeader = location.pathname === "/";

  return (
    <>
      {!hideHeader && <Header />}
      {children}
    </>
  );
}

function AppRoutes() {
  return (
    <Routes>
      <Route path="/" element={<Login />} />
      <Route path="/home" element={<HomeView />} />
      <Route path="/cost" element={<Navigate to="/home" replace />} />
      <Route path="/chats" element={<ChatSummaryView />} />
    </Routes>
  );
}

function App() {
  return (
    <div className="content-container">
      <Router>
        <Layout>
          <AppRoutes />
        </Layout>
      </Router>
    </div>
  );
}

export default App;