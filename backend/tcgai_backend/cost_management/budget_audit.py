"""C6: deterministic advisor budget-compliance audit.

The chatbot's Sales Advisor emits per-run telemetry (stated shopper budget +
picks with prices) to log_advisor_telemetry/. This module audits it
deterministically -- pure functions over stored rows, never LLM-judged.

The rule under audit is the advisor's BUDGET CONSTRAINT RULE: the hero pick
(the first pick, products[0]) must come in at or below the shopper's stated
budget. Over-budget add-on picks are flagged individually as informational --
the constraint governs the hero pick.

A null stated_budget means "unknown" (no budget stated, or the parse was
ambiguous -- the chatbot never guesses). Unknown is never a violation: a
false "non-compliant" is worse than "unknown".
"""

import logging
from decimal import Decimal

from django.core.cache import cache
from django.http import JsonResponse

from .api_auth import api_login_required
from .models import AdvisorTelemetry
from .month_utils import (
    conversation_count,
    current_month_start,
    lifetime_months,
    month_range,
    parse_month_param,
)

logger = logging.getLogger(__name__)


def _dec(value):
    """Coerce to Decimal, or None when missing/invalid. Never raises."""
    if value is None:
        return None
    try:
        return Decimal(str(value))
    except Exception:
        return None


def audit_chat_compliance(picks, stated_budget):
    """Audit one advisor run's picks against the stated budget.

    Pure function: takes the stored picks list and stated budget, returns a
    dict. Hero is picks[0] by advisor convention.

    Returns {
        "budget_known": bool,
        "hero_price": float | None,
        "hero_title": str | None,
        "compliant": bool,            # True when unknown OR hero <= budget
        "over_budget_addons": [       # informational only
            {"title": str, "price": float, "over_by": float}
        ],
    }
    An empty/invalid picks list yields budget_known from the budget alone and
    compliant=True -- nothing to audit is not a violation.
    """
    budget = _dec(stated_budget)
    budget_known = budget is not None

    prices = []
    titles = []
    for p in picks or []:
        if not isinstance(p, dict):
            continue
        price = _dec(p.get("price"))
        if price is None:
            continue
        prices.append(price)
        titles.append(str(p.get("title") or "")[:500])

    if not prices:
        return {
            "budget_known": budget_known,
            "hero_price": None,
            "hero_title": None,
            "compliant": True,
            "over_budget_addons": [],
        }

    hero_price, hero_title = prices[0], titles[0]
    compliant = (not budget_known) or (hero_price <= budget)

    over_budget_addons = []
    if budget_known:
        for price, title in zip(prices[1:], titles[1:]):
            if price > budget:
                over_budget_addons.append(
                    {
                        "title": title,
                        "price": float(price),
                        "over_by": float(price - budget),
                    }
                )

    return {
        "budget_known": budget_known,
        "hero_price": float(hero_price),
        "hero_title": hero_title,
        "compliant": compliant,
        "over_budget_addons": over_budget_addons,
    }


