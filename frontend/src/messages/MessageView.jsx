import { useEffect, useState } from "react";
import { useParams, useNavigate } from "react-router-dom";
import "./MessageView.css";

function MessageView() {
  const { chatId } = useParams();
  const navigate = useNavigate();

  const [chats, setChats] = useState([]);
  const [selectedChatId, setSelectedChatId] = useState(chatId ?? null);
  const [messages, setMessages] = useState([]);
  const [loadingChats, setLoadingChats] = useState(true);
  const [loadingMessages, setLoadingMessages] = useState(false);
  const [chatCost, setChatCost] = useState(0);
  const [expandedMessages, setExpandedMessages] = useState({});

  const costPerInput = 1 / 1000000;
  const costPerOutput = 5 / 1000000;

  const toggleExpand = (id, type) => {
    const key = `${id}-${type}`;
    setExpandedMessages(prev => ({
      ...prev,
      [key]: !prev[key],
    }));
  };

  useEffect(() => {
    if (chatId) setSelectedChatId(chatId);
  }, [chatId]);

  // Fetch chats
  useEffect(() => {
    fetch("https://tcgai-costapp.onrender.com/api/cost/get_chat_ids/")
      .then((res) => res.json())
      .then((data) => {
        setChats(data);
        setLoadingChats(false);

        if (!chatId && data.length > 0) {
          navigate(`/messages/${data[0].chat_id}`, { replace: true });
        }
      })
      .catch(() => setLoadingChats(false));
  }, []);

  // Fetch messages whenever selectedChatId changes
  useEffect(() => {
    if (!selectedChatId) return;

    setLoadingMessages(true);
    setMessages([]);
    setExpandedMessages({});

    fetch(
      `https://tcgai-costapp.onrender.com/api/cost/get_messages_by_chat_id/${selectedChatId}/`
    )
      .then((res) => res.json())
      .then((data) => {
        const totalCost = data.reduce((sum, msg) => {
          return (
            sum +
            msg.tokens_in * costPerInput +
            msg.tokens_out * costPerOutput
          );
        }, 0);
        setChatCost(totalCost.toPrecision(2));
        setMessages(data);
        setLoadingMessages(false);
      })
      .catch(() => setLoadingMessages(false));
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
          chats.map((chat) => (
            <div
              key={chat.chat_id}
              className={`chat-id ${
                selectedChatId === chat.chat_id ? "active" : "inactive"
              }`}
              onClick={() => navigate(`/messages/${chat.chat_id}`)}
            >
              Chat {chat.chat_id}
            </div>
          ))
        )}
      </div>

      {/* Main messages panel */}
      <div className="messages-panel">
        <h3>
          Messages for Chat {selectedChatId} — Cost: ${chatCost}
        </h3>

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
                  <div className="timestamp">
                    ${(msg.tokens_in * costPerInput).toPrecision(2)}
                  </div>
                  <div className="timestamp">
                    {new Date(msg.timestamp).toLocaleString()}
                  </div>

                  <button
                    className="expand-button"
                    onClick={() => toggleExpand(msg.id, "in")}
                  >
                    {expandedMessages[`${msg.id}-in`] ? "Collapse" : "Expand"}
                  </button>

                  {expandedMessages[`${msg.id}-in`] && (
                    <div className="formatted-message">
                      <pre>
                        {msg.llm_formatted_message
                          .replace(/([{,]\s*)'([^']+?)'/g, '$1"$2"')
                          .replace(/},\s*{/g, '},\n\n{')}
                      </pre>
                    </div>
                  )}
                </div>

                <div className="ai-message">
                  <div className="message-label">LLM:</div>
                  <div className="message-content">{msg.returned_content}</div>
                  <div className="timestamp">Tokens Out: {msg.tokens_out}</div>
                  <div className="timestamp">
                    {new Date(msg.timestamp).toLocaleString()}
                  </div>
                  <div className="timestamp">
                    ${(msg.tokens_out * costPerOutput).toPrecision(2)}
                  </div>

                  <button
                    className="expand-button"
                    onClick={() => toggleExpand(msg.id, "out")}
                  >
                    {expandedMessages[`${msg.id}-out`] ? "Collapse" : "Expand"}
                  </button>

                  {expandedMessages[`${msg.id}-out`] && (
                    <div className="formatted-message">
                      <pre>
                        {msg.llm_formatted_returned_message
                          .replace(/([{,]\s*)'([^']+?)'/g, '$1"$2"')
                          .replace(/},\s*{/g, '},\n\n{')}
                      </pre>
                    </div>
                  )}
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
