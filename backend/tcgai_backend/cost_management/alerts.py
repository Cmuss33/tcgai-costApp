"""C4a: operator alerts -- deterministic rule evaluation + Slack delivery.

Rules are DB-backed (AlertRule) so thresholds are editable from the dashboard
settings surface without a deploy. The hourly Render cron runs the
`evaluate_alerts` management command, which calls evaluate_all_rules().

Firing semantics: a rule fires on the rising edge (not-breached -> breached)
gated by its cooldown -- exactly one alert per breach episode, no duplicates.
The breached flags reset at month turnover so a new month of continued
breach counts as a new episode (monthly signals like eval drops would
otherwise fire once and stay silent forever).

Delivery: AOP Slack channel (#p-agent-operations-platform) via an incoming
webhook URL in SLACK_ALERTS_WEBHOOK_URL. Plain urllib POST, no Slack SDK.
Fail-silent with a logged warning when unset -- the firing is still recorded
in the DB so the dashboard shows it.
"""
import calendar
import json
import logging
import os
import urllib.request
import urllib.error
from datetime import timedelta

from django.utils import timezone

from .models import AlertRule, AlertFiring, OperatorPreference
from .month_utils import current_month_start, prev_month
from .stats_views import _build_stats, _cache_economics_for, _cost_reconciliation_for
from .verdicts import _build_context, _eval_verdict

logger = logging.getLogger(__name__)

SLACK_WEBHOOK_ENV = "SLACK_ALERTS_WEBHOOK_URL"
DASHBOARD_URL_ENV = "COSTAPP_DASHBOARD_URL"
DASHBOARD_URL_DEFAULT = "https://tcgai-costapp-24lh.onrender.com"
SLACK_TIMEOUT_S = 8

# Rule metadata for the settings UI: human labels, threshold units, and the
# hint shown next to the threshold input. Threshold semantics live on
# AlertRule's docstring; this is the presentation copy.
RULE_META = {
    "cost_per_conversation": {
        "label": "Cost per conversation above $X",
        "unit": "USD / conversation",
        "hint": "Fires when this month's cost per conversation exceeds the threshold.",
    },
    "spend_anomaly": {
        "label": "Spend anomaly vs trailing baseline",
        "unit": "× baseline",
        "hint": "Fires when month-to-date billed spend exceeds the prorated trailing-3-month average by this multiple.",
    },
    "cache_hit_rate_drop": {
        "label": "Cache savings-rate drop",
        "unit": "percentage points",
        "hint": "Fires when the cache savings rate (savings_pct) falls this far vs last month.",
    },
    "eval_score_drop": {
        "label": "Eval score drop",
        "unit": "score points",
        "hint": "Fires when the average evaluation score falls this far vs last month (needs 10+ scored chats).",
    },
}

# Conservative seed defaults (data migration): alert fatigue is the called-out
# risk, so these sit well above the C3 verdict bars.
DEFAULT_RULES = [
    {
        "rule_type": "cost_per_conversation",
        "name": "Cost per conversation above $0.20",
        "threshold": 0.20,
        "cooldown_hours": 24,
    },
    {
        "rule_type": "spend_anomaly",
        "name": "Spend anomaly (2× trailing baseline)",
        "threshold": 2.0,
        "cooldown_hours": 24,
    },
    {
        "rule_type": "cache_hit_rate_drop",
        "name": "Cache savings-rate drop (15pp)",
        "threshold": 15.0,
        "cooldown_hours": 24,
    },
    {
        "rule_type": "eval_score_drop",
        "name": "Eval score drop (8 pts)",
        "threshold": 8.0,
        "cooldown_hours": 24,
    },
]

# Key under which evaluate_all_rules remembers the month it last ran, so
# breached flags reset once at month turnover.
_LAST_EVAL_MONTH_KEY = "alerts_last_eval_month"


def _ev(metric, value, source):
    """One C1-idiom evidence citation for the alert body."""
    return {"kind": "stat", "metric": metric, "value": value, "source": source}


def _usd(value):
    if value is None:
        return "—"
    return f"${value:,.2f}"


def _metric_cost_per_conversation(month_start):
    """(breached, value, headline, evidence) for cost/conversation > $X."""
    stats = _build_stats(month_start)
    cost = (stats.get("per_conversation") or {}).get("cost")
    evidence = [_ev("cost.per_conversation", cost, "monthly_stats")]
    if cost is None:
        return False, None, None, evidence
    return True, cost, None, evidence  # breach decided against the rule threshold