def monthly_budget_audit(month_start, shops=None):
    """Aggregate budget-compliance numbers for one month of telemetry.

    Rows are scoped by created_at (arrival month). Distinct chats are counted
    by chat_id_raw so unlinked rows (no Chat row) still count toward coverage.
    """
    start_dt, end_dt = month_range(month_start)
    telemetry_qs = AdvisorTelemetry.objects.filter(
        created_at__gte=start_dt, created_at__lt=end_dt
    )
    if shops:
        telemetry_qs = telemetry_qs.filter(shop__in=list(shops))
    rows = list(telemetry_qs.order_by("created_at"))

    # One row per chat: keep the latest emit per (chat_id_raw, surface).
    latest = {}
    for r in rows:
        latest[(r.chat_id_raw, r.surface)] = r
    chats = list(latest.values())

    telemetry_chats = len(chats)
    stated = [c for c in chats if c.stated_budget is not None]
    budget_stated_chats = len(stated)

    violations = []
    for c in stated:
        audit = audit_chat_compliance(c.picks, c.stated_budget)
        if not audit["compliant"]:
            violations.append(
                {
                    "chat_id": c.chat_id_raw,
                    "surface": c.surface,
                    "stated_budget": float(c.stated_budget),
                    "hero_price": audit["hero_price"],
                    "hero_title": audit["hero_title"],
                    "over_by": round(audit["hero_price"] - float(c.stated_budget), 2),
                }
            )
    violations.sort(key=lambda v: v["over_by"], reverse=True)

    non_compliant_chats = len(violations)
    compliance_rate = (
        round((budget_stated_chats - non_compliant_chats) / budget_stated_chats * 100, 1)
        if budget_stated_chats
        else None
    )

    total_convos = conversation_count(month_start, shops)
    emit_coverage_pct = (
        round(telemetry_chats / total_convos * 100, 1) if total_convos else None
    )
    unlinked_telemetry = sum(1 for c in chats if c.chat_id is None)

    return {
        "month": month_start.strftime("%Y-%m"),
        "telemetry_chats": telemetry_chats,
        "budget_stated_chats": budget_stated_chats,
        "non_compliant_chats": non_compliant_chats,
        "compliance_rate": compliance_rate,
        "violations": violations[:10],
        "violations_total": non_compliant_chats,
        "emit_coverage_pct": emit_coverage_pct,
        "total_conversations": total_convos,
        "unlinked_telemetry": unlinked_telemetry,
    }


@api_login_required
def budget_audit(request):
    """C6 budget-compliance audit for the dashboard's C2 mission.

    Logged-in dashboard endpoint (same pattern as verdicts/): ?month=YYYY-MM.
    Cached per month; refresh=1 bypasses.
    """
    refresh = request.GET.get("refresh", "").lower() in ("1", "true", "yes")
    month_param = request.GET.get("month")
    shops = request.GET.getlist("shop") or None

    if month_param == "lifetime":
        key = "budget_audit:lifetime"
        if shops:
            key += ":shop=" + ",".join(sorted(shops))
        if not refresh:
            cached = cache.get(key)
            if cached is not None:
                return JsonResponse(cached)
        # Lifetime: aggregate monthly audits.
        total_telemetry = 0
        total_stated = 0
        total_non_compliant = 0
        total_convos = 0
        total_unlinked = 0
        all_violations = []
        for m in lifetime_months():
            ma = monthly_budget_audit(m, shops)
            total_telemetry += ma["telemetry_chats"]
            total_stated += ma["budget_stated_chats"]
            total_non_compliant += ma["non_compliant_chats"]
            total_convos += ma["total_conversations"] or 0
            total_unlinked += ma["unlinked_telemetry"]
            all_violations.extend(ma["violations"])
        all_violations.sort(key=lambda v: v["over_by"], reverse=True)
        payload = {
            "month": "lifetime",
            "is_lifetime": True,
            "telemetry_chats": total_telemetry,
            "budget_stated_chats": total_stated,
            "non_compliant_chats": total_non_compliant,
            "compliance_rate": (
                round((total_stated - total_non_compliant) / total_stated * 100, 1)
                if total_stated
                else None
            ),
            "violations": all_violations[:10],
            "violations_total": total_non_compliant,
            "emit_coverage_pct": (
                round(total_telemetry / total_convos * 100, 1) if total_convos else None
            ),
            "total_conversations": total_convos,
            "unlinked_telemetry": total_unlinked,
            "shop_filtered": bool(shops),
        }
        cache.set(key, payload, 300)
        return JsonResponse(payload)

    current = current_month_start()

    month_start = current
    if month_param:
        parsed = parse_month_param(month_param)
        if parsed is None:
            return JsonResponse({"error": "invalid month; expected YYYY-MM"}, status=400)
        month_start = parsed

    key = f"budget_audit:{month_start:%Y-%m}"
    if shops:
        key += ":shop=" + ",".join(sorted(shops))
    if refresh:
        cache.delete(key)
    else:
        cached = cache.get(key)
        if cached is not None:
            return JsonResponse(cached)

    payload = monthly_budget_audit(month_start, shops)
    payload["shop_filtered"] = bool(shops)
    cache.set(key, payload, 300)
    return JsonResponse(payload)
