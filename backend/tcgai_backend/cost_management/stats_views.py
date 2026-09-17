"""Month-scoped cost / token / engagement stats for the home dashboard."""
import calendar
import os

from django.contrib.auth.decorators import login_required
from django.core.cache import cache
from django.db.models import Avg, Count
from django.db.models.functions import TruncDate
from django.http import JsonResponse
from django.utils import timezone

from . import views as base_views
from .llm_provider_adapter_implementations import app_api_key_ids
from .models import Chat
from .month_utils import current_month_start, month_range, parse_month_param, prev_month, real_chats

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
    """(input, output, [{day, input, output}], cache_info, error) from the
    Anthropic usage report. `input` is the TRUE total (uncached + cache
    creation + cache read, per AnthropicAdapter.get_tokens -- ENG-148),
    not just uncached input as before. cache_info is {creation_tokens,
    read_tokens, hit_rate} -- defaults to all-zero/None if the adapter
    response doesn't carry a "cache" key at all (an older/unpatched adapter,
    or an error response), so callers never need a None-check of their own."""
    resp = base_views.llmprovider.get_tokens(year=month_start.year, month=month_start.month)
    if not isinstance(resp, dict) or resp.get("error"):
        err = resp.get("error") if isinstance(resp, dict) else "usage source unavailable"
        return None, None, [], {"creation_tokens": 0, "read_tokens": 0, "hit_rate": None}, err
    rows = resp.get("tokens") or []
    total_in = sum(int(r.get("input_tokens") or 0) for r in rows)
    total_out = sum(int(r.get("output_tokens") or 0) for r in rows)
    daily = [
        {"day": r.get("day"), "input": int(r.get("input_tokens") or 0),
         "output": int(r.get("output_tokens") or 0)}
        for r in rows
    ]
    cache_info = resp.get("cache") or {}
    cache_info = {
        "creation_tokens": cache_info.get("creation_tokens", 0),
        "read_tokens": cache_info.get("read_tokens", 0),
        "hit_rate": cache_info.get("hit_rate"),
    }
    return total_in, total_out, daily, cache_info, None


def _chat_qs(month_start):
    start_dt, end_dt = month_range(month_start)
    return real_chats(Chat.objects.filter(timestamp__gte=start_dt, timestamp__lt=end_dt))


def _daily_counts(month_start):
    """Per-day conversation counts, split real vs. automated/bot (ENG-149/150
    -- see month_utils.real_chats). `count` is real-only, same population as
    the "Conversations" KPI/busiest-day logic below -- `bot_count` is purely
    additive so the stacked-bar chart can show both without changing what
    counts as a "real" conversation anywhere else."""
    start_dt, end_dt = month_range(month_start)
    rows = (
        Chat.objects.filter(timestamp__gte=start_dt, timestamp__lt=end_dt)
        .annotate(day=TruncDate("timestamp"))
        .values("day", "likely_automated")
        .annotate(count=Count("chat_id"))
        .order_by("day")
    )
    by_day = {}
    for r in rows:
        day = r["day"].isoformat()
        entry = by_day.setdefault(day, {"day": day, "count": 0, "bot_count": 0})
        if r["likely_automated"]:
            entry["bot_count"] += r["count"]
        else:
            entry["count"] += r["count"]
    return [by_day[day] for day in sorted(by_day)]


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
    tok_in, tok_out, tok_daily, cache_info, tok_err = _tokens_for(month_start)
    prev_in, prev_out, _, _, _ = _tokens_for(previous)

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
        "workspace_id": os.environ.get('ANTHROPIC_WORKSPACE_ID') or None,
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
            # ENG-148: "input" above already includes cache tokens (see
            # _tokens_for) -- these are the breakdown + the actual "how well
            # is caching doing" metric the store owner asked to see. A high
            # hit_rate means most input tokens are billed at Anthropic's
            # cache-read discount rather than full price -- caching reduces
            # cost, it does not make those calls free.
            "cache_creation": cache_info["creation_tokens"],
            "cache_read": cache_info["read_tokens"],
            "cache_hit_rate": cache_info["hit_rate"],
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


