import { NavLink } from 'react-router-dom';
import './Header.css'

function Header() {
  return (
    <>
      <div className="header-container">
        <div className="header-content">
          <NavLink to="/cost" className="header-btn">View Costs</NavLink>
          <NavLink to="/messages" className="header-btn">View Messages</NavLink>
          <NavLink to="/chats" className="header-btn">Chat Summary</NavLink>
        </div>
      </div>
    </>
  )
}

export default Header