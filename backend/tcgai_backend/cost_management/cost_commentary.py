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
import math
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

# C1: every driver carries its evidence as structured citations, not just a
# free-text changelog_date. Two citation kinds:
#   {"kind": "changelog", "date": "2026-09-16", "category": "measurement_fix"}
#       -- one of the CostMethodologyChange rows listed in the prompt; the
#       date must exactly match a listed entry.
#   {"kind": "stat", "metric": "spend.total", "value": 15.0}
#       -- one of the dashboard numbers in the prompt; the value must exactly
#       match the figure given.
# An empty citations list means "no known cause" -- honest, and preferable
# to an invented citation. _verify_citations checks every citation
# deterministically (no LLM involved) and marks each driver cited/uncited.
_VALID_CITATION_KINDS = {"changelog", "stat"}

# Metric name -> (stats section, key). The single source of truth: both the
# prompt's citable-figures table (_stat_metrics_table) and the deterministic
# verifier (_verify_citations) resolve through these paths.
_STAT_METRIC_PATHS = {
    "spend.total": ("spend", "total"),
    "spend.prev_total": ("spend", "prev_total"),
    "spend.delta_pct": ("spend", "delta_pct"),
    "conversations.total": ("conversations", "total"),
    "conversations.prev_total": ("conversations", "prev_total"),
    "conversations.delta_pct": ("conversations", "delta_pct"),
    "per_conversation.cost": ("per_conversation", "cost"),
    "per_conversation.prev_cost": ("per_conversation", "prev_cost"),
    "per_conversation.cost_delta_pct": ("per_conversation", "cost_delta_pct"),
    "tokens.cache_hit_rate": ("tokens", "cache_hit_rate"),
}

# Backwards-compat alias (the name used during the C1 build).
_STAT_METRICS = _STAT_METRIC_PATHS