def _metric_spend_anomaly(month_start):
    """Month-to-date billed spend vs the prorated trailing-3-month average."""
    recon = _cost_reconciliation_for(month_start)
    billed_mtd = recon.get("billed_spend")
    baseline_months = []
    m = month_start
    for _ in range(3):
        m = prev_month(m)
        b = _cost_reconciliation_for(m).get("billed_spend")
        if b is not None and b > 0:
            baseline_months.append(b)
    evidence = [
        _ev("spend.billed_mtd", billed_mtd, "cost_reconciliation"),
        _ev("spend.baseline_months", len(baseline_months), "cost_reconciliation"),
    ]
    # Not enough history, or still day 1 (too noisy) -- insufficient data.
    if billed_mtd is None or len(baseline_months) < 2:
        return False, billed_mtd, None, evidence
    days_elapsed = timezone.now().day
    days_in_month = calendar.monthrange(month_start.year, month_start.month)[1]
    if days_elapsed < 2:
        return False, billed_mtd, None, evidence
    baseline = sum(baseline_months) / len(baseline_months)
    expected = baseline * days_elapsed / days_in_month
    ratio = billed_mtd / expected if expected > 0 else None
    evidence.append(_ev("spend.baseline_avg", round(baseline, 2), "cost_reconciliation"))
    evidence.append(_ev("spend.expected_mtd", round(expected, 2), "cost_reconciliation"))
    if ratio is None:
        return False, billed_mtd, None, evidence
    return True, ratio, None, evidence  # breach decided against the rule threshold


def _metric_cache_drop(month_start):
    """Cache savings_pct fall vs last month, in percentage points."""
    cur = _cache_economics_for(month_start).get("savings_pct")
    prev = _cache_economics_for(prev_month(month_start)).get("savings_pct")
    evidence = [
        _ev("cache.savings_pct", cur, "cache_economics"),
        _ev("cache.prev_savings_pct", prev, "cache_economics"),
    ]
    if cur is None or prev is None:
        return False, None, None, evidence
    drop = prev - cur
    return True, drop, None, evidence  # breach decided against the rule threshold


def _metric_eval_drop(month_start, threshold):
    """Reuses C3's eval-drop verdict with the rule's own threshold -- no
    duplicated logic. Returns (breached, drop_points, headline, evidence)."""
    ctx = _build_context(month_start)
    card = _eval_verdict(ctx, drop_points=threshold)
    drop = None
    if ctx["eval_avg"] is not None and ctx["prev_eval_avg"] is not None:
        drop = round(ctx["prev_eval_avg"] - ctx["eval_avg"], 1)
    if card is None:
        return False, drop, None, []
    return True, drop, card["headline"], card["evidence"]


def evaluate_rule(rule, month_start):
    """Evaluate one rule against the current month.

    Returns (breached, value, headline, evidence). `value` is the observed
    metric in the rule's threshold units (or None on insufficient data);
    headline/evidence are only set when breached.
    """
    rtype = rule.rule_type
    if rtype == "cost_per_conversation":
        ok, value, _, evidence = _metric_cost_per_conversation(month_start)
        breached = ok and value is not None and value > rule.threshold
        headline = (
            f"Cost per conversation hit {_usd(value)} (alert above {_usd(rule.threshold)})."
            if breached else None
        )
    elif rtype == "spend_anomaly":
        ok, value, _, evidence = _metric_spend_anomaly(month_start)
        breached = ok and value is not None and value > rule.threshold
        headline = (
            f"Anthropic spend is running {value:.1f}× the trailing baseline "
            f"(alert above {rule.threshold:g}×)." if breached else None
        )
    elif rtype == "cache_hit_rate_drop":
        ok, value, _, evidence = _metric_cache_drop(month_start)
        breached = ok and value is not None and value > rule.threshold
        headline = (
            f"Cache savings rate fell {value:.1f}pp vs last month "
            f"(alert above {rule.threshold:g}pp)." if breached else None
        )
    elif rtype == "eval_score_drop":
        breached, value, headline, evidence = _metric_eval_drop(month_start, rule.threshold)
    else:
        logger.warning("[alerts] unknown rule_type=%s rule=%s; skipping", rtype, rule.id)
        return False, None, None, []
    return breached, value, headline, evidence


def _dashboard_link(deep_link):
    base = os.environ.get(DASHBOARD_URL_ENV, DASHBOARD_URL_DEFAULT).rstrip("/")
    if deep_link:
        return f"{base}/#{deep_link}"
    return base


