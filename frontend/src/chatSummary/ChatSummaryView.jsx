import { useEffect, useState } from "react";
import { useNavigate } from "react-router-dom";
import "./ChatSummaryView.css";

function ChatSummaryView() {
  const API_URL = import.meta.env.VITE_API_URL;

  const navigate = useNavigate();

  const [chats, setChats] = useState([]);

  const [loadingEval, setLoadingEval] = useState({});
  const [accuracy, setAccuracy] = useState({});

  // Modal state
  const [selectedChatId, setSelectedChatId] = useState(null);
  const [groupedMessages, setGroupedMessages] =
    useState([]);
  const [loadingMessages, setLoadingMessages] =
    useState(false);
  const [expandedMessages, setExpandedMessages] =
    useState({});

  // Pagination
  const [offset, setOffset] = useState(0);
  const limit = 10;
  const [hasNext, setHasNext] = useState(false);

  const costPerInput = 1 / 1000000;
  const costPerOutput = 5 / 1000000;

  // Auth check
  useEffect(() => {
    fetch(`${API_URL}/api/cost/auth-check/`, {
      credentials: "include",
    })
      .then((res) => res.json())
      .then((data) => {
        if (!data.authenticated) {
          navigate("/");
        }
      });
  }, [navigate]);

  // Fetch chats
  useEffect(() => {
    fetch(
      `${API_URL}/api/cost/get_chat_ids/?limit=${limit}&offset=${offset}`, { credentials: "include" })
      .then((res) => res.json())
      .then((data) => {
        const chatsArray = data.results ?? data;

        setChats(chatsArray);
        setHasNext(data.has_next ?? false);

        const initialAccuracy = {};

        chatsArray.forEach((chat) => {
          if (
            chat.evaluation_score !== null &&
            chat.evaluation_score !== undefined
          ) {
            initialAccuracy[chat.chat_id] =
              chat.evaluation_score;
          }
        });

        setAccuracy(initialAccuracy);
      })
      .catch((err) =>
        console.error("Error fetching chats:", err)
      );
  }, [offset]);

  const evaluateAccuracy = async (chatId) => {
    setLoadingEval((prev) => ({
      ...prev,
      [chatId]: true,
    }));

    try {
      const res = await fetch(
        `${API_URL}/api/cost/evaluate_chat/`,
        {
          method: "POST",
          headers: {
            "Content-Type": "application/json",
          },
          body: JSON.stringify({ chat_id: chatId }),
          credentials: "include",
        }
      );

      const data = await res.json();

      setAccuracy((prev) => ({
        ...prev,
        [chatId]: data.eval_percentage,
      }));
    } catch (err) {
      console.error("Accuracy evaluation failed:", err);
    } finally {
      setLoadingEval((prev) => ({
        ...prev,
        [chatId]: false,
      }));
    }
  };

  const toggleExpand = (id, type) => {
    const key = `${id}-${type}`;

    setExpandedMessages((prev) => ({
      ...prev,
      [key]: !prev[key],
    }));
  };

  // GROUP SAME USER MESSAGES
  const groupMessages = (messages) => {
    const grouped = [];

    messages.forEach((msg) => {
      const lastGroup =
        grouped[grouped.length - 1];

      if (
        lastGroup &&
        lastGroup.userMessage === msg.content
      ) {
        lastGroup.responses.push(msg);
      } else {
        grouped.push({
          userMessage: msg.content,
          tokensIn: msg.tokens_in,
          timestamp: msg.timestamp,
          formattedMessage:
            msg.llm_formatted_message,
          responses: [msg],
        });
      }
    });

    return grouped;
  };

  const openChatModal = async (chatId) => {
    setSelectedChatId(chatId);
    setLoadingMessages(true);
    setExpandedMessages({});

    try {
      const res = await fetch(
        `${API_URL}/api/cost/get_messages_by_chat_id/${chatId}/`, { credentials: "include" });

      const data = await res.json();

      setGroupedMessages(groupMessages(data));
    } catch (err) {
      console.error(
        "Failed to fetch messages:",
        err
      );
    } finally {
      setLoadingMessages(false);
    }
  };

  const closeModal = () => {
    setSelectedChatId(null);
    setGroupedMessages([]);
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
          {chats.map((chat) => (
            <tr key={chat.chat_id}>
              <td>
                <button
                  className="chat-link"
                  onClick={() =>
                    openChatModal(chat.chat_id)
                  }
                >
                  {chat.chat_id}
                </button>
              </td>

              <td>
                {new Date(
                  chat.timestamp
                ).toLocaleString()}
              </td>

              <td>{chat.intent}</td>

              <td>
                {accuracy[chat.chat_id] !==
                undefined ? (
                  <span className="accuracy-result">
                    {accuracy[chat.chat_id]}%
                  </span>
                ) : (
                  <button
                    className="eval-button"
                    onClick={() =>
                      evaluateAccuracy(
                        chat.chat_id
                      )
                    }
                    disabled={
                      loadingEval[chat.chat_id]
                    }
                  >
                    {loadingEval[
                      chat.chat_id
                    ] ? (
                      <span className="spinner" />
                    ) : (
                      "Evaluate"
                    )}
                  </button>
                )}
              </td>

              <td>{chat.tokens_in}</td>

              <td>{chat.tokens_out}</td>

              <td>
                $
                {(
                  chat.tokens_in *
                    costPerInput +
                  chat.tokens_out *
                    costPerOutput
                ).toPrecision(2)}
              </td>

              <td>{chat.model}</td>
            </tr>
          ))}
        </tbody>
      </table>

      {/* Pagination */}
      <div className="chat-pagination">
        <button
          onClick={() =>
            setOffset((prev) =>
              Math.max(0, prev - limit)
            )
          }
          disabled={offset === 0}
        >
          ◀ Prev 10
        </button>

        <button
          onClick={() =>
            setOffset((prev) => prev + limit)
          }
          disabled={!hasNext}
        >
          Next 10 ▶
        </button>
      </div>

      {/* MODAL */}
      {selectedChatId && (
        <div className="modal-overlay">
          <div className="chat-modal">
            <div className="modal-header">
              <h2>
                Chat {selectedChatId}
              </h2>
            </div>

            {loadingMessages ? (
              <div className="spinner"></div>
            ) : groupedMessages.length === 0 ? (
              <p>No messages found.</p>
            ) : (
              <div className="messages-list">
                {groupedMessages.map(
                  (group, index) => (
                    <div
                      key={index}
                      className="message-pair"
                    >
                      {/* USER */}
                      <div className="user-message">
                        <div className="message-label">
                          User
                        </div>

                        <div className="message-content">
                          {group.userMessage}
                        </div>

                        <div className="timestamp">
                          Tokens In:{" "}
                          {group.tokensIn}
                        </div>

                        <div className="timestamp">
                          $
                          {(
                            group.tokensIn *
                            costPerInput
                          ).toPrecision(2)}
                        </div>

                        <div className="timestamp">
                          {new Date(
                            group.timestamp
                          ).toLocaleString()}
                        </div>

                        <button
                          className="expand-button"
                          onClick={() =>
                            toggleExpand(
                              index,
                              "in"
                            )
                          }
                        >
                          {expandedMessages[
                            `${index}-in`
                          ]
                            ? "Collapse"
                            : "Expand"}
                        </button>

                        {expandedMessages[
                          `${index}-in`
                        ] && (
                          <div className="formatted-message">
                            <pre>
                              {group.formattedMessage
                                .replace(
                                  /([{,]\s*)'([^']+?)'/g,
                                  '$1"$2"'
                                )
                                .replace(
                                  /},\s*{/g,
                                  "},\n\n{"
                                )}
                            </pre>
                          </div>
                        )}
                      </div>

                      {/* MULTIPLE AI RESPONSES */}
                      <div>
                        {group.responses.map(
                          (
                            msg,
                            responseIndex
                          ) => (
                            <div
                              key={msg.id}
                              className="ai-message"
                            >
                              <div className="message-label">
                                LLM Response #
                                {responseIndex +
                                  1}
                              </div>

                              <div className="message-content">
                                {
                                  msg.returned_content
                                }
                              </div>

                              <div className="timestamp">
                                Tokens Out:{" "}
                                {
                                  msg.tokens_out
                                }
                              </div>

                              <div className="timestamp">
                                $
                                {(
                                  msg.tokens_out *
                                  costPerOutput
                                ).toPrecision(2)}
                              </div>

                              <div className="timestamp">
                                {new Date(
                                  msg.timestamp
                                ).toLocaleString()}
                              </div>

                              <button
                                className="expand-button"
                                onClick={() =>
                                  toggleExpand(
                                    msg.id,
                                    "out"
                                  )
                                }
                              >
                                {expandedMessages[
                                  `${msg.id}-out`
                                ]
                                  ? "Collapse"
                                  : "Expand"}
                              </button>

                              {expandedMessages[
                                `${msg.id}-out`
                              ] && (
                                <div className="formatted-message">
                                  <pre>
                                    {msg.llm_formatted_returned_message
                                      .replace(
                                        /([{,]\s*)'([^']+?)'/g,
                                        '$1"$2"'
                                      )
                                      .replace(
                                        /},\s*{/g,
                                        "},\n\n{"
                                      )}
                                  </pre>
                                </div>
                              )}
                            </div>
                          )
                        )}
                      </div>
                    </div>
                  )
                )}
              </div>
            )}

            <div className="modal-footer">
              <button
                className="close-modal-button"
                onClick={closeModal}
              >
                Close
              </button>
            </div>
          </div>
        </div>
      )}
    </div>
  );
}

export default ChatSummaryView;