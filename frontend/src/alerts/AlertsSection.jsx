import { useCallback, useEffect, useState } from "react";

const API_URL = import.meta.env.VITE_API_URL ?? "";

/* ---------- C4: alerts & preference memory ---------- */
function AlertsSection() {
  const [bundle, setBundle] = useState(null);
  const [prefs, setPrefs] = useState(null);
  const [loadError, setLoadError] = useState(null);
  const [form, setForm] = useState({
    rule_type: "cost_per_conversation", name: "", threshold: "", cooldown_hours: 24,
  });
  const [formError, setFormError] = useState(null);
  const [edits, setEdits] = useState({});

  const apiCall = useCallback(async (path, method = "GET", body) => {
    const res = await fetch(`${API_URL}/api/cost${path}`, {
      method,
      credentials: "include",
      headers: body ? { "Content-Type": "application/json" } : undefined,
      body: body ? JSON.stringify(body) : undefined,
    });
    const data = await res.json().catch(() => ({}));
    if (!res.ok) throw new Error(data.error || `HTTP ${res.status}`);
    return data;
  }, []);

  const reload = useCallback(async () => {
    try {
      const [b, p] = await Promise.all([apiCall("/alert_rules/"), apiCall("/preferences/")]);
      setBundle(b);
      setPrefs(p.preferences ?? {});
    } catch (e) {
      setLoadError(e.message);
    }
  }, [apiCall]);

  useEffect(() => {
    // Initial load on mount. State updates happen in the async continuation,
    // not synchronously in the effect body.
    let cancelled = false;
    (async () => {
      try {
        const [b, p] = await Promise.all([apiCall("/alert_rules/"), apiCall("/preferences/")]);
        if (!cancelled) {
          setBundle(b);
          setPrefs(p.preferences ?? {});
        }
      } catch (e) {
        if (!cancelled) setLoadError(e.message);
      }
    })();
    return () => { cancelled = true; };
  }, [apiCall]);

  const addRule = async (e) => {
    e.preventDefault();
    setFormError(null);
    try {
      await apiCall("/alert_rules/", "POST", {
        rule_type: form.rule_type,
        name: form.name.trim(),
        threshold: parseFloat(form.threshold),
        cooldown_hours: parseInt(form.cooldown_hours, 10) || 24,
      });
      setForm({ rule_type: "cost_per_conversation", name: "", threshold: "", cooldown_hours: 24 });
      reload();
    } catch (err) {
      setFormError(err.message);
    }
  };

  const saveEdit = async (rule) => {
    const ed = edits[rule.id] || {};
    try {
      await apiCall(`/alert_rules/${rule.id}/`, "PUT", {
        threshold: parseFloat(ed.threshold ?? rule.threshold),
        cooldown_hours: parseInt(ed.cooldown_hours ?? rule.cooldown_hours, 10),
        enabled: rule.enabled,
        name: rule.name,
      });
      setEdits((prev) => ({ ...prev, [rule.id]: undefined }));
      reload();
    } catch (err) {
      setLoadError(err.message);
    }
  };

  const toggleRule = async (rule) => {
    try {
      await apiCall(`/alert_rules/${rule.id}/`, "PUT", {
        threshold: rule.threshold,
        cooldown_hours: rule.cooldown_hours,
        enabled: !rule.enabled,
        name: rule.name,
      });
      reload();
    } catch (err) {
      setLoadError(err.message);
    }
  };

  const deleteRule = async (rule) => {
    if (!window.confirm(`Delete alert rule "${rule.name}"?`)) return;
    try {
      await apiCall(`/alert_rules/${rule.id}/`, "DELETE");
      reload();
    } catch (err) {
      setLoadError(err.message);
    }
  };

  const ackFiring = async (f) => {
    try {
      await apiCall(`/alert_firings/${f.id}/acknowledge/`, "POST");
      reload();
    } catch (err) {
      setLoadError(err.message);
    }
  };

  const forgetPref = async (key) => {
    try {
      await apiCall(`/preferences/${encodeURIComponent(key)}/`, "DELETE");
      reload();
    } catch (err) {
      setLoadError(err.message);
    }
  };

  if (loadError) {
    return (
      <section aria-label="Alerts and preferences">
        <div className="cr__section-eyebrow"><span>🔔</span> Alerts &amp; preferences</div>
        <p className="cr__notice">Couldn&rsquo;t load alert settings ({loadError}).</p>
      </section>
    );
  }
  if (!bundle) {
    return (
      <section aria-label="Alerts and preferences">
        <div className="cr__section-eyebrow"><span>🔔</span> Alerts &amp; preferences</div>
        <p className="cr__prov">Loading alert settings…</p>
      </section>
    );
  }

  const { rules = [], recent_firings = [], rule_types = {} } = bundle;
  const rememberedMonth = prefs?.dashboard_month;

  return (
    <section aria-label="Alerts and preferences">
      <div className="cr__section-eyebrow"><span>🔔</span> Alerts &amp; preferences</div>
      <p className="cr__prov">
        The app watches these rules hourly and posts to the AOP Slack channel on a new breach —
        once per breach, no duplicates. Thresholds are editable here; no deploy needed.
      </p>

      <div className="cr__alert-rules">
        {rules.map((rule) => {
          const ed = edits[rule.id];
          return (
            <div key={rule.id} className={`cr__alert-rule${rule.breached ? " is-breaching" : ""}${rule.enabled ? "" : " is-off"}`}>
              <div className="cr__alert-rule-head">
                <div>
                  <strong>{rule.name}</strong>
                  <span className="cr__alert-rule-type">{rule.type_label}</span>
                </div>
                <span className={`cr__alert-status cr__alert-status--${!rule.enabled ? "off" : rule.breached ? "breach" : "ok"}`}>
                  {!rule.enabled ? "Off" : rule.breached ? "Breaching" : "OK"}
                </span>
              </div>
              {ed ? (
                <div className="cr__alert-edit">
                  <label>
                    Threshold ({rule.unit})
                    <input
                      type="number" step="any" min="0"
                      value={ed.threshold ?? rule.threshold}
                      onChange={(e) => setEdits((p) => ({ ...p, [rule.id]: { ...ed, threshold: e.target.value } }))}
                    />
                  </label>
                  <label>
                    Cooldown (hours)
                    <input
                      type="number" step="1" min="1"
                      value={ed.cooldown_hours ?? rule.cooldown_hours}
                      onChange={(e) => setEdits((p) => ({ ...p, [rule.id]: { ...ed, cooldown_hours: e.target.value } }))}
                    />
                  </label>
                  <div className="cr__alert-actions">
                    <button type="button" className="cr__btn" onClick={() => saveEdit(rule)}>Save</button>
                    <button type="button" className="cr__btn cr__btn--ghost" onClick={() => setEdits((p) => ({ ...p, [rule.id]: undefined }))}>Cancel</button>
                  </div>
                </div>
              ) : (
                <div className="cr__alert-meta">
                  <span>Above <strong>{rule.threshold}</strong> {rule.unit}</span>
                  <span>Cooldown {rule.cooldown_hours}h</span>
                  {rule.last_fired_at && <span>Last fired {new Date(rule.last_fired_at).toLocaleString()}</span>}
                </div>
              )}
              {!ed && (
                <div className="cr__alert-actions">
                  <button type="button" className="cr__btn cr__btn--ghost" onClick={() => setEdits((p) => ({ ...p, [rule.id]: { threshold: rule.threshold, cooldown_hours: rule.cooldown_hours } }))}>Edit</button>
                  <button type="button" className="cr__btn cr__btn--ghost" onClick={() => toggleRule(rule)}>{rule.enabled ? "Disable" : "Enable"}</button>
                  <button type="button" className="cr__btn cr__btn--ghost cr__btn--danger" onClick={() => deleteRule(rule)}>Delete</button>
                </div>
              )}
              {rule.hint && <p className="cr__alert-hint">{rule.hint}</p>}
            </div>
          );
        })}
      </div>

      <form className="cr__alert-add" onSubmit={addRule}>
        <strong>Add a rule</strong>
        <div className="cr__alert-add-row">
          <label>
            Metric
            <select value={form.rule_type} onChange={(e) => setForm((f) => ({ ...f, rule_type: e.target.value }))}>
              {Object.entries(rule_types).map(([k, v]) => (
                <option key={k} value={k}>{v.label}</option>
              ))}
            </select>
          </label>
          <label>
            Name <span className="cr__muted">(optional)</span>
            <input value={form.name} onChange={(e) => setForm((f) => ({ ...f, name: e.target.value }))} placeholder="e.g. Weekday cost spike" />
          </label>
          <label>
            Threshold
            <input type="number" step="any" min="0" required value={form.threshold} onChange={(e) => setForm((f) => ({ ...f, threshold: e.target.value }))} />
          </label>
          <label>
            Cooldown (h)
            <input type="number" step="1" min="1" value={form.cooldown_hours} onChange={(e) => setForm((f) => ({ ...f, cooldown_hours: e.target.value }))} />
          </label>
          <button type="submit" className="cr__btn">Add rule</button>
        </div>
        {formError && <p className="cr__notice">Couldn&rsquo;t add the rule ({formError}).</p>}
      </form>

      {recent_firings.length > 0 && (
        <div className="cr__alert-firings">
          <strong>Recent alerts</strong>
          {recent_firings.map((f) => (
            <div key={f.id} className="cr__alert-firing">
              <div>
                <span>{f.headline}</span>
                <span className="cr__muted"> · {f.rule_name} · {new Date(f.fired_at).toLocaleString()}</span>
              </div>
              {f.acknowledged_at ? (
                <span className="cr__muted">Acknowledged ✓</span>
              ) : (
                <button type="button" className="cr__btn cr__btn--ghost" onClick={() => ackFiring(f)}>Acknowledge</button>
              )}
            </div>
          ))}
        </div>
      )}

      <div className="cr__alert-prefs">
        <strong>Remembered</strong>
        {rememberedMonth ? (
          <span>
            Last viewed month: <strong>{rememberedMonth}</strong>{" "}
            <button type="button" className="cr__btn cr__btn--ghost" onClick={() => forgetPref("dashboard_month")}>Forget</button>
          </span>
        ) : (
          <span className="cr__muted">Nothing remembered yet — the month you pick above is saved here.</span>
        )}
      </div>
    </section>
  );
}

export default AlertsSection;
