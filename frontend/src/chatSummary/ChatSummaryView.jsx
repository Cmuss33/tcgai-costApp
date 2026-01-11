import { useEffect, useState } from "react";
import { Link } from "react-router-dom";
import "./ChatSummaryView.css";

function ChatSummaryView() {
  const [chats, setChats] = useState([]);

  const costPerInput = 1 / 1000000;
  const costPerOutput = 5 / 1000000;

  useEffect(() => {
    fetch("https://tcgai-costapp.onrender.com/api/cost/get_chat_ids/")
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
            <th>Intent</th>
            <th>Tokens In</th>
            <th>Tokens Out</th>
            <th>Est. Cost ($)</th>
            <th>Eval %</th>
            <th>Model</th>
          </tr>
        </thead>
        <tbody>
          {chats.map(chat => (
            <tr key={chat.chat_id}>
              <td>
                <Link
                  to={`/messages/${chat.chat_id}`}
                >
                  {chat.chat_id}
                </Link>
              </td>
              <td>INTENT NOT FOUND</td>
              <td>{chat.tokens_in}</td>
              <td>{chat.tokens_out}</td>
              <td>
                $
                {(chat.tokens_in * costPerInput +
                  chat.tokens_out * costPerOutput
                ).toPrecision(2)}
              </td>
              <td>EVAL % NOT FOUND</td>
              <td>{chat.model}</td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}

export default ChatSummaryView;