def _stat_metrics_table(stats):
    """The exact dashboard figures the model may cite, keyed by the metric
    names the citations schema accepts. The prompt prints this table so the
    model cites names and values it can copy verbatim instead of inventing
    its own."""
    table = {}
    for name, (section, key) in _STAT_METRIC_PATHS.items():
        table[name] = (stats.get(section) or {}).get(key)
    return table

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
                        "citations": {
                            "type": "array",
                            "description": (
                                "One entry per piece of evidence behind this driver. "
                                '{"kind": "changelog", "date": "<exact ISO date from the changelog list>", '
                                '"category": "<its category>"} or '
                                '{"kind": "stat", "metric": "<one of spend.total, spend.prev_total, '
                                "spend.delta_pct, conversations.total, conversations.prev_total, "
                                "conversations.delta_pct, per_conversation.cost, per_conversation.prev_cost, "
                                'per_conversation.cost_delta_pct, tokens.cache_hit_rate>", "value": <the exact figure>}. '
                                "Leave empty when nothing explains the driver (mark it unexplained) -- "
                                "an empty list is honest, an invented citation is the worst failure here."
                            ),
                            "items": {
                                "type": "object",
                                "properties": {
                                    "kind": {"type": "string", "enum": ["changelog", "stat"]},
                                    "date": {"type": "string"},
                                    "category": {"type": "string"},
                                    "metric": {"type": "string"},
                                    "value": {"type": ["number", "string", "null"]},
                                },
                                "required": ["kind"],
                            },
                        },
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
    it estimate or invent them.

    C1: the numbers are printed as a named metric table and every driver must
    cite its evidence as structured citations (changelog entries and/or table
    metrics). The citations are then checked deterministically by
    _verify_citations -- a citation that doesn't match the real records is
    treated as no evidence at all, so the prompt is explicit that inventing
    one is the worst failure here."""
    table = _stat_metrics_table(stats)

    changelog_lines = "\n".join(
        f"- {c.date.isoformat()} [{c.category}]: {c.description}"
        for c in changes
    ) or "(none on record for this window)"
    metrics_lines = "\n".join(
        f"- {name} = {value!r}" for name, value in table.items()
    )

    return (
        f"Cost data for {month_label} vs. the prior month, computed directly from "
        "Anthropic's own billing API and this app's database -- these are the exact, "
        "correct figures; never recompute or estimate them yourself:\n\n"
        "Exact dashboard figures. Cite these with the metric name and the exact "
        "value shown -- e.g. {\"kind\": \"stat\", \"metric\": \"spend.total\", "
        "\"value\": 12.5}:\n"
        f"{metrics_lines}\n\n"
        "Known changes to the app or its cost-measurement pipeline dated within this "
        "comparison window (from the prior month through this one) -- these are the "
        "ONLY facts you may cite as a cause for a delta:\n"
        f"{changelog_lines}\n\n"
        "Produce, through the report_cost_commentary tool:\n"
        "- assessment: your overall read of the cost-per-conversation change.\n"
        "- drivers: one entry per factor you believe contributed. Every driver MUST "
        "carry a \"citations\" array -- one entry per piece of evidence behind it:\n"
        "  * {\"kind\": \"changelog\", \"date\": \"<the exact ISO date as listed above>\", "
        "\"category\": \"<its category as listed>\"} -- for causes backed by a "
        "changelog entry. Mark the driver \"measurement_artifact\" when the cited "
        "entry's category is measurement_fix/config_change/incident, or "
        "\"real_usage_change\" when it is new_feature.\n"
        "  * {\"kind\": \"stat\", \"metric\": \"<one of the metric names above>\", "
        "\"value\": <the exact figure>} -- for drivers directly derivable from the "
        "numbers alone (e.g. conversation volume itself moved a lot).\n"
        "- A driver with no evidence behind it gets an EMPTY citations array and "
        "type \"unexplained\" -- that is honest and expected. NEVER invent a "
        "changelog date, category, metric, or value: every citation is checked "
        "against the real records, and a citation that doesn't match is treated "
        "as no evidence at all.\n"
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
    for d in core["drivers"]:
        # C1: citations must be a list of objects; anything else degrades to [].
        if not isinstance(d.get("citations"), list):
            d["citations"] = []
        else:
            d["citations"] = [c for c in d["citations"] if isinstance(c, dict)]
    if not isinstance(core.get("headline"), str):
        core["headline"] = ""
    if core.get("assessment") not in _VALID_ASSESSMENTS:
        core["assessment"] = "insufficient_data"
    return core


def _verify_citations(core, stats, changes):
    """Deterministic, no-LLM check of every driver citation against the real
    records (C1: no evidence -> no claim).

    - A changelog citation is valid only if its date exactly matches a
      CostMethodologyChange row in the comparison window AND its category
      matches that row's category.
    - A stat citation is valid only if its metric is a known dashboard metric
      AND its value exactly matches the figure the dashboard computed
      (numeric comparison with a tiny tolerance for float rendering).
    - Invalid citations are dropped. A driver left with no valid citation
      whose type isn't already "unexplained" is downgraded to "unexplained" --
      the observation may stand, but the claimed cause is unverified.
    - Backward compat: the pre-C1 schema carried a free-text changelog_date;
      when a driver has no citations but its changelog_date matches a real
      row, it's converted into an equivalent changelog citation instead of
      being discarded.

    Also stamps claims_total / claims_cited / grounding_rate coverage counts
    so the frontend can show how much of the narrative is evidence-backed."""
    table = _stat_metrics_table(stats)
    changelog_by_date = {}
    for c in changes:
        changelog_by_date.setdefault(c.date.isoformat(), c.category)

    def _citation_valid(cit):
        if not isinstance(cit, dict):
            return False
        kind = cit.get("kind")
        if kind not in _VALID_CITATION_KINDS:
            return False
        if kind == "changelog":
            date = cit.get("date")
            return (
                isinstance(date, str)
                and date in changelog_by_date
                and cit.get("category") == changelog_by_date[date]
            )
        # kind == "stat"
        metric = cit.get("metric")
        if metric not in table:
            return False
        expected, value = table[metric], cit.get("value")
        if isinstance(expected, bool) or isinstance(value, bool):
            return expected == value
        if isinstance(expected, (int, float)) and isinstance(value, (int, float)):
            return math.isclose(expected, value, rel_tol=1e-9, abs_tol=1e-9)
        return expected == value

    core = dict(core)
    drivers = []
    for d in core.get("drivers") or []:
        if not isinstance(d, dict):
            continue
        d = dict(d)
        citations = d.get("citations")
        if not isinstance(citations, list):
            citations = []
        if not citations and isinstance(d.get("changelog_date"), str):
            date = d["changelog_date"]
            if date in changelog_by_date:
                citations = [{
                    "kind": "changelog",
                    "date": date,
                    "category": changelog_by_date[date],
                }]
        valid = [c for c in citations if _citation_valid(c)]
        d["citations"] = valid
        d["cited"] = bool(valid)
        if not valid and d.get("type") != "unexplained":
            d["type"] = "unexplained"
        drivers.append(d)
    core["drivers"] = drivers
    core["claims_total"] = len(drivers)
    core["claims_cited"] = sum(1 for d in drivers if d["cited"])
    core["grounding_rate"] = (
        round(core["claims_cited"] / core["claims_total"], 3) if drivers else None
    )
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
            core = _verify_citations(
                _sanitize_commentary(
                    _generate_cost_commentary(stats, changes, stats["month"])
                ),
                stats,
                changes,
            )
            result = {**core, "generated_at": timezone.now().isoformat()}
        except Exception as exc:  # degrade gracefully -- never break the insights page
            result = {"error": str(exc)}

    cache.set(key, result, CURRENT_TTL if is_current else PAST_TTL)
    return result
