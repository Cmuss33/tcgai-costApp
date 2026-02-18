import { useEffect, useState } from "react";
import { Link, useNavigate } from "react-router-dom";
import "./ChatSummaryView.css";

function ChatSummaryView() {
  const [chats, setChats] = useState([]);
  const [loadingEval, setLoadingEval] = useState({});
  const [accuracy, setAccuracy] = useState({});

  const navigate = useNavigate();

  const costPerInput = 1 / 1000000;
  const costPerOutput = 5 / 1000000;

  useEffect(() => {
    fetch("http://127.0.0.1:8000/api/cost/get_chat_ids/")
      .then(res => res.json())
      .then(data => {
        setChats(data);

        // Initialize accuracy state for already-evaluated chats
        const initialAccuracy = {};
        data.forEach(chat => {
          if (chat.evaluation_score !== null && chat.evaluation_score !== undefined) {
            initialAccuracy[chat.chat_id] = chat.evaluation_score;
          }
        });
        setAccuracy(initialAccuracy);
      })
      .catch(err => console.error("Error fetching chats:", err));
  }, [navigate]);

  const evaluateAccuracy = async (chatId) => {
    setLoadingEval(prev => ({ ...prev, [chatId]: true }));

    try {
      const res = await fetch(`http://127.0.0.1:8000/api/cost/evaluate_chat/`, {
        method: "POST",
        headers: {
          "Content-Type": "application/json",
        },
        body: JSON.stringify({ chat_id: chatId }),
        credentials: "include",
      });

      if (!res.ok) throw new Error(`Server returned ${res.status}`);

      const data = await res.json();

      setAccuracy(prev => ({
        ...prev,
        [chatId]: data.eval_percentage,
      }));
    } catch (err) {
      console.error("Accuracy evaluation failed:", err);
    } finally {
      setLoadingEval(prev => ({ ...prev, [chatId]: false }));
    }
  };

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
          {chats.map(chat => (
            <tr key={chat.chat_id}>
              <td>
                <Link className="chat-link" to={`/messages/${chat.chat_id}`}>
                  {chat.chat_id}
                </Link>
              </td>
              <td>{new Date(chat.timestamp).toLocaleString()}</td>
              <td>{chat.intent}</td>
              <td>
                {accuracy[chat.chat_id] !== undefined ? (
                  <span className="accuracy-result">
                    {accuracy[chat.chat_id]}%
                  </span>
                ) : (
                  <button
                    className="eval-button"
                    onClick={() => evaluateAccuracy(chat.chat_id)}
                    disabled={loadingEval[chat.chat_id]}
                  >
                    {loadingEval[chat.chat_id] ? (
                      <span className="spinner" />
                    ) : (
                      "Evaluate Accuracy"
                    )}
                  </button>
                )}
              </td>
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
