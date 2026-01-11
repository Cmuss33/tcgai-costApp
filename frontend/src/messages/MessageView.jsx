import { useEffect, useState } from "react";
import "./MessageView.css";

function MessageView() {
  const [chats, setChats] = useState([]); // now full chat objects
  const [selectedChatId, setSelectedChatId] = useState(null);
  const [messages, setMessages] = useState([]);
  const [loadingChats, setLoadingChats] = useState(true);
  const [loadingMessages, setLoadingMessages] = useState(false);
  const [chatCost, setChatCost] = useState(0);
  const [expandedMessages, setExpandedMessages] = useState({});

  // TODO: Find more accurate cost
  const costPerInput = 1 / 1000000;
  const costPerOutput = 5 / 1000000;

  const toggleExpand = (id, type) => {
    const key = `${id}-${type}`;
    setExpandedMessages(prev => ({
      ...prev,
      [key]: !prev[key],
    }));
  };

  // Fetch full chat objects on mount
  useEffect(() => {
    fetch("https://tcgai-costapp.onrender.com/api/cost/get_chat_ids/")
      .then((res) => res.json())
      .then((data) => {
        setChats(data);
        setLoadingChats(false);
        if (data.length > 0) setSelectedChatId(data[0].chat_id); // select first by default
      })
      .catch((err) => {
        console.error("Error fetching chats:", err);
        setLoadingChats(false);
      });
  }, []);

  // Fetch messages whenever selectedChatId changes
  useEffect(() => {
    if (!selectedChatId) return;
    setExpandedMessages({});

    setLoadingMessages(true);
    setMessages([]); // clear previous messages while loading
    fetch(`https://tcgai-costapp.onrender.com/api/cost/get_messages_by_chat_id/${selectedChatId}/`)
      .then((res) => res.json())
      .then((data) => {
        const totalCost = data.reduce((sum, msg) => {
          const inputCost = msg.tokens_in * costPerInput;
          const outputCost = msg.tokens_out * costPerOutput;
          return sum + inputCost + outputCost;
        }, 0);
        setChatCost(totalCost.toPrecision(2));

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
          chats.map((chat) => (
            <div
              key={chat.chat_id}
              className={`chat-id ${selectedChatId === chat.chat_id ? "active" : "inactive"}`}
              onClick={() => setSelectedChatId(chat.chat_id)}
            >
              <div>Chat {chat.chat_id}</div>
            </div>
          ))
        )}
      </div>

      {/* Main messages panel */}
      <div className="messages-panel">
        <h3>Messages for Chat {selectedChatId} - Cost: ${chatCost}</h3>
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
                  <div className="timestamp">${(msg.tokens_in * costPerInput).toPrecision(2)}</div> 
                  <div className="expand-button-container">
                    <button
                      className="expand-button"
                      onClick={() => toggleExpand(msg.id, 'in')}
                    >
                      {expandedMessages[`${msg.id}-in`] ? "Collapse" : "Expand"}
                    </button>
                  </div>

                  {expandedMessages[`${msg.id}-in`] && (
                    <div className="formatted-message">
                      <pre>
                        {msg.llm_formatted_message
                          .replace(/([{,]\s*)'([^']+?)'/g, '$1"$2"')
                          .replace(/},\s*{/g, '},\n\n{')
                        }
                      </pre>
                    </div>
                  )}
                </div>
                <div className="ai-message">
                  <div className="message-label">LLM:</div>
                  <div className="message-content">{msg.returned_content}</div>
                  <div className="timestamp">Tokens Out: {msg.tokens_out}</div>
                  <div className="timestamp">{new Date(msg.timestamp).toLocaleString()}</div>
                  <div className="timestamp">${(msg.tokens_out * costPerOutput).toPrecision(2)}</div>
                  <div className="expand-button-container">
                    <button
                      className="expand-button"
                      onClick={() => toggleExpand(msg.id, 'out')}
                    >
                      {expandedMessages[`${msg.id}-out`] ? "Collapse" : "Expand"}
                    </button>
                  </div>

                  {expandedMessages[`${msg.id}-out`] && (
                    <div className="formatted-message">
                      <pre>
                        {msg.llm_formatted_returned_message
                          .replace(/([{,]\s*)'([^']+?)'/g, '$1"$2"')
                          .replace(/},\s*{/g, '},\n\n{')
                        }
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
