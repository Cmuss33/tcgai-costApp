"""Month-scoped cost / token / engagement stats for the home dashboard."""
import calendar

from django.contrib.auth.decorators import login_required
from django.core.cache import cache
from django.db.models import Avg, Count
from django.db.models.functions import TruncDate
from django.http import JsonResponse
from django.utils import timezone

from . import views as base_views
from .models import Chat
from .month_utils import current_month_start, month_range, parse_month_param, prev_month

CURRENT_TTL = 900       # 15 min — the current month's cost figures still move
PAST_TTL = 86400        # a day — past months are effectively fixed


def _pct_delta(current, previous):
    if not previous or current is None:
        return None
    return round((current - previous) / previous * 100, 1)


def _spend_for(month_start):
    """(total_usd, [{day, amount}], error) from the Anthropic cost report."""
    resp = base_views.llmprovider.get_cost(year=month_start.year, month=month_start.month)
    if not isinstance(resp, dict) or resp.get("error"):
        err = resp.get("error") if isinstance(resp, dict) else "cost source unavailable"
        return None, [], err
    costs = resp.get("costs") or []
    total = round(sum(float(c.get("total_cost") or 0) for c in costs), 2)
    daily = [
        {"day": c.get("day"), "amount": round(float(c.get("total_cost") or 0), 2)}
        for c in costs
    ]
    return total, daily, None


def _tokens_for(month_start):
    """(input, output, [{day, input, output}], error) from the Anthropic usage report."""
    resp = base_views.llmprovider.get_tokens(year=month_start.year, month=month_start.month)
    if not isinstance(resp, dict) or resp.get("error"):
        err = resp.get("error") if isinstance(resp, dict) else "usage source unavailable"
        return None, None, [], err
    rows = resp.get("tokens") or []
    total_in = sum(int(r.get("input_tokens") or 0) for r in rows)
    total_out = sum(int(r.get("output_tokens") or 0) for r in rows)
    daily = [
        {"day": r.get("day"), "input": int(r.get("input_tokens") or 0),
         "output": int(r.get("output_tokens") or 0)}
        for r in rows
    ]
    return total_in, total_out, daily, None


def _chat_qs(month_start):
    start_dt, end_dt = month_range(month_start)
    return Chat.objects.filter(timestamp__gte=start_dt, timestamp__lt=end_dt)


def _daily_counts(month_start):
    rows = (
        _chat_qs(month_start)
        .annotate(day=TruncDate("timestamp"))
        .values("day")
        .annotate(count=Count("chat_id"))
        .order_by("day")
    )
    return [{"day": r["day"].isoformat(), "count": r["count"]} for r in rows]


def _daily_mean(qs, field):
    """Unweighted mean of daily means for a Chat numeric field (mirrors get_avg_*)."""
    rows = (
        qs.annotate(day=TruncDate("timestamp")).values("day").annotate(v=Avg(field))
    )
    vals = [r["v"] for r in rows if r["v"] is not None]
    return round(sum(vals) / len(vals), 1) if vals else 0.0


def _eval_avg(month_start):
    qs = _chat_qs(month_start).filter(evaluation_score__isnull=False)
    rows = qs.annotate(day=TruncDate("timestamp")).values("day").annotate(v=Avg("evaluation_score"))
    vals = [r["v"] for r in rows if r["v"] is not None]
    return round(sum(vals) / len(vals), 1) if vals else None


