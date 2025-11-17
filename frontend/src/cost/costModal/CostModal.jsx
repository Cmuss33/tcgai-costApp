import "./CostModal.css";

function CostModal({ active, payload, label }) {
  if (active && payload && payload.length) {
    const [year, month, day] = label.split("-").map(Number);
    const formattedLabel = new Date(year, month - 1, day).toLocaleDateString("en-US", {
      month: "long",
      day: "numeric"
    });

    return (
      <div className="cost-modal">
        <p className="cost-modal-date">{formattedLabel}</p>
        <p className="cost-modal-value">
          Total Cost: <strong>${payload[0].value}</strong>
        </p>
      </div>
    );
  }
  return null;
}

export default CostModal;
