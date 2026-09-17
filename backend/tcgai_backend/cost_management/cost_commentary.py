"""Grounded "why did cost move" narrative alongside the monthly Insights
report (Task D). Fed real monthly_stats deltas plus a human-maintained
CostMethodologyChange changelog -- never free speculation about root causes,
the same discipline insights_views.report_insights already applies to
conversation counts (see its _build_prompt's grounding instruction).

Deliberately a separate, cheap Claude Haiku call rather than folded into
report_insights: that call is transcript-heavy (up to 200 conversations,
hour-cached, frozen per month) and about customer behavior; this only needs
the small numeric monthly_stats payload plus changelog rows, so it can
regenerate on monthly_stats' own faster cache cycle and a bug in one can't
regress the other.
"""
import os

from django.core.cache import cache
from django.utils import timezone

from .models import CostMethodologyChange
from .month_utils import current_month_start, month_range, prev_month
from .stats_views import _build_stats

CURRENT_TTL = 900       # matches stats_views' own current-month cache window
PAST_TTL = 86400
COST_COMMENTARY_MODEL = "claude-haiku-4-5-20251001"

_VALID_ASSESSMENTS = {
    "real_increase", "real_decrease", "measurement_artifact",
    "mixed", "no_significant_change", "insufficient_data",
}

REPORT_COST_COMMENTARY_TOOL = {
    "name": "report_cost_commentary",
    "description": "Explain what's behind this month's cost/conversation movement, grounded only in the provided numbers and changelog.",
    "input_schema": {
        "type": "object",
        "properties": {
            "headline": {"type": "string"},
            "assessment": {
                "type": "string",
                "enum": sorted(_VALID_ASSESSMENTS),
            },
            "drivers": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "type": {
                            "type": "string",
                            "enum": ["measurement_artifact", "real_usage_change", "unexplained"],
                        },
                        "description": {"type": "string"},
                        "changelog_date": {"type": "string"},
                    },
                    "required": ["type", "description"],
                },
            },
        },
        "required": ["headline", "assessment", "drivers"],
    },
}


def _changelog_for_window(month_start):
    """CostMethodologyChange rows dated from the start of the PRIOR month
    (the comparison baseline) through the end of the current one -- wide
    enough to catch a fix that landed early this month or late last month,
    either of which can explain a delta computed between the two."""
    window_start, _ = month_range(prev_month(month_start))
    _, window_end = month_range(month_start)
    return list(
        CostMethodologyChange.objects.filter(
            date__gte=window_start.date(), date__lt=window_end.date()
        ).order_by("date")
    )


def _build_cost_commentary_prompt(stats, changes, month_label):
    """Pure string-building, split out so the grounding instruction can be
    tested without a real API call -- mirrors insights_views._build_prompt's
    pattern of pinning the model to known-correct numbers instead of letting
    it estimate or invent them."""
    spend = stats.get("spend") or {}
    convs = stats.get("conversations") or {}
    pc = stats.get("per_conversation") or {}
    tokens = stats.get("tokens") or {}

    changelog_lines = "\n".join(
        f"- {c.date.isoformat()} [{c.get_category_display()}]: {c.description}"
        for c in changes
    ) or "(none on record for this window)"

    return (
        f"Cost data for {month_label} vs. the prior month, computed directly from "
        "Anthropic's own billing API and this app's database -- these are the exact, "
        "correct figures; never recompute or estimate them yourself:\n\n"
        f"- Spend: ${spend.get('total')} (prior month: ${spend.get('prev_total')}, "
        f"change: {spend.get('delta_pct')}%)\n"
        f"- Conversations: {convs.get('total')} (prior month: {convs.get('prev_total')}, "
        f"change: {convs.get('delta_pct')}%)\n"
        f"- Cost per conversation: ${pc.get('cost')} (prior month: ${pc.get('prev_cost')}, "
        f"change: {pc.get('cost_delta_pct')}%)\n"
        f"- Prompt-cache hit rate this month: {tokens.get('cache_hit_rate')}\n\n"
        "Known changes to the app or its cost-measurement pipeline dated within this "
        "comparison window (from the prior month through this one) -- these are the "
        "ONLY facts you may cite as a cause for a delta:\n"
        f"{changelog_lines}\n\n"
        "Produce, through the report_cost_commentary tool:\n"
        "- assessment: your overall read of the cost-per-conversation change.\n"
        "- drivers: one entry per factor you believe contributed. Each MUST be either "
        "(a) backed by one of the changelog entries above -- cite its date in "
        "changelog_date and mark it \"measurement_artifact\" if its category is "
        "measurement_fix/config_change/incident, or \"real_usage_change\" if "
        "new_feature -- or (b) directly derivable from the numbers alone (e.g. "
        "conversation volume itself changed a lot), marked \"unexplained\" if you "
        "cannot point to a specific cause. NEVER invent a cause with no changelog "
        "entry or number behind it -- if the delta is large and nothing in the "
        "changelog or numbers explains it, say so plainly as \"unexplained\" rather "
        "than guessing.\n"
        "- headline: one or two plain sentences. State whether this reflects a real "
        "spend change or is substantially explained by known measurement changes; "
        "cite the single most important driver."
    )