@login_required
def model_rates(request):
    """Effective $/token rate per model this month, straight from Anthropic's
    own billing data (see AnthropicAdapter.get_model_rates) - not a hardcoded
    price table, so it stays correct if Anthropic changes prices."""
    refresh = request.GET.get("refresh", "").lower() in ("1", "true", "yes")
    month_param = request.GET.get("month")
    current = current_month_start()

    month_start = current
    if month_param:
        parsed = parse_month_param(month_param)
        if parsed is None:
            return JsonResponse({"error": "invalid month; expected YYYY-MM"}, status=400)
        month_start = parsed

    key = f"model_rates:{month_start:%Y-%m}"
    if not refresh:
        cached = cache.get(key)
        if cached is not None:
            return JsonResponse({**cached, "cached": True})

    payload = base_views.llmprovider.get_model_rates(year=month_start.year, month=month_start.month)
    cache.set(key, payload, CURRENT_TTL if month_start == current else PAST_TTL)
    return JsonResponse(payload)


@login_required
def usage_by_key(request):
    """Per-API-key token usage + an estimated $ cost this month, combining
    AnthropicAdapter.get_usage_by_key (real per-key token counts) with
    get_model_rates (this month's real effective $/token per model, from
    model_rates above). The per-key dollar figure is necessarily an
    estimate -- Anthropic's cost_report has no per-key breakdown to derive a
    real billed figure from (see get_usage_by_key's docstring) -- so this
    multiplies each key's per-model token counts by that model's blended
    rate rather than reporting Anthropic's own billed cost per key. Each
    token type (uncached input, output, cache creation, cache read) is
    priced at its own rate -- lumping cache tokens into the plain input/
    output rate (or dropping them) would misprice every cache-hit turn and
    make this panel's rows unable to sum to the "Anthropic spend" KPI,
    which does include cache costs (see get_cost).

    Unlike get_usage_by_key itself (deliberately unscoped -- it exists to
    show every key with usage, expected or not), this VIEW filters its
    output down to ANTHROPIC_APP_API_KEY_IDS when set, the same key-id list
    that already scopes the "Anthropic spend"/"Tokens" KPIs -- so this
    panel reads as "usage by *our* keys" rather than every key in the org,
    and its rows sum to the same spend total shown elsewhere on the page."""
    refresh = request.GET.get("refresh", "").lower() in ("1", "true", "yes")
    month_param = request.GET.get("month")
    current = current_month_start()

    month_start = current
    if month_param:
        parsed = parse_month_param(month_param)
        if parsed is None:
            return JsonResponse({"error": "invalid month; expected YYYY-MM"}, status=400)
        month_start = parsed

    key = f"usage_by_key:{month_start:%Y-%m}"
    if not refresh:
        cached = cache.get(key)
        if cached is not None:
            return JsonResponse({**cached, "cached": True})

    usage_resp = base_views.llmprovider.get_usage_by_key(year=month_start.year, month=month_start.month)
    if not isinstance(usage_resp, dict) or usage_resp.get("error"):
        err = usage_resp.get("error") if isinstance(usage_resp, dict) else "usage source unavailable"
        payload = {"keys": [], "workspace_id": None, "estimated": True, "error": err}
        cache.set(key, payload, CURRENT_TTL if month_start == current else PAST_TTL)
        return JsonResponse(payload)

    rates_resp = base_views.llmprovider.get_model_rates(year=month_start.year, month=month_start.month)
    rates = rates_resp.get("rates", {}) if isinstance(rates_resp, dict) else {}

    allowed_ids = set(app_api_key_ids())
    raw_keys = usage_resp.get("keys", [])
    if allowed_ids:
        raw_keys = [k for k in raw_keys if k["api_key_id"] in allowed_ids]

    keys = []
    for k in raw_keys:
        estimated_cost = 0.0
        for model, tok in k.get("by_model", {}).items():
            rate = rates.get(model, {})
            estimated_cost += tok.get("uncached_input_tokens", 0) * rate.get("input", 0)
            estimated_cost += tok.get("output_tokens", 0) * rate.get("output", 0)
            estimated_cost += tok.get("cache_creation_tokens", 0) * rate.get("cache_creation", 0)
            estimated_cost += tok.get("cache_read_tokens", 0) * rate.get("cache_read", 0)
        keys.append({
            "api_key_id": k["api_key_id"],
            "name": k["name"],
            "input_tokens": k["input_tokens"],
            "output_tokens": k["output_tokens"],
            "estimated_cost": round(estimated_cost, 2),
        })
    keys.sort(key=lambda k: k["estimated_cost"], reverse=True)

    payload = {
        "keys": keys,
        "workspace_id": usage_resp.get("workspace_id"),
        "estimated": True,
    }
    cache.set(key, payload, CURRENT_TTL if month_start == current else PAST_TTL)
    return JsonResponse(payload)
