import { NavLink } from 'react-router-dom';
import './Header.css'

function Header() {
  return (
    <>
      <div className="header-container">
        <div className="header-content">
          <NavLink to="/home" className="header-btn">Overview</NavLink>
          <NavLink to="/chats" className="header-btn">Chat Summary</NavLink>
        </div>
      </div>
    </>
  )
}

export default Header