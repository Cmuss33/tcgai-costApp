import { useEffect, useState } from "react";
import { Link } from "react-router-dom";
import "./ChatSummaryView.css";
import { useNavigate } from 'react-router-dom';

function ChatSummaryView() {
  const [chats, setChats] = useState([]);

  const navigate = useNavigate();

  const costPerInput = 1 / 1000000;
  const costPerOutput = 5 / 1000000;

  useEffect(() => {
    fetch("http://localhost:8000/api/cost/auth-check/", {
      credentials: "include",
      })
        .then(res => res.json())
        .then(data => {
          if (!data.authenticated) {
            navigate("/");
          }
    });

    fetch("http://127.0.0.1:8000/api/cost/get_chat_ids/")
      .then((res) => res.json())
      .then((data) => {
        setChats(data);
      })
      .catch((err) => {
        console.error("Error fetching chats:", err);
      });
  }, []);

  return (
    <div className="chat-summary-container">
      <table className="chat-summary-table">
        <thead>
          <tr>
            <th>Chat ID</th>
            <th>Date</th>
            <th>Intent</th>
            <th>Eval %</th>
            <th>Tokens In</th>
            <th>Tokens Out</th>
            <th>Est. Cost ($)</th>
            <th>Model</th>
          </tr>
        </thead>
        <tbody>
          {chats.map((chat) => (
            <tr key={chat.chat_id}>
              <td>
                <Link className="chat-link" to={`/messages/${chat.chat_id}`}>
                  {chat.chat_id}
                </Link>
              </td>
              <td>{new Date(chat.timestamp).toLocaleString()}</td>
              <td>EVAL % NOT FOUND</td>
              <td>{chat.intent}</td>
              <td>{chat.tokens_in}</td>
              <td>{chat.tokens_out}</td>
              <td>
                $
                {(
                  chat.tokens_in * costPerInput +
                  chat.tokens_out * costPerOutput
                ).toPrecision(2)}
              </td>
              <td>{chat.model}</td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}

export default ChatSummaryView;
