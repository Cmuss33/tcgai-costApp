import { useState } from "react";

function FlagChatModal({ chatId, initialReason = "", pending, error, onSubmit, onClose }) {
  const [reason, setReason] = useState(initialReason);

  return (
    <div className="modal-overlay">
      <div className="flag-modal">
        <div className="modal-header">
          <h2>Flag chat {chatId}</h2>
        </div>

        <div className="flag-modal-body">
          <label htmlFor="flag-reason">
            Why does this conversation need investigation?
          </label>
          <textarea
            id="flag-reason"
            className="flag-reason-input"
            value={reason}
            onChange={(e) => setReason(e.target.value)}
            rows={6}
            placeholder="Describe what looks wrong…"
            disabled={pending}
          />
          {error && <div className="flag-error">{error}</div>}
        </div>

        <div className="modal-footer">
          <button
            className="close-modal-button flag-cancel"
            onClick={onClose}
            disabled={pending}
          >
            Cancel
          </button>
          <button
            className="close-modal-button"
            onClick={() => onSubmit(reason.trim())}
            disabled={pending || reason.trim() === ""}
          >
            {pending ? <span className="spinner" /> : "Flag & create issues"}
          </button>
        </div>
      </div>
    </div>
  );
}

export default FlagChatModal;
