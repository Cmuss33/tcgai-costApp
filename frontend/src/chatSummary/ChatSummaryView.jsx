import { useEffect, useRef, useState } from "react";
import { useNavigate, useSearchParams } from "react-router-dom";
import "./ChatSummaryView.css";
import FlagChatModal from "./FlagChatModal";
import { estimateCost, estimateInputCost, estimateTokenCost, formatCost, getModelRate } from "./pricing";

function getPageNumbers(currentPage, totalPages) {
  if (totalPages <= 7) {
    return Array.from({ length: totalPages }, (_, i) => i + 1);
  }
  if (currentPage <= 4) {
    return [1, 2, 3, 4, 5, "...", totalPages];
  }
  if (currentPage >= totalPages - 3) {
    return [1, "...", totalPages - 4, totalPages - 3, totalPages - 2, totalPages - 1, totalPages];
  }
  return [1, "...", currentPage - 1, currentPage, currentPage + 1, "...", totalPages];
}

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
  const API_URL = import.meta.env.VITE_API_URL ?? "";

  const navigate = useNavigate();

  const [chats, setChats] = useState([]);

  const [searchParams, setSearchParams] = useSearchParams();
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

  // Pagination & Page Navigation
  const initialPage = parseInt(searchParams.get("page") || "1", 10);
  const validInitialPage = !isNaN(initialPage) && initialPage >= 1 ? initialPage : 1;
  const [pageSize, setPageSize] = useState(10);
  const [offset, setOffset] = useState((validInitialPage - 1) * 10);
  const [hasNext, setHasNext] = useState(false);
  const [totalChats, setTotalChats] = useState(0);
  const [loadingChats, setLoadingChats] = useState(false);
  const [jumpPageInput, setJumpPageInput] = useState("");
  const [kpiStats, setKpiStats] = useState({
    audited_count: 0,
    avg_score: null,
    needs_attention_count: 0,
    total_conversations: 0,
  });
  const [refreshKey, setRefreshKey] = useState(0);

  const hasOpenedInitialChat = useRef(false);
  const tableRef = useRef(null);

  const [modelRates, setModelRates] = useState({});
  const [batchAuditing, setBatchAuditing] = useState(false);
  const [batchBanner, setBatchBanner] = useState(null);

  const currentPage = Math.floor(offset / pageSize) + 1;
  const totalPages = Math.max(1, Math.ceil(totalChats / pageSize));

  // Real $/token rates derived from Anthropic's own billing data for this
  // month (see pricing.js) - fetched once, not recomputed per chat.
  useEffect(() => {
    fetch(`${API_URL}/api/cost/get_model_rates/`, {
      credentials: "include",
    })
      .then((res) => res.json())
      .then((data) => setModelRates(data.rates || {}))
      .catch((err) =>
        console.error("Error fetching model rates:", err)
      );
  }, [API_URL]);

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
  }, [API_URL, navigate]);

  const [activeFilter, setActiveFilter] = useState(searchParams.get("filter") || "all");
  const [searchQuery, setSearchQuery] = useState("");

  // Store attribution filter (mirrors HomeView.jsx). selectedShops === null
  // means "All stores" (no filtering). Defaults to PRODUCTION_SHOPS (from
  // /api/cost/config/) when designated, otherwise all stores. Rows logged
  // before the chatbot started sending `shop` carry shop="" ("Unknown").
  const [shopOptions, setShopOptions] = useState([]);
  const [productionShops, setProductionShops] = useState([]);
  const [selectedShops, setSelectedShops] = useState(null);
  const [shopConfigLoaded, setShopConfigLoaded] = useState(false);

  useEffect(() => {
    let cancelled = false;
    (async () => {
      try {
        const [cfgRes, shopsRes] = await Promise.all([
          fetch(`${API_URL}/api/cost/config/`, { credentials: "include" }),
          fetch(`${API_URL}/api/cost/shops/`, { credentials: "include" }),
        ]);
        if (cancelled) return;
        const cfg = cfgRes.ok ? await cfgRes.json() : { production_shops: [] };
        const shopsData = shopsRes.ok ? await shopsRes.json() : { shops: [] };
        const prod = cfg.production_shops || [];
        setProductionShops(prod);
        setShopOptions(shopsData.shops || []);
        // Default selection: production shops when designated, else all stores.
        setSelectedShops(prod.length ? [...prod] : null);
      } catch {
        if (!cancelled) setSelectedShops(null);
      } finally {
        if (!cancelled) setShopConfigLoaded(true);
      }
    })();
    return () => {
      cancelled = true;
    };
  }, [API_URL]);

  // Fetch chats
  useEffect(() => {
    // Wait for the store config so the first load already carries the
    // right ?shop= params (production shops when designated).
    if (!shopConfigLoaded) return;
    const params = new URLSearchParams({
      limit: String(pageSize),
      offset: String(offset),
    });
    if (activeFilter && activeFilter !== "all") {
      params.set("filter", activeFilter);
    }
    if (searchQuery.trim()) {
      params.set("search", searchQuery.trim());
    }
    if (selectedShops && selectedShops.length) {
      selectedShops.forEach((shop) => params.append("shop", shop));
    }

    setLoadingChats(true);
    fetch(
      `${API_URL}/api/cost/get_chat_ids/?${params.toString()}`, { credentials: "include" })
      .then((res) => res.json())
      .then((data) => {
        const chatsArray = data.results ?? data;

        setChats(chatsArray);
        setHasNext(data.has_next ?? false);
        const resolvedTotal = data.total !== undefined ? data.total : (data.has_next ? offset + chatsArray.length + 1 : offset + chatsArray.length);
        setTotalChats(resolvedTotal);

        if (data.kpis) {
          setKpiStats(data.kpis);
        }

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

        setAccuracy((prev) => ({ ...prev, ...initialAccuracy }));
      })
      .catch((err) =>
        console.error("Error fetching chats:", err)
      )
      .finally(() => {
        setLoadingChats(false);
      });
  }, [API_URL, offset, pageSize, activeFilter, searchQuery, refreshKey, shopConfigLoaded, selectedShops]);

  // Deep link: /chats?chat=<id> auto-opens that chat's transcript modal
  useEffect(() => {
    const chatParam = searchParams.get("chat");
    if (chatParam && !hasOpenedInitialChat.current) {
      hasOpenedInitialChat.current = true;
      openChatModal(chatParam);
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [searchParams]);

  const handlePageChange = (targetPage) => {
    if (targetPage < 1 || targetPage > totalPages || targetPage === currentPage) return;
    const newOffset = (targetPage - 1) * pageSize;
    setOffset(newOffset);

    const nextParams = new URLSearchParams(searchParams);
    if (targetPage > 1) {
      nextParams.set("page", String(targetPage));
    } else {
      nextParams.delete("page");
    }
    setSearchParams(nextParams, { replace: true });

    if (tableRef.current) {
      tableRef.current.scrollIntoView({ behavior: "smooth", block: "start" });
    }
  };

  const handlePageSizeChange = (e) => {
    const newSize = Number(e.target.value);
    setPageSize(newSize);
    setOffset(0);

    const nextParams = new URLSearchParams(searchParams);
    nextParams.delete("page");
    setSearchParams(nextParams, { replace: true });
  };

  const handleJumpSubmit = (e) => {
    e.preventDefault();
    const pageNum = parseInt(jumpPageInput, 10);
    if (!isNaN(pageNum) && pageNum >= 1 && pageNum <= totalPages) {
      handlePageChange(pageNum);
      setJumpPageInput("");
    }
  };

  const handleFilterClick = (filterKey) => {
    setActiveFilter(filterKey);
    setOffset(0);

    const nextParams = new URLSearchParams(searchParams);
    nextParams.delete("page");
    if (filterKey && filterKey !== "all") {
      nextParams.set("filter", filterKey);
    } else {
      nextParams.delete("filter");
    }
    setSearchParams(nextParams, { replace: true });
  };

  const handleSearchChange = (e) => {
    setSearchQuery(e.target.value);
    setOffset(0);
  };

  // Store filter dropdown value: "all" | "production" | a specific shop domain.
  const shopSelectValue = (() => {
    if (!selectedShops) return "all";
    if (
      productionShops.length &&
      selectedShops.length === productionShops.length &&
      selectedShops.every((shop) => productionShops.includes(shop))
    )
      return "production";
    return selectedShops[0] ?? "all";
  })();

  const handleShopChange = (value) => {
    let next = null; // "All stores" -- no filtering
    if (value === "production") next = [...productionShops];
    else if (value !== "all") next = [value]; // a shop domain, or "" for Unknown
    setSelectedShops(next);
    setOffset(0);
  };

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
      setRefreshKey((k) => k + 1);
    } catch (err) {
      console.error("Accuracy evaluation failed:", err);
    } finally {
      setLoadingEval((prev) => ({
        ...prev,
        [chatId]: false,
      }));
    }
  };

  const handleBatchAudit = async () => {
    if (batchAuditing) return;
    setBatchAuditing(true);
    setBatchBanner(null);

    try {
      const res = await fetch(`${API_URL}/api/cost/batch_evaluate/`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        credentials: "include",
        body: JSON.stringify({ limit: 25 }),
      });
      const data = await res.json();
      if (!res.ok) {
        setBatchBanner({
          type: "error",
          message: data.error || data.message || `Audit failed (${res.status})`,
        });
        return;
      }

      if (data.audited_count === 0) {
        setBatchBanner({
          type: "info",
          message: "All conversations are already audited! No pending chats to evaluate.",
        });
        return;
      }

      const newScores = {};
      data.results.forEach((r) => {
        newScores[r.chat_id] = r.score;
      });
      setAccuracy((prev) => ({ ...prev, ...newScores }));
      setRefreshKey((k) => k + 1);

      setBatchBanner({
        type: "success",
        message: `⚡ Successfully auto-evaluated ${data.audited_count} conversations (Est. cost: $${data.estimated_cost_usd} USD)! Store Accuracy Rating updated.`,
      });
    } catch (err) {
      setBatchBanner({ type: "error", message: String(err) });
    } finally {
      setBatchAuditing(false);
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

      setChats((prev) => {
        const exists = prev.some((c) => c.chat_id === chatId);
        if (exists) {
          return prev.map((c) =>
            c.chat_id === chatId
              ? {
                  ...c,
                  investigation_status: data.investigation_status || "flagged",
                  github_issue_url: data.github_issue_url ?? c.github_issue_url,
                  linear_issue_url: data.linear_issue_url ?? c.linear_issue_url,
                  flag_error: data.flag_error ?? data.linear_error ?? "",
                }
              : c
          );
        }
        return [
          {
            chat_id: chatId,
            investigation_status: data.investigation_status || "flagged",
            github_issue_url: data.github_issue_url,
            linear_issue_url: data.linear_issue_url,
            flag_error: data.flag_error ?? data.linear_error ?? "",
          },
          ...prev,
        ];
      });
      setFlagState(null);
    } catch (err) {
      setFlagState((s) => ({ ...s, pending: false, error: String(err) }));
    }
  };

  const renderModalInvestigation = (chat, chatId) => {
    const status = chat?.investigation_status || "unflagged";
    const links = (
      <span className="issue-links">
        {chat?.github_issue_url && (
          <a href={chat.github_issue_url} target="_blank" rel="noopener noreferrer">
            GitHub ↗
          </a>
        )}
        {chat?.linear_issue_url && (
          <a href={chat.linear_issue_url} target="_blank" rel="noopener noreferrer">
            Linear ↗
          </a>
        )}
      </span>
    );

    if (status === "unflagged") {
      return (
        <button
          className="modal-flag-button"
          onClick={() => openFlagModal(chatId)}
          disabled={loadingMessages}
        >
          🚩 Flag
        </button>
      );
    }

    if (status === "resolved") {
      return (
        <div className="modal-investigation-status">
          <span className="badge badge-resolved">Resolved ✓</span>
          {links}
        </div>
      );
    }

    return (
      <div className="modal-investigation-status">
        <span
          className="badge badge-flagged"
          title={
            chat?.flag_error && chat?.linear_issue_url ? chat.flag_error : undefined
          }
        >
          Flagged
        </span>
        {chat?.flag_error && !chat?.linear_issue_url ? (
          <button
            className="retry-link"
            title={chat.flag_error}
            onClick={() => openFlagModal(chatId)}
          >
            ⚠ Retry
          </button>
        ) : null}
        {links}
      </div>
    );
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
        <span
          className="badge badge-flagged"
          title={
            chat.flag_error && chat.linear_issue_url ? chat.flag_error : undefined
          }
        >
          Flagged
        </span>
        {chat.flag_error && !chat.linear_issue_url ? (
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
  //
  // A multi-tool-use turn logs one Message per LLM round-trip, all sharing
  // the same user `content` -- each round-trip has its OWN tokens_in (the
  // conversation history keeps growing across the turn), not just the
  // first. Previously only the first message's tokens_in was kept per
  // group and every later round-trip's input tokens were silently dropped
  // from both the display and the cost estimate -- undercounting any
  // multi-tool turn on top of the separate cache-token gap (ENG-148).
  // cache_creation_tokens/cache_read_tokens are summed the same way.
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
        lastGroup.tokensIn += msg.tokens_in;
        lastGroup.cacheCreationTokens += msg.cache_creation_tokens || 0;
        lastGroup.cacheReadTokens += msg.cache_read_tokens || 0;
      } else {
        grouped.push({
          userMessage: msg.content,
          tokensIn: msg.tokens_in,
          cacheCreationTokens: msg.cache_creation_tokens || 0,
          cacheReadTokens: msg.cache_read_tokens || 0,
          model: msg.model,
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
    if (searchParams.get("chat")) {
      const nextParams = new URLSearchParams(searchParams);
      nextParams.delete("chat");
      setSearchParams(nextParams, { replace: true });
    }
  };

  return (
    <div className="chat-summary-container">
      {/* Executive Triage Header */}
      <div className="inspector-header">
        <div className="inspector-header-top">
          <div className="inspector-titles">
            <h1><span>🔬</span> Quality &amp; Trust Inspector</h1>
            <p>
              Audit individual shopper conversations, review AI accuracy scores,
              and flag inventory, pricing, or tournament legality discrepancies directly to engineering.
            </p>
          </div>
        </div>

        <div className="inspector-kpi-row">
          <div className="inspector-kpi-card">
            <div className="inspector-kpi-label">Audited Conversations</div>
            <div className="inspector-kpi-val">{kpiStats.audited_count}</div>
          </div>
          <div className="inspector-kpi-card">
            <div className="inspector-kpi-label">Store Accuracy Rating</div>
            <div className="inspector-kpi-val" style={{ color: "#34d399" }}>
              {kpiStats.avg_score != null ? `${kpiStats.avg_score}%` : "—"}
            </div>
          </div>
          <div className="inspector-kpi-card">
            <div className="inspector-kpi-label">Needs Attention</div>
            <div className="inspector-kpi-val" style={{ color: kpiStats.needs_attention_count > 0 ? "#f87171" : "#10b981" }}>
              {kpiStats.needs_attention_count}
            </div>
          </div>
        </div>

        <div className="inspector-controls">
          <div className="inspector-filters">
            <button
              type="button"
              className={`inspector-filter-btn ${activeFilter === "all" ? "active" : ""}`}
              onClick={() => handleFilterClick("all")}
            >
              All Chats
            </button>
            <button
              type="button"
              className={`inspector-filter-btn ${activeFilter === "needs_attention" ? "active" : ""}`}
              onClick={() => handleFilterClick("needs_attention")}
            >
              🚨 Needs Attention (&lt;75% or Flagged)
            </button>
            <button
              type="button"
              className={`inspector-filter-btn ${activeFilter === "unaudited" ? "active" : ""}`}
              onClick={() => handleFilterClick("unaudited")}
            >
              ✨ Unaudited
            </button>
          </div>

          <div className="inspector-actions">
            <button
              type="button"
              className="inspector-batch-btn"
              onClick={handleBatchAudit}
              disabled={batchAuditing}
              title="Audit the latest 25 unaudited chats (~$0.01)"
            >
              {batchAuditing ? (
                <>
                  <span className="spinner" style={{ width: 13, height: 13, marginRight: 6 }} />
                  Auditing 25 chats...
                </>
              ) : (
                <>
                  <span>⚡ Batch Audit 25</span>
                  <span className="batch-cost-pill">~$0.01</span>
                </>
              )}
            </button>
          </div>

          <div className="inspector-store-filter" role="group" aria-label="Store filter">
            <label htmlFor="inspector-shop-filter" title="Filter conversations by store">
              🏪
            </label>
            <select
              id="inspector-shop-filter"
              value={shopSelectValue}
              onChange={(e) => handleShopChange(e.target.value)}
              disabled={!shopConfigLoaded}
              title="Filter conversations by store"
            >
              {productionShops.length > 0 && (
                <option value="production">Production stores</option>
              )}
              <option value="all">All stores</option>
              {shopOptions.map((o) => (
                <option key={o.shop || "__unknown"} value={o.shop}>
                  {o.shop || "Unknown"} ({o.chat_count})
                </option>
              ))}
            </select>
          </div>

          <div className="inspector-search-wrap">
            <span className="inspector-search-icon">🔍</span>
            <input
              type="text"
              className="inspector-search-input"
              placeholder="Search chat ID or customer query…"
              value={searchQuery}
              onChange={handleSearchChange}
            />
          </div>
        </div>

        {batchBanner && (
          <div className={`batch-audit-banner batch-audit-banner--${batchBanner.type}`}>
            <span>{batchBanner.message}</span>
            <button
              type="button"
              className="batch-audit-banner-close"
              onClick={() => setBatchBanner(null)}
              aria-label="Close message"
            >
              ✕
            </button>
          </div>
        )}
      </div>

      {/* Table meta bar with count, active page info, and page size selector */}
      <div className="chat-meta-bar" ref={tableRef}>
        <div className="chat-meta-count">
          {loadingChats ? (
            <span className="chat-meta-loading">
              <span className="spinner" style={{ width: 13, height: 13, marginRight: 6 }} />
              Loading conversations...
            </span>
          ) : totalChats > 0 ? (
            <span>
              Showing <strong className="chat-meta-highlight">{offset + 1}–{Math.min(offset + chats.length, totalChats)}</strong> of <strong className="chat-meta-highlight">{totalChats}</strong> conversations
              {totalPages > 1 && (
                <span className="chat-meta-page-tag">Page {currentPage} of {totalPages}</span>
              )}
            </span>
          ) : (
            <span>0 conversations found</span>
          )}
        </div>

        <div className="chat-meta-right">
          <label htmlFor="top-page-size" className="chat-pagesize-label">Chats per page:</label>
          <select
            id="top-page-size"
            value={pageSize}
            onChange={handlePageSizeChange}
            className="chat-pagesize-select"
          >
            <option value={10}>10</option>
            <option value={25}>25</option>
            <option value={50}>50</option>
            <option value={100}>100</option>
          </select>
        </div>
      </div>

      <table className="chat-summary-table">
        <thead>
          <tr>
            <th>Chat ID</th>
            <th>Date</th>
            <th>Customer Inquiry</th>
            <th>AI Accuracy Score</th>
            <th>Products</th>
            <th>Est. Cost ($)</th>
            <th>Model</th>
            <th>Investigation</th>
          </tr>
        </thead>

        <tbody>
          {loadingChats && chats.length === 0 ? (
            <tr>
              <td colSpan="8" style={{ textAlign: "center", padding: "40px", color: "#9ca3af" }}>
                <span className="spinner" style={{ width: 18, height: 18, marginRight: 8, verticalAlign: "middle" }} />
                Loading conversations...
              </td>
            </tr>
          ) : chats.length === 0 ? (
            <tr>
              <td colSpan="8" style={{ textAlign: "center", padding: "40px", color: "#9ca3af" }}>
                No conversations found matching criteria.
              </td>
            </tr>
          ) : (
            chats.map((chat) => (
              <tr key={chat.chat_id}>
                <td>
                  <button
                    className="chat-link"
                    onClick={() => openChatModal(chat.chat_id)}
                  >
                    {chat.chat_id}
                  </button>
                </td>

                <td style={{ whiteSpace: "nowrap", fontSize: "12.5px" }}>
                  {new Date(chat.timestamp).toLocaleDateString()} &middot; {new Date(chat.timestamp).toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' })}
                </td>

                <td>
                  <div className="chat-preview-snippet" title={chat.preview || chat.intent}>
                    {chat.preview ? `"${chat.preview}"` : chat.intent || "—"}
                  </div>
                </td>

                <td>
                  {accuracy[chat.chat_id] !== undefined ? (
                    <span
                      className={`chat-score-badge ${
                        accuracy[chat.chat_id] >= 90
                          ? "chat-score-badge--good"
                          : accuracy[chat.chat_id] >= 75
                          ? "chat-score-badge--fair"
                          : "chat-score-badge--bad"
                      }`}
                    >
                      {accuracy[chat.chat_id] >= 90 ? "🛡️ " : accuracy[chat.chat_id] >= 75 ? "⚠️ " : "🚨 "}
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
                        "⚡ Evaluate"
                      )}
                    </button>
                  )}
                </td>

                <td>
                  {chat.products_shown_count > 0 ? (
                    <span className="chat-products-badge">
                      📦 {chat.products_shown_count}
                    </span>
                  ) : (
                    "—"
                  )}
                </td>

                <td>
                  {formatCost(
                    estimateCost(
                      getModelRate(modelRates, chat.model),
                      chat.tokens_in,
                      chat.tokens_out,
                      chat.cache_creation_tokens,
                      chat.cache_read_tokens
                    )
                  )}
                </td>

                <td style={{ fontSize: "12px", color: "#9ca3af" }}>{chat.model}</td>

                <td>{renderInvestigationCell(chat)}</td>
              </tr>
            ))
          )}
        </tbody>
      </table>

      {/* Pagination Navigation */}
      <div className="chat-pagination-wrapper">
        <div className="chat-pagination-summary">
          {totalChats > 0 ? (
            <span>
              Page <strong className="chat-meta-highlight">{currentPage}</strong> of <strong className="chat-meta-highlight">{totalPages}</strong>
              <span className="chat-summary-count"> ({totalChats} total conversations)</span>
            </span>
          ) : (
            <span>Page 1 of 1</span>
          )}
        </div>

        <nav className="chat-pagination-controls" aria-label="Conversation list pagination">
          <button
            type="button"
            className="chat-page-btn chat-page-nav-btn"
            onClick={() => handlePageChange(1)}
            disabled={currentPage === 1 || loadingChats}
            title="Go to first page"
            aria-label="First page"
          >
            « First
          </button>

          <button
            type="button"
            className="chat-page-btn chat-page-nav-btn"
            onClick={() => handlePageChange(currentPage - 1)}
            disabled={currentPage === 1 || loadingChats}
            title="Go to previous page"
            aria-label="Previous page"
          >
            ‹ Prev
          </button>

          <div className="chat-page-numbers">
            {getPageNumbers(currentPage, totalPages).map((p, idx) => {
              if (p === "...") {
                return (
                  <span key={`ellipsis-${idx}`} className="chat-page-ellipsis" aria-hidden="true">
                    &hellip;
                  </span>
                );
              }
              const isCurrent = p === currentPage;
              return (
                <button
                  key={p}
                  type="button"
                  className={`chat-page-btn chat-page-num-btn ${isCurrent ? "chat-page-btn--active" : ""}`}
                  onClick={() => handlePageChange(p)}
                  disabled={isCurrent || loadingChats}
                  aria-current={isCurrent ? "page" : undefined}
                  aria-label={`Page ${p}`}
                >
                  {p}
                </button>
              );
            })}
          </div>

          <button
            type="button"
            className="chat-page-btn chat-page-nav-btn"
            onClick={() => handlePageChange(currentPage + 1)}
            disabled={currentPage >= totalPages || !hasNext || loadingChats}
            title="Go to next page"
            aria-label="Next page"
          >
            Next ›
          </button>

          <button
            type="button"
            className="chat-page-btn chat-page-nav-btn"
            onClick={() => handlePageChange(totalPages)}
            disabled={currentPage >= totalPages || loadingChats}
            title="Go to last page"
            aria-label="Last page"
          >
            Last »
          </button>
        </nav>

        {totalPages > 3 && (
          <form className="chat-page-jump-form" onSubmit={handleJumpSubmit}>
            <label htmlFor="jump-page-input">Go to page:</label>
            <input
              id="jump-page-input"
              type="number"
              min={1}
              max={totalPages}
              className="chat-page-jump-input"
              value={jumpPageInput}
              onChange={(e) => setJumpPageInput(e.target.value)}
              placeholder={String(currentPage)}
            />
            <button
              type="submit"
              className="chat-page-jump-btn"
              disabled={
                !jumpPageInput ||
                parseInt(jumpPageInput, 10) < 1 ||
                parseInt(jumpPageInput, 10) > totalPages ||
                parseInt(jumpPageInput, 10) === currentPage
              }
            >
              Go
            </button>
          </form>
        )}
      </div>

      {/* MODAL */}
      {selectedChatId && (
        <div className="modal-overlay">
          <div className="chat-modal">
            <div className="modal-header">
              <h2>Chat {selectedChatId}</h2>
              <button
                className="modal-header-close"
                onClick={closeModal}
                aria-label="Close transcript"
              >
                ✕
              </button>
            </div>

            {/* Quality Scorecard Bar in Modal */}
            <div className="modal-eval-header">
              <div className="modal-eval-left">
                <span style={{ fontSize: "12px", color: "#9ca3af", textTransform: "uppercase", fontWeight: 600 }}>
                  Quality Rubric Score:
                </span>
                {accuracy[selectedChatId] !== undefined ? (
                  <span
                    className={`chat-score-badge ${
                      accuracy[selectedChatId] >= 90
                        ? "chat-score-badge--good"
                        : accuracy[selectedChatId] >= 75
                        ? "chat-score-badge--fair"
                        : "chat-score-badge--bad"
                    }`}
                  >
                    {accuracy[selectedChatId]}% Accuracy
                  </span>
                ) : (
                  <span style={{ fontSize: "13px", color: "#9ca3af" }}>Not yet audited</span>
                )}
                <button
                  type="button"
                  className="modal-re-eval-btn"
                  onClick={() => evaluateAccuracy(selectedChatId)}
                  disabled={loadingEval[selectedChatId]}
                >
                  {loadingEval[selectedChatId] ? <span className="spinner" /> : "⚡ Re-evaluate with Claude"}
                </button>
              </div>

              <div className="modal-eval-right">
                {renderModalInvestigation(chats.find((c) => c.chat_id === selectedChatId), selectedChatId)}
              </div>
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
                          {(group.cacheCreationTokens > 0 || group.cacheReadTokens > 0) && (
                            <> (incl. {group.cacheCreationTokens} cache write, {group.cacheReadTokens} cache read)</>
                          )}
                        </div>

                        <div className="timestamp">
                          {formatCost(
                            estimateInputCost(
                              getModelRate(modelRates, group.model),
                              group.tokensIn,
                              group.cacheCreationTokens,
                              group.cacheReadTokens
                            )
                          )}
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
                                {formatCost(
                                  estimateTokenCost(
                                    getModelRate(modelRates, msg.model),
                                    msg.tokens_out,
                                    "output"
                                  )
                                )}
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
              <div className="modal-footer-actions">
                {renderModalInvestigation(
                  chats.find((c) => c.chat_id === selectedChatId),
                  selectedChatId
                )}
              </div>
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