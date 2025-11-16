import './App.css'
import { BrowserRouter as Router, Route, Routes } from 'react-router-dom';
import CostView from './cost/CostView';
import Header from './header/header';

function App() {
  return (
    <>
      <div className="content-container">
        <Router>
          <Header />
          <Routes>
            <Route path="/cost" element={<CostView />} />
          </Routes>
        </Router>
      </div>
    </>
  )
}

export default App