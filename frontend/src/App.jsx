import './App.css'
import { BrowserRouter as Router, Route, Routes } from 'react-router-dom';
import CostView from './cost/CostView';
import Header from './header/Header';
import MessageView from './messages/MessageView';
import ChatSummaryView from './chatSummary/ChatSummaryView';

function App() {
  return (
    <>
      <div className="content-container">
        <Router>
          <Header />
          <Routes>
            <Route path="/cost" element={<CostView />} />
            <Route path="/messages" element={<MessageView />} />
            <Route path="chats" element={<ChatSummaryView/>} />
          </Routes>
        </Router>
      </div>
    </>
  )
}

export default App