def _build_stats(month_start):
    current = current_month_start()
    is_current = month_start == current
    previous = prev_month(month_start)
    label = month_start.strftime("%Y-%m")

    spend, spend_daily, cost_err = _spend_for(month_start)
    prev_spend, _, _ = _spend_for(previous)
    tok_in, tok_out, tok_daily, tok_err = _tokens_for(month_start)
    prev_in, prev_out, _, _ = _tokens_for(previous)

    convs = _chat_qs(month_start).count()
    prev_convs = _chat_qs(previous).count()
    daily_counts = _daily_counts(month_start)
    busiest = max(daily_counts, key=lambda d: d["count"], default=None)

    days_in_month = calendar.monthrange(month_start.year, month_start.month)[1]
    days_elapsed = timezone.now().day if is_current else days_in_month
    per_day_avg = round(convs / days_elapsed, 1) if days_elapsed else 0.0

    eval_avg = _eval_avg(month_start)
    prev_eval = _eval_avg(previous)
    scored = _chat_qs(month_start).filter(evaluation_score__isnull=False).count()

    in_pc = _daily_mean(_chat_qs(month_start), "tokens_in")
    out_pc = _daily_mean(_chat_qs(month_start), "tokens_out")
    prev_in_pc = _daily_mean(_chat_qs(previous), "tokens_in")
    prev_out_pc = _daily_mean(_chat_qs(previous), "tokens_out")

    cost_pc = round(spend / convs, 4) if (spend is not None and convs) else None
    prev_cost_pc = round(prev_spend / prev_convs, 4) if (prev_spend is not None and prev_convs) else None

    projected = None
    if is_current and spend is not None and timezone.now().day:
        projected = round(spend / timezone.now().day * days_in_month, 2)

    model_mix = [
        {
            "model": row["model"] or "unknown",
            "conversations": row["c"],
            "share_pct": round(row["c"] / convs * 100, 1) if convs else 0.0,
        }
        for row in _chat_qs(month_start).values("model").annotate(c=Count("chat_id")).order_by("-c")
    ]

    return {
        "month": label,
        "is_current": is_current,
        "generated_at": timezone.now().isoformat(),
        "currency": "USD",
        "cost_source_error": cost_err or tok_err,
        "spend": {
            "total": spend,
            "prev_total": prev_spend,
            "delta_pct": _pct_delta(spend, prev_spend),
            "projected_month_end": projected,
            "daily": spend_daily,
        },
        "tokens": {
            "input": tok_in,
            "output": tok_out,
            "prev_input": prev_in,
            "prev_output": prev_out,
            "input_delta_pct": _pct_delta(tok_in, prev_in),
            "output_delta_pct": _pct_delta(tok_out, prev_out),
            "daily": tok_daily,
        },
        "conversations": {
            "total": convs,
            "prev_total": prev_convs,
            "delta_pct": _pct_delta(convs, prev_convs),
            "per_day_avg": per_day_avg,
            "busiest": busiest,
            "daily": daily_counts,
        },
        "eval_score": {
            "avg": eval_avg,
            "prev_avg": prev_eval,
            "delta_pct": _pct_delta(eval_avg, prev_eval),
            "scored": scored,
            "total": convs,
            "coverage_pct": round(scored / convs * 100, 1) if convs else 0.0,
        },
        "per_conversation": {
            "tokens_in": in_pc,
            "tokens_out": out_pc,
            "prev_tokens_in": prev_in_pc,
            "prev_tokens_out": prev_out_pc,
            "tokens_in_delta_pct": _pct_delta(in_pc, prev_in_pc),
            "tokens_out_delta_pct": _pct_delta(out_pc, prev_out_pc),
            "cost": cost_pc,
            "prev_cost": prev_cost_pc,
            "cost_delta_pct": _pct_delta(cost_pc, prev_cost_pc),
        },
        "model_mix": model_mix,
    }


@login_required
def monthly_stats(request):
    refresh = request.GET.get("refresh", "").lower() in ("1", "true", "yes")
    month_param = request.GET.get("month")
    current = current_month_start()

    month_start = current
    if month_param:
        parsed = parse_month_param(month_param)
        if parsed is None:
            return JsonResponse({"error": "invalid month; expected YYYY-MM"}, status=400)
        month_start = parsed

    key = f"monthly_stats:{month_start:%Y-%m}"
    if not refresh:
        cached = cache.get(key)
        if cached is not None:
            return JsonResponse({**cached, "cached": True})

    payload = _build_stats(month_start)
    cache.set(key, payload, CURRENT_TTL if month_start == current else PAST_TTL)
    return JsonResponse(payload)