def _post_slack(headline, reason_lines, deep_link):
    """Post one alert to the AOP Slack channel via incoming webhook.

    Returns True when Slack accepted it. Fail-silent: unset webhook or any
    transport error logs a warning and returns False -- the firing stays in
    the DB so the dashboard still shows it.
    """
    webhook = os.environ.get(SLACK_WEBHOOK_ENV)
    if not webhook:
        logger.warning("[alerts] %s: %s unset; skipping Slack post", SLACK_WEBHOOK_ENV, "webhook")
        return False
    text_lines = [f"*{headline}*"]
    text_lines.extend(reason_lines)
    text_lines.append(f"<{_dashboard_link(deep_link)}|Open the dashboard>")
    payload = json.dumps({"text": "\n".join(text_lines)}).encode()
    req = urllib.request.Request(
        webhook, data=payload, method="POST",
        headers={"Content-Type": "application/json"},
    )
    try:
        with urllib.request.urlopen(req, timeout=SLACK_TIMEOUT_S) as resp:
            ok = 200 <= resp.status < 300
            if not ok:
                logger.warning("[alerts] Slack webhook returned HTTP %s", resp.status)
            return ok
    except Exception as exc:  # noqa: BLE001 -- fail-silent by design
        logger.warning("[alerts] Slack webhook post failed: %s", exc)
        return False


def _maybe_reset_month(month_start):
    """Reset breached flags once per calendar month so a new month of
    continued breach counts as a new episode (monthly signals would otherwise
    fire once and stay silent forever)."""
    label = month_start.strftime("%Y-%m")
    pref, _ = OperatorPreference.objects.get_or_create(
        key=_LAST_EVAL_MONTH_KEY, defaults={"value": {"month": label}}
    )
    last = (pref.value or {}).get("month")
    if last != label:
        pref.value = {"month": label}
        pref.save(update_fields=["value", "updated_at"])
        reset = AlertRule.objects.filter(breached=True).update(breached=False)
        if reset:
            logger.info("[alerts] month rollover %s -> %s; reset %d breached flags",
                         last, label, reset)


def evaluate_all_rules():
    """Evaluate every enabled rule for the current month. Returns a summary
    dict. Called by the `evaluate_alerts` management command (hourly cron)."""
    month_start = current_month_start()
    _maybe_reset_month(month_start)
    now = timezone.now()
    summary = {"month": month_start.strftime("%Y-%m"), "evaluated": 0, "fired": 0, "skipped": 0}
    for rule in AlertRule.objects.filter(enabled=True).order_by("id"):
        summary["evaluated"] += 1
        try:
            breached, value, headline, evidence = evaluate_rule(rule, month_start)
        except Exception as exc:  # noqa: BLE001 -- one bad rule must not kill the cron run
            logger.warning("[alerts] rule=%s evaluation failed: %s", rule.id, exc)
            summary["skipped"] += 1
            continue
        was_breached = rule.breached
        if rule.breached != breached:
            rule.breached = breached
            rule.save(update_fields=["breached"])
        if not breached or was_breached:
            continue
        # Rising edge: check the cooldown before firing.
        last = rule.firings.order_by("-fired_at").first()
        if last and (now - last.fired_at) < timedelta(hours=rule.cooldown_hours):
            logger.info(
                "[alerts] rule=%s rising edge suppressed by cooldown (last fired %s)",
                rule.id, last.fired_at.isoformat(),
            )
            continue
        firing = AlertFiring.objects.create(
            rule=rule, metric_value=value, headline=headline or rule.name,
            evidence=evidence,
        )
        deep_link = _rule_deep_link(rule)
        reason = _reason_lines(rule, value, evidence)
        _post_slack(firing.headline, reason, deep_link)
        logger.info(
            "[alerts] alert_fired rule=%s type=%s value=%s headline=%s",
            rule.id, rule.rule_type, value, firing.headline,
        )
        summary["fired"] += 1
    return summary


def _rule_deep_link(rule):
    return {
        "cost_per_conversation": "panel-cost-commentary",
        "spend_anomaly": "panel-cost-reconciliation",
        "cache_hit_rate_drop": "panel-cache-economics",
        "eval_score_drop": "panel-quality-themes",
    }.get(rule.rule_type)


def _reason_lines(rule, value, evidence):
    meta = RULE_META.get(rule.rule_type, {})
    lines = [f"Rule: {rule.name}"]
    if value is not None:
        lines.append(f"Observed: `{value}` vs threshold `{rule.threshold:g}` ({meta.get('unit', '')})".rstrip())
    for ev in evidence or []:
        if isinstance(ev, dict) and ev.get("value") is not None:
            lines.append(f"• `{ev['metric']}` = `{ev['value']}` ({ev.get('source', '')})")
    return lines
