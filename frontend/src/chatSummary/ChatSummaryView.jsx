import { useEffect, useState } from "react";
import "./ChatSummaryView.css";

function ChatSummaryView() {

  const [chats, setChats] = useState([]);

  useEffect(() => {
      fetch("http://127.0.0.1:8000/api/cost/get_chat_ids/")
        .then((res) => res.json())
        .then((data) => {
          setChats(data);
          console.log(data);
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
              <td>{chat.chat_id}</td>
              <td>null</td>
              <td>null</td>
              <td>null</td>
              <td>null</td>
              <td>null</td>
              <td>{chat.model}</td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}

export default ChatSummaryView;
