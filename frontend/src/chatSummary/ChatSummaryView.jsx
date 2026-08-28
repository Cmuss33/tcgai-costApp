import { useEffect, useState } from "react";
import { useNavigate, useSearchParams } from "react-router-dom";
import "./ChatSummaryView.css";
import FlagChatModal from "./FlagChatModal";

function ProductCard({ product }) {
  return (
    <a
      className="product-card"
      href={product.url}
      target="_blank"
      rel="noopener noreferrer"
    >
      <div className="product-card-image-wrap">
        <img
          className="product-card-image"
          src={product.image_url}
          alt={product.title}
        />
        {product.available === false && (
          <span className="product-badge-oos">Out of Stock</span>
        )}
      </div>
      <div className="product-card-title">{product.title}</div>
      <div className="product-card-price">${product.price}</div>
    </a>
  );
}

function ChatSummaryView() {
  const API_URL = import.meta.env.VITE_API_URL;

  const navigate = useNavigate();

  const [chats, setChats] = useState([]);

  const [searchParams] = useSearchParams();
  const [flagState, setFlagState] = useState(null); // { chatId, pending, error }

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

  // Deep link: /chats?chat=<id> auto-opens that chat's transcript modal
  useEffect(() => {
    const chatParam = searchParams.get("chat");
    if (chatParam) {
      openChatModal(chatParam);
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [searchParams]);

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

  const openFlagModal = (chatId) => {
    setFlagState({ chatId, pending: false, error: null });
  };

  const closeFlagModal = () => setFlagState(null);

  const submitFlag = async (reason) => {
    if (!reason) return;
    const chatId = flagState.chatId;
    setFlagState((s) => ({ ...s, pending: true, error: null }));

    try {
      const res = await fetch(`${API_URL}/api/cost/flag_chat/`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        credentials: "include",
        body: JSON.stringify({ chat_id: chatId, reason }),
      });
      const data = await res.json();

      if (!res.ok) {
        setFlagState((s) => ({
          ...s,
          pending: false,
          error: data.error || `Request failed (${res.status})`,
        }));
        return;
      }

      setChats((prev) =>
        prev.map((c) =>
          c.chat_id === chatId
            ? {
                ...c,
                investigation_status: data.investigation_status || "flagged",
                github_issue_url: data.github_issue_url ?? c.github_issue_url,
                linear_issue_url: data.linear_issue_url ?? c.linear_issue_url,
                flag_error: data.flag_error ?? "",
              }
            : c
        )
      );
      setFlagState(null);
    } catch (err) {
      setFlagState((s) => ({ ...s, pending: false, error: String(err) }));
    }
  };

  const renderInvestigationCell = (chat) => {
    const status = chat.investigation_status || "unflagged";
    const links = (
      <span className="issue-links">
        {chat.github_issue_url && (
          <a href={chat.github_issue_url} target="_blank" rel="noopener noreferrer">
            GitHub ↗
          </a>
        )}
        {chat.linear_issue_url && (
          <a href={chat.linear_issue_url} target="_blank" rel="noopener noreferrer">
            Linear ↗
          </a>
        )}
      </span>
    );

    if (status === "unflagged") {
      return (
        <button className="flag-button" onClick={() => openFlagModal(chat.chat_id)}>
          🚩 Flag
        </button>
      );
    }

    if (status === "resolved") {
      return (
        <span className="investigation-cell">
          <span className="badge badge-resolved">Resolved ✓</span>
          {links}
        </span>
      );
    }

    return (
      <span className="investigation-cell">
        <span className="badge badge-flagged">Flagged</span>
        {chat.flag_error ? (
          <button
            className="retry-link"
            title={chat.flag_error}
            onClick={() => openFlagModal(chat.chat_id)}
          >
            ⚠ Retry
          </button>
        ) : null}
        {links}
      </span>
    );
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
            <th>Products</th>
            <th>Investigation</th>
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

              <td>
                {chat.products_shown_count > 0
                  ? chat.products_shown_count
                  : "-"}
              </td>

              <td>{renderInvestigationCell(chat)}</td>
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

                              {msg.products_shown && (
                                <div className="products-shown">
                                  {msg.products_shown.primary?.length > 0 && (
                                    <div className="product-section">
                                      <div className="product-section-label">
                                        Primary
                                      </div>
                                      <div className="product-card-list">
                                        {msg.products_shown.primary.map(
                                          (product) => (
                                            <ProductCard
                                              key={product.id}
                                              product={product}
                                            />
                                          )
                                        )}
                                      </div>
                                    </div>
                                  )}

                                  {msg.products_shown.complementary?.length >
                                    0 && (
                                    <div className="product-section product-section-complementary">
                                      <div className="product-section-label">
                                        You Might Also Like{" "}
                                        <span className="product-badge">
                                          Recommended
                                        </span>
                                      </div>
                                      <div className="product-card-list">
                                        {msg.products_shown.complementary.map(
                                          (product) => (
                                            <ProductCard
                                              key={product.id}
                                              product={product}
                                            />
                                          )
                                        )}
                                      </div>
                                    </div>
                                  )}
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

      {flagState && (
        <FlagChatModal
          chatId={flagState.chatId}
          initialReason={
            chats.find((c) => c.chat_id === flagState.chatId)?.flag_reason || ""
          }
          pending={flagState.pending}
          error={flagState.error}
          onSubmit={submitFlag}
          onClose={closeFlagModal}
        />
      )}
    </div>
  );
}

export default ChatSummaryView;