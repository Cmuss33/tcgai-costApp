import { Link } from 'react-router-dom';
import './Header.css'

function Header() {
  return (
    <>
      <div className="header-container">
        <div className="header-content">
          <Link to="/cost" className="header-btn">View Costs</Link>
          <Link to="/calendar" className="header-btn">View Messages</Link>
        </div>
      </div>
    </>
  )
}

export default Header