def _sanitize_commentary(core):
    """Mirrors insights_views._sanitize_report's discipline: a malformed
    tool call (a field emitted as the wrong shape) must degrade to an empty/
    safe value rather than flow through to the frontend and crash it."""
    core = dict(core)
    if not isinstance(core.get("drivers"), list) or not all(isinstance(d, dict) for d in core["drivers"]):
        core["drivers"] = []
    if not isinstance(core.get("headline"), str):
        core["headline"] = ""
    if core.get("assessment") not in _VALID_ASSESSMENTS:
        core["assessment"] = "insufficient_data"
    return core


def _generate_cost_commentary(stats, changes, month_label):
    """Single, cheap Claude Haiku call. Patched out in tests."""
    import anthropic

    system = (
        "You analyze cost/usage data for a customer-support chat app. Report "
        "findings only through the report_cost_commentary tool. Ground every "
        "claim in the numbers and changelog you are given -- never speculate "
        "beyond them."
    )
    prompt = _build_cost_commentary_prompt(stats, changes, month_label)
    client = anthropic.Anthropic(api_key=os.environ.get("ANTHROPIC_API_KEY"))
    message = client.messages.create(
        model=COST_COMMENTARY_MODEL,
        max_tokens=1024,
        system=system,
        tools=[REPORT_COST_COMMENTARY_TOOL],
        tool_choice={"type": "tool", "name": "report_cost_commentary"},
        messages=[{"role": "user", "content": prompt}],
    )
    for block in message.content:
        if getattr(block, "type", None) == "tool_use" and block.name == "report_cost_commentary":
            return block.input
    raise ValueError("model did not return a report_cost_commentary tool call")


def cost_commentary_for(month_start):
    """Entry point insights_views calls. Owns its own cache, independent of
    InsightsSnapshot's freeze -- cost data moves on monthly_stats' own
    cadence (15 min for the current month, a day for past months), not the
    transcript-narrative's hourly/frozen-per-month cycle. Fails silently
    (returns an error/insufficient-data payload, never raises) -- this must
    never break the insights page."""
    is_current = month_start == current_month_start()
    key = f"cost_commentary:{month_start:%Y-%m}"
    cached = cache.get(key)
    if cached is not None:
        return cached

    stats = _build_stats(month_start)
    if stats.get("cost_source_error") or not (stats.get("conversations") or {}).get("total"):
        result = {"insufficient_data": True}
    else:
        try:
            changes = _changelog_for_window(month_start)
            core = _sanitize_commentary(
                _generate_cost_commentary(stats, changes, stats["month"])
            )
            result = {**core, "generated_at": timezone.now().isoformat()}
        except Exception as exc:  # degrade gracefully -- never break the insights page
            result = {"error": str(exc)}

    cache.set(key, result, CURRENT_TTL if is_current else PAST_TTL)
    return result
