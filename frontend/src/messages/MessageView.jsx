import { useEffect, useState } from "react";
import "./MessageView.css";

function MessageView() {
  const [chatIds, setChatIds] = useState([]);
  const [selectedChatId, setSelectedChatId] = useState(null);
  const [messages, setMessages] = useState([]);
  const [loadingChats, setLoadingChats] = useState(true);
  const [loadingMessages, setLoadingMessages] = useState(false);

  // Fetch chat IDs on mount
  useEffect(() => {
    fetch("http://127.0.0.1:8000/api/cost/get_chat_ids/")
      .then((res) => res.json())
      .then((data) => {
        setChatIds(data);
        setLoadingChats(false);
        if (data.length > 0) setSelectedChatId(data[0]); // select first by default
      })
      .catch((err) => {
        console.error("Error fetching chat IDs:", err);
        setLoadingChats(false);
      });
  }, []);

  // Fetch messages whenever selectedChatId changes
  useEffect(() => {
    if (!selectedChatId) return;

    setLoadingMessages(true);
    setMessages([]); // clear previous messages while loading
    fetch(`http://127.0.0.1:8000/api/cost/get_messages_by_chat_id/${selectedChatId}/`)
      .then((res) => res.json())
      .then((data) => {
        setMessages(data);
        setLoadingMessages(false);
      })
      .catch((err) => {
        console.error("Error fetching messages:", err);
        setLoadingMessages(false);
      });
  }, [selectedChatId]);

  return (
    <div className="message-view-container">
      {/* Sidebar */}
      <div className="sidebar">
        <h3>Chats</h3>
        {loadingChats ? (
          <div className="message-spinner-container">
            <div className="message-spinner"></div>
          </div>
        ) : (
          chatIds.map((id) => (
            <div
              key={id}
              className={`chat-id ${selectedChatId === id ? "active" : "inactive"}`}
              onClick={() => setSelectedChatId(id)}
            >
              Chat {id}
            </div>
          ))
        )}
      </div>

      {/* Main messages panel */}
      <div className="messages-panel">
        <h3>Messages for Chat {selectedChatId}</h3>
        {loadingMessages ? (
          <div className="spinner"></div>
        ) : messages.length === 0 ? (
          <p>No messages found.</p>
        ) : (
          <div className="messages-list">
            {messages.map((msg) => (
              <div key={msg.id} className="message-pair">
                <div className="user-message">
                  <div className="message-label">User:</div>
                  <div className="message-content">{msg.content}</div>
                  <div className="timestamp">Tokens In: {msg.tokens_in}</div>
                  <div className="timestamp">{new Date(msg.timestamp).toLocaleString()}</div>
                </div>
                <div className="ai-message">
                  <div className="message-label">LLM:</div>
                  <div className="message-content">{msg.returned_content}</div>
                  <div className="timestamp">Tokens Out: {msg.tokens_out}</div>
                  <div className="timestamp">{new Date(msg.timestamp).toLocaleString()}</div>
                </div>
              </div>
            ))}
          </div>
        )}
      </div>
    </div>
  );
}

export default MessageView;
