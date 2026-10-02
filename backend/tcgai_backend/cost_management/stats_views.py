"""Month-scoped cost / token / engagement stats for the home dashboard."""
import calendar
import os

from .api_auth import api_login_required
from django.core.cache import cache
from django.db.models import Avg, Count, Q, Sum
from django.db.models.functions import TruncDate
from django.http import JsonResponse
from django.utils import timezone

from . import views as base_views
from .llm_provider_adapter_implementations import app_api_key_ids, chat_api_key_ids
from .models import Chat, Message
from .month_utils import (
    CONVERSATION_START_DATE,
    current_month_start,
    lifetime_months,
    month_range,
    parse_month_param,
    prev_month,
    real_chats,
)

CURRENT_TTL = 900       # 15 min — the current month's cost figures still move
PAST_TTL = 86400        # a day — past months are effectively fixed


def _pct_delta(current, previous):
    if not previous or current is None:
        return None
    return round((current - previous) / previous * 100, 1)


def _spend_for(month_start, rates_resp):
    """(total_usd, [{day, amount}], error) from the Anthropic cost report,
    scoped to the chat surface's own key(s) -- see chat_api_key_ids -- so
    AI Search Curator/narrative/report spend can't inflate cost/conversation
    against a denominator that only ever counts chat conversations.

    `rates_resp` (see _rates_resp_for) is this month's already-fetched
    get_model_rates() response, passed straight through to get_cost so it
    doesn't derive its own -- get_cost needs a whole-org rate to estimate
    spend either way, and callers of _spend_for also need that same rate
    for their own proration math, so fetching it once and sharing it avoids
    hitting cost_report+usage_report a second time for the same month."""
    key = f"spend_for:{month_start:%Y-%m}"
    cached = cache.get(key)
    if cached is not None and cached[0] is not None and not cached[2]:
        return cached

    resp = base_views.llmprovider.get_cost(
        year=month_start.year, month=month_start.month, key_ids=chat_api_key_ids(), rates_resp=rates_resp
    )
    if not isinstance(resp, dict) or resp.get("error"):
        err = resp.get("error") if isinstance(resp, dict) else "cost source unavailable"
        return None, [], err
    costs = resp.get("costs") or []
    total = round(sum(float(c.get("total_cost") or 0) for c in costs), 2)
    daily = [
        {"day": c.get("day"), "amount": round(float(c.get("total_cost") or 0), 2)}
        for c in costs
    ]
    result = (total, daily, None)
    current = current_month_start()
    cache.set(key, result, CURRENT_TTL if month_start == current else PAST_TTL)
    return result


def _tokens_for(month_start):
    """(input, output, [{day, input, output}], cache_info, error) from the
    Anthropic usage report. `input` is the TRUE total (uncached + cache
    creation + cache read, per AnthropicAdapter.get_tokens -- ENG-148),
    not just uncached input as before. cache_info is {creation_tokens,
    read_tokens, hit_rate} -- defaults to all-zero/None if the adapter
    response doesn't carry a "cache" key at all (an older/unpatched adapter,
    or an error response), so callers never need a None-check of their own.
    Scoped to the chat surface's own key(s) -- see chat_api_key_ids -- to
    stay consistent with _spend_for above."""
    key = f"tokens_for:{month_start:%Y-%m}"
    cached = cache.get(key)
    if cached is not None and cached[0] is not None and not cached[4]:
        return cached

    resp = base_views.llmprovider.get_tokens(
        year=month_start.year, month=month_start.month, key_ids=chat_api_key_ids()
    )
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
    result = (total_in, total_out, daily, cache_info, None)
    current = current_month_start()
    cache.set(key, result, CURRENT_TTL if month_start == current else PAST_TTL)
    return result


def _rates_resp_for(month_start):
    """Raw get_model_rates() response for the month (whole-org) -- never
    raises, degrading to {"error": ...} on an exception so callers can
    treat "adapter threw" and "adapter returned an error payload" the same
    way. Callers that need to tell a real rate failure apart from "nothing
    priceable this month" (e.g. get_cost, via _spend_for's rates_resp) use
    this directly; _rates_for below is the error-swallowing convenience
    wrapper for callers that already have a fallback."""
    key = f"model_rates:{month_start:%Y-%m}"
    cached = cache.get(key)
    if cached is not None and not cached.get("error") and cached.get("rates"):
        return cached
    try:
        resp = base_views.llmprovider.get_model_rates(year=month_start.year, month=month_start.month)
    except Exception:
        return {"error": "rate derivation failed"}

    if isinstance(resp, dict) and not resp.get("error") and resp.get("rates"):
        current = current_month_start()
        cache.set(key, resp, CURRENT_TTL if month_start == current else PAST_TTL)
    return resp


def _raw_usage_by_key(month_start):
    """Raw get_usage_by_key() response for the month, shared between
    usage_by_key and _chat_cache_buckets so the same upstream usage_report
    and /api_keys calls are not made twice in parallel."""
    key = f"raw_usage_by_key:{month_start:%Y-%m}"
    cached = cache.get(key)
    if cached is not None and not cached.get("error") and "keys" in cached:
        return cached
    try:
        resp = base_views.llmprovider.get_usage_by_key(year=month_start.year, month=month_start.month)
    except Exception:
        return {"error": "usage source unavailable"}

    if isinstance(resp, dict) and not resp.get("error") and "keys" in resp:
        current = current_month_start()
        cache.set(key, resp, CURRENT_TTL if month_start == current else PAST_TTL)
    return resp


def _rates_from_resp(resp):
    """Unwrap a get_model_rates()-shaped response to its rates dict, or {}
    on any error -- the pure part of _rates_for's contract, split out so
    _build_stats/cost_reconciliation can reuse a `resp` they already fetched
    via _rates_resp_for instead of calling get_model_rates a second time."""
    if not isinstance(resp, dict) or resp.get("error"):
        return {}
    return resp.get("rates") or {}


def _rates_for(month_start):
    """This month's effective $/token rate per model, from get_model_rates --
    or {} on any error/exception (a missing rate source must degrade the
    cost/conversation KPI to its unadjusted figure, never break the whole
    dashboard). Whole-org, like get_model_rates itself -- see that
    function's docstring for why it must never be key-scoped.

    On day 1 of the month, Anthropic's daily cost_report hasn't closed yet,
    so current month rates may be empty while token usage has already begun.
    If current month rates are empty, falls back to the previous month's
    rates so token usage is immediately priceable."""
    rates = _rates_from_resp(_rates_resp_for(month_start))
    if not rates and month_start == current_month_start():
        rates = _rates_from_resp(_rates_resp_for(prev_month(month_start)))
    return rates


def _rate_for(rates, model):
    """Longest-prefix match against `rates` -- mirrors the frontend's
    getModelRate (chatSummary/pricing.js). Chat/Message `model` values carry
    a dated snapshot suffix (e.g. "claude-haiku-4-5-20251001") while
    Anthropic's cost/usage reports key rates by a shorter model string; an
    exact dict lookup would silently price a whole model at $0."""
    if not model or not rates:
        return None
    candidates = [k for k in rates if model.startswith(k)]
    if not candidates:
        return None
    return rates[max(candidates, key=len)]


def _logged_spend_split(month_start, rates):
    """{"real": $, "bot": $} -- this month's LOGGED (Chat/Message) token
    counts priced via `rates` and split by likely_automated. Chat.tokens_in/
    tokens_out are the non-cache totals (ENG-148 -- cache tokens are
    additive on Message, not folded into Chat's running total), so they're
    priced at "input"/"output"; Message.cache_creation_tokens/
    cache_read_tokens are priced separately at their own, steeply
    different rates. A model with no entry in `rates` contributes $0 rather
    than erroring -- same "missing rate prices at zero" rule as
    stats_views.usage_by_key."""
    start_dt, end_dt = month_range(month_start)
    real = 0.0
    bot = 0.0

    for row in (
        Chat.objects.filter(timestamp__gte=start_dt, timestamp__lt=end_dt)
        .values("model", "likely_automated")
        .annotate(tin=Sum("tokens_in"), tout=Sum("tokens_out"))
    ):
        rate = _rate_for(rates, row["model"])
        if not rate:
            continue
        cost = (row["tin"] or 0) * rate.get("input", 0) + (row["tout"] or 0) * rate.get("output", 0)
        if row["likely_automated"]:
            bot += cost
        else:
            real += cost

    for row in (
        Message.objects.filter(chat__timestamp__gte=start_dt, chat__timestamp__lt=end_dt)
        .values("model", "chat__likely_automated")
        .annotate(ccreate=Sum("cache_creation_tokens"), cread=Sum("cache_read_tokens"))
    ):
        rate = _rate_for(rates, row["model"])
        if not rate:
            continue
        cost = (row["ccreate"] or 0) * rate.get("cache_creation", 0) + (row["cread"] or 0) * rate.get("cache_read", 0)
        if row["chat__likely_automated"]:
            bot += cost
        else:
            real += cost

    return {"real": real, "bot": bot}


def _bot_spend_share(month_start, rates):
    """Cost-weighted fraction of this month's LOGGED spend attributable to
    likely_automated chats (see _logged_spend_split) -- cost-weighted, not
    token-weighted, because cache reads bill at a steep discount and cache
    writes at a premium; a token-count share would badly mis-state a
    cache-heavy population's true cost share. None -- not 0 -- when there's
    no rate source or nothing priceable was logged this month, so a missing
    rate/model mapping degrades _real_spend_for to the unadjusted billed
    figure instead of silently claiming zero bot spend."""
    if not rates:
        return None
    split = _logged_spend_split(month_start, rates)
    total = split["real"] + split["bot"]
    if not total:
        return None
    return split["bot"] / total


def _real_spend_for(spend, month_start, rates):
    """(prorated_spend, bot_share) -- billed `spend` with the bot's
    cost-weighted share (see _bot_spend_share) removed, so cost_pc's
    numerator reflects what real shoppers' conversations actually cost
    instead of the full billed total (which includes the ENG-149/150 bot's
    traffic -- it hits the same chat API key as real shoppers). Degrades to
    (spend, None) -- the old, unadjusted figure -- when spend is None or
    there's no bot share to apply, rather than guessing."""
    if spend is None:
        return spend, None
    share = _bot_spend_share(month_start, rates)
    if share is None:
        return spend, None
    return spend * (1 - share), share


def _chat_scope_is_app_wide():
    """True when ANTHROPIC_CHAT_API_KEY_IDS isn't set -- chat_api_key_ids()
    then falls back to app_api_key_ids(), so cost_pc's numerator (and
    cost_reconciliation below) still includes AI Search Curator/narrative/
    report spend, not chat traffic alone."""
    raw = os.environ.get('ANTHROPIC_CHAT_API_KEY_IDS') or ''
    return not bool([k.strip() for k in raw.split(',') if k.strip()])


def _chat_cache_buckets(month_start):
    """({model: {uncached_input_tokens, output_tokens, cache_creation_tokens,
    cache_read_tokens}}, error) for the chat surface this month, summed from
    AnthropicAdapter.get_usage_by_key's per-key by_model breakdown.
    get_usage_by_key is deliberately never scoped by itself (it exists to
    show every key with usage) -- the chat_api_key_ids() filter is applied
    here, mirroring how stats_views.usage_by_key filters the same raw
    response to app_api_key_ids() one layer up. Model keys come straight
    from Anthropic's usage report on both sides of this ratio (buckets and,
    via _rates_for, rates), so an exact dict lookup is correct here --
    unlike _rate_for's prefix match, which exists only to bridge our own
    dated Chat.model strings against Anthropic's shorter rate keys."""
    resp = _raw_usage_by_key(month_start)
    if not isinstance(resp, dict) or resp.get("error"):
        err = resp.get("error") if isinstance(resp, dict) else "usage source unavailable"
        return {}, err

    allowed_ids = set(chat_api_key_ids())
    buckets = {}
    for k in resp.get("keys", []):
        if allowed_ids and k.get("api_key_id") not in allowed_ids:
            continue
        for model, tok in k.get("by_model", {}).items():
            agg = buckets.setdefault(model, {
                "uncached_input_tokens": 0, "output_tokens": 0,
                "cache_creation_tokens": 0, "cache_read_tokens": 0,
            })
            agg["uncached_input_tokens"] += tok.get("uncached_input_tokens", 0)
            agg["output_tokens"] += tok.get("output_tokens", 0)
            agg["cache_creation_tokens"] += tok.get("cache_creation_tokens", 0)
            agg["cache_read_tokens"] += tok.get("cache_read_tokens", 0)
    return buckets, None


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

    # Fetched once per month and shared with get_cost (via _spend_for's
    # rates_resp) below instead of each deriving its own -- get_cost needs
    # this month's whole-org rate to estimate spend either way, and the
    # bot-proration step further down needs the same rate again; without
    # sharing it here, a single monthly_stats request used to make this
    # exact cost_report+usage_report pair 3x per month (current & previous).
    rates_resp = _rates_resp_for(month_start)
    prev_rates_resp = _rates_resp_for(previous)
    if is_current and (rates_resp.get("error") or not rates_resp.get("rates")) and prev_rates_resp.get("rates"):
        rates_resp = prev_rates_resp

    spend, spend_daily, cost_err = _spend_for(month_start, rates_resp)
    prev_spend, _, _ = _spend_for(previous, prev_rates_resp)
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

    # Cost-weighted proration: billed spend this month includes the
    # ENG-149/150 bot's own traffic (same chat API key as real shoppers),
    # but `convs` above already excludes it -- dividing raw billed spend by
    # a bot-free denominator overstates cost/conversation by roughly the
    # bot's share of logged spend. See _real_spend_for/_bot_spend_share.
    # Reuses rates_resp/prev_rates_resp fetched above rather than calling
    # get_model_rates again -- _real_spend_for only touches `rates` at all
    # when `spend` isn't None, so this is exactly equivalent to the old
    # "only derive rates when there's a spend figure to prorate" guard.
    rates = _rates_from_resp(rates_resp)
    prev_rates = _rates_from_resp(prev_rates_resp)
    real_spend, bot_share = _real_spend_for(spend, month_start, rates)
    prev_real_spend, _prev_bot_share = _real_spend_for(prev_spend, previous, prev_rates)

    cost_pc = round(real_spend / convs, 4) if (real_spend is not None and convs) else None
    prev_cost_pc = round(prev_real_spend / prev_convs, 4) if (prev_real_spend is not None and prev_convs) else None

    # Retail Labor Substitution calculation:
    # Benchmark: $18.00/hour retail associate, avg conversation = 4 minutes (0.0667 hrs)
    labor_rate = 18.0
    labor_hours = round((convs * 4.0) / 60.0, 1)
    labor_value = round(labor_hours * labor_rate, 2)
    net_savings = round(labor_value - (real_spend if real_spend is not None else 0.0), 2)

    # 24/7 After-hours coverage: inquiries received outside physical store hours (before 10:00 or after 19:00)
    chat_timestamps = list(_chat_qs(month_start).values_list("timestamp", flat=True))
    after_hours_count = sum(1 for ts in chat_timestamps if ts and (ts.hour < 10 or ts.hour >= 19))
    after_hours_pct = round((after_hours_count / convs * 100), 1) if convs else 0.0

    # Low score count: chats this month with evaluation_score < 75
    low_score_count = _chat_qs(month_start).filter(
        evaluation_score__isnull=False, evaluation_score__lt=75
    ).count()

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
        "labor_savings": {
            "labor_rate_hourly": labor_rate,
            "estimated_labor_hours": labor_hours,
            "estimated_labor_value": labor_value,
            "net_savings": net_savings,
            "after_hours_count": after_hours_count,
            "after_hours_pct": after_hours_pct,
        },
        "low_score_count": low_score_count,
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
            # Leave `spend.total` above (the "Anthropic spend" KPI) whole --
            # the bot's cost was real money. Only cost_pc's numerator is
            # adjusted; these three fields show the adjustment itself.
            "billed_spend": spend,
            "spend_excl_bot": round(real_spend, 2) if real_spend is not None else None,
            "bot_share_pct": round(bot_share * 100, 1) if bot_share is not None else None,
        },
        "model_mix": model_mix,
    }



def _build_lifetime_stats():
    months = lifetime_months()

    total_spend = 0.0
    all_spend_daily = []
    cost_err = None
    has_spend = False
    total_real_spend = 0.0

    for m in months:
        rates_resp = _rates_resp_for(m)
        if m == current_month_start() and (rates_resp.get("error") or not rates_resp.get("rates")):
            prev_rates_resp = _rates_resp_for(prev_month(m))
            if prev_rates_resp.get("rates"):
                rates_resp = prev_rates_resp
        m_spend, m_spend_daily, m_err = _spend_for(m, rates_resp)
        if m_err and not cost_err:
            cost_err = m_err
        if m_spend is not None:
            has_spend = True
            total_spend += m_spend
            all_spend_daily.extend(m_spend_daily)
            rates = _rates_from_resp(rates_resp)
            m_real, _ = _real_spend_for(m_spend, m, rates)
            total_real_spend += (m_real if m_real is not None else m_spend)

    spend = round(total_spend, 2) if has_spend else None
    real_spend = round(total_real_spend, 2) if has_spend else None
    all_spend_daily.sort(key=lambda d: d.get("day", ""))

    total_in = 0
    total_out = 0
    all_tok_daily = []
    total_creation = 0
    total_read = 0
    tok_err = None
    has_tokens = False

    for m in months:
        m_in, m_out, m_daily, m_cache, m_err = _tokens_for(m)
        if m_err and not tok_err:
            tok_err = m_err
        if m_in is not None and m_out is not None:
            has_tokens = True
            total_in += m_in
            total_out += m_out
            all_tok_daily.extend(m_daily)
            total_creation += m_cache.get("creation_tokens", 0)
            total_read += m_cache.get("read_tokens", 0)

    tok_in = total_in if has_tokens else None
    tok_out = total_out if has_tokens else None
    cache_hit_rate = round(total_read / total_in, 3) if total_in else None
    all_tok_daily.sort(key=lambda d: d.get("day", ""))

    lifetime_qs = real_chats(Chat.objects.filter(timestamp__gte=CONVERSATION_START_DATE))
    convs = lifetime_qs.count()

    rows = (
        Chat.objects.filter(timestamp__gte=CONVERSATION_START_DATE)
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
    daily_counts = [by_day[day] for day in sorted(by_day)]
    busiest = max(daily_counts, key=lambda d: d["count"], default=None)

    days_elapsed = max(1, (timezone.now().date() - CONVERSATION_START_DATE.date()).days + 1)
    per_day_avg = round(convs / days_elapsed, 1) if days_elapsed else 0.0

    all_up = lifetime_qs.aggregate(
        audited_count=Count('chat_id', filter=Q(evaluation_score__isnull=False)),
        avg_score=Avg('evaluation_score'),
        needs_attention_count=Count('chat_id', filter=Q(evaluation_score__lt=75) | Q(investigation_status="flagged")),
    )
    eval_avg = round(all_up["avg_score"], 1) if all_up["avg_score"] is not None else None
    scored = all_up["audited_count"] or 0
    low_score_count = all_up["needs_attention_count"] or 0

    in_pc = _daily_mean(lifetime_qs, "tokens_in")
    out_pc = _daily_mean(lifetime_qs, "tokens_out")

    cost_pc = round(real_spend / convs, 4) if (real_spend is not None and convs) else None
    bot_share = (spend - real_spend) / spend if (spend and real_spend is not None) else None

    labor_rate = 18.0
    labor_hours = round((convs * 4.0) / 60.0, 1)
    labor_value = round(labor_hours * labor_rate, 2)
    net_savings = round(labor_value - (real_spend if real_spend is not None else 0.0), 2)

    chat_timestamps = list(lifetime_qs.values_list("timestamp", flat=True))
    after_hours_count = sum(1 for ts in chat_timestamps if ts and (ts.hour < 10 or ts.hour >= 19))
    after_hours_pct = round((after_hours_count / convs * 100), 1) if convs else 0.0

    model_mix = [
        {
            "model": row["model"] or "unknown",
            "conversations": row["c"],
            "share_pct": round(row["c"] / convs * 100, 1) if convs else 0.0,
        }
        for row in lifetime_qs.values("model").annotate(c=Count("chat_id")).order_by("-c")
    ]

    return {
        "month": "lifetime",
        "label": "Lifetime (Since June 1, 2026)",
        "is_lifetime": True,
        "is_current": False,
        "generated_at": timezone.now().isoformat(),
        "currency": "USD",
        "workspace_id": os.environ.get('ANTHROPIC_WORKSPACE_ID') or None,
        "cost_source_error": cost_err or tok_err,
        "spend": {
            "total": spend,
            "prev_total": None,
            "delta_pct": None,
            "projected_month_end": None,
            "daily": all_spend_daily,
        },
        "tokens": {
            "input": tok_in,
            "output": tok_out,
            "prev_input": None,
            "prev_output": None,
            "input_delta_pct": None,
            "output_delta_pct": None,
            "daily": all_tok_daily,
            "cache_creation": total_creation,
            "cache_read": total_read,
            "cache_hit_rate": cache_hit_rate,
        },
        "conversations": {
            "total": convs,
            "prev_total": None,
            "delta_pct": None,
            "per_day_avg": per_day_avg,
            "busiest": busiest,
            "daily": daily_counts,
        },
        "eval_score": {
            "avg": eval_avg,
            "prev_avg": None,
            "delta_pct": None,
            "scored": scored,
            "total": convs,
            "coverage_pct": round(scored / convs * 100, 1) if convs else 0.0,
        },
        "labor_savings": {
            "labor_rate_hourly": labor_rate,
            "estimated_labor_hours": labor_hours,
            "estimated_labor_value": labor_value,
            "net_savings": net_savings,
            "after_hours_count": after_hours_count,
            "after_hours_pct": after_hours_pct,
        },
        "low_score_count": low_score_count,
        "per_conversation": {
            "tokens_in": in_pc,
            "tokens_out": out_pc,
            "prev_tokens_in": None,
            "prev_tokens_out": None,
            "tokens_in_delta_pct": None,
            "tokens_out_delta_pct": None,
            "cost": cost_pc,
            "prev_cost": None,
            "cost_delta_pct": None,
            "billed_spend": spend,
            "spend_excl_bot": round(real_spend, 2) if real_spend is not None else None,
            "bot_share_pct": round(bot_share * 100, 1) if bot_share is not None else None,
        },
        "model_mix": model_mix,
    }


@api_login_required
def monthly_stats(request):
    refresh = request.GET.get("refresh", "").lower() in ("1", "true", "yes")
    month_param = request.GET.get("month")

    if month_param == "lifetime":
        key = "monthly_stats:lifetime"
        if refresh:
            cache.delete(key)
        else:
            cached = cache.get(key)
            if cached is not None:
                return JsonResponse({**cached, "cached": True})
        payload = _build_lifetime_stats()
        if not payload.get("cost_source_error") and not payload.get("error"):
            cache.set(key, payload, CURRENT_TTL)
        return JsonResponse(payload)

    current = current_month_start()

    month_start = current
    if month_param:
        parsed = parse_month_param(month_param)
        if parsed is None:
            return JsonResponse({"error": "invalid month; expected YYYY-MM"}, status=400)
        month_start = parsed

    key = f"monthly_stats:{month_start:%Y-%m}"
    if refresh:
        cache.delete(key)
        cache.delete(f"model_rates:{month_start:%Y-%m}")
        cache.delete(f"spend_for:{month_start:%Y-%m}")
        cache.delete(f"tokens_for:{month_start:%Y-%m}")
        cache.delete(f"raw_usage_by_key:{month_start:%Y-%m}")
    else:
        cached = cache.get(key)
        if cached is not None:
            return JsonResponse({**cached, "cached": True})

    payload = _build_stats(month_start)
    if not payload.get("cost_source_error") and not payload.get("error"):
        cache.set(key, payload, CURRENT_TTL if month_start == current else PAST_TTL)
    return JsonResponse(payload)


@api_login_required
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
    if refresh:
        cache.delete(key)
    else:
        cached = cache.get(key)
        if cached is not None:
            return JsonResponse({**cached, "cached": True})

    payload = _rates_resp_for(month_start)
    return JsonResponse(payload)


@api_login_required
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

    if month_param == "lifetime":
        key = "usage_by_key:lifetime"
        if not refresh:
            cached = cache.get(key)
            if cached is not None:
                return JsonResponse({**cached, "cached": True})

        months = lifetime_months()
        allowed_ids = set(app_api_key_ids())
        aggregated_keys = {}
        ws_id = None
        err = None

        for m in months:
            usage_resp = base_views.llmprovider.get_usage_by_key(year=m.year, month=m.month)
            if not isinstance(usage_resp, dict) or usage_resp.get("error"):
                if not err and isinstance(usage_resp, dict):
                    err = usage_resp.get("error")
                continue
            if not ws_id:
                ws_id = usage_resp.get("workspace_id")
            rates = _rates_for(m)
            raw_keys = usage_resp.get("keys", [])
            if allowed_ids:
                raw_keys = [k for k in raw_keys if k["api_key_id"] in allowed_ids]

            for k in raw_keys:
                kid = k["api_key_id"]
                if kid not in aggregated_keys:
                    aggregated_keys[kid] = {
                        "api_key_id": kid,
                        "name": k.get("name") or kid,
                        "input_tokens": 0,
                        "output_tokens": 0,
                        "estimated_cost": 0.0,
                    }
                aggregated_keys[kid]["input_tokens"] += k.get("input_tokens", 0)
                aggregated_keys[kid]["output_tokens"] += k.get("output_tokens", 0)
                for model, tok in k.get("by_model", {}).items():
                    rate = rates.get(model, {})
                    cost = (
                        tok.get("uncached_input_tokens", 0) * rate.get("input", 0)
                        + tok.get("output_tokens", 0) * rate.get("output", 0)
                        + tok.get("cache_creation_tokens", 0) * rate.get("cache_creation", 0)
                        + tok.get("cache_read_tokens", 0) * rate.get("cache_read", 0)
                    )
                    aggregated_keys[kid]["estimated_cost"] += cost

        keys = list(aggregated_keys.values())
        for k in keys:
            k["estimated_cost"] = round(k["estimated_cost"], 2)
        keys.sort(key=lambda k: k["estimated_cost"], reverse=True)

        payload = {
            "month": "lifetime",
            "is_lifetime": True,
            "keys": keys,
            "workspace_id": ws_id,
            "estimated": True,
            "error": err if not keys else None,
        }
        cache.set(key, payload, CURRENT_TTL)
        return JsonResponse(payload)

    current = current_month_start()

    month_start = current
    if month_param:
        parsed = parse_month_param(month_param)
        if parsed is None:
            return JsonResponse({"error": "invalid month; expected YYYY-MM"}, status=400)
        month_start = parsed

    key = f"usage_by_key:{month_start:%Y-%m}"
    if refresh:
        cache.delete(key)
        cache.delete(f"raw_usage_by_key:{month_start:%Y-%m}")
    else:
        cached = cache.get(key)
        if cached is not None:
            return JsonResponse({**cached, "cached": True})

    usage_resp = _raw_usage_by_key(month_start)
    if not isinstance(usage_resp, dict) or usage_resp.get("error"):
        err = usage_resp.get("error") if isinstance(usage_resp, dict) else "usage source unavailable"
        payload = {"keys": [], "workspace_id": None, "estimated": True, "error": err}
        return JsonResponse(payload)

    rates = _rates_for(month_start)

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


@api_login_required
def cost_reconciliation(request):
    """Billed Anthropic spend (chat-key-scoped -- the same numerator cost_pc
    uses) vs. this month's summed per-chat/per-message LOGGED token
    estimates (_logged_spend_split's real+bot total). The gap
    ("unaccounted") is chat-key spend with no matching Message row --
    rejected probes, failed requests, calls the chatbot never logged.
    `chat_scope_is_app_wide` warns when ANTHROPIC_CHAT_API_KEY_IDS isn't
    set: in that state both sides of this reconciliation (and cost_pc's own
    numerator) are still scoped to every key this app has issued -- AI
    Search Curator/narrative/report included, not chat alone."""
    refresh = request.GET.get("refresh", "").lower() in ("1", "true", "yes")
    month_param = request.GET.get("month")

    if month_param == "lifetime":
        key = "cost_reconciliation:lifetime"
        if refresh:
            cache.delete(key)
        else:
            cached = cache.get(key)
            if cached is not None:
                return JsonResponse({**cached, "cached": True})

        months = lifetime_months()
        total_billed = 0.0
        total_real = 0.0
        total_bot = 0.0
        cost_err = None
        has_billed = False

        for m in months:
            rates_resp = _rates_resp_for(m)
            if m == current_month_start() and (rates_resp.get("error") or not rates_resp.get("rates")):
                prev_rates_resp = _rates_resp_for(prev_month(m))
                if prev_rates_resp.get("rates"):
                    rates_resp = prev_rates_resp
            spend, _, m_cost_err = _spend_for(m, rates_resp)
            if m_cost_err and not cost_err:
                cost_err = m_cost_err
            rates = _rates_from_resp(rates_resp) if spend is not None else {}
            split = _logged_spend_split(m, rates)
            total_real += split.get("real", 0.0)
            total_bot += split.get("bot", 0.0)
            if spend is not None:
                has_billed = True
                total_billed += spend

        billed_spend = round(total_billed, 2) if has_billed else None
        logged_spend = round(total_real + total_bot, 2)
        payload = {
            "month": "lifetime",
            "is_lifetime": True,
            "billed_spend": billed_spend,
            "logged_spend": logged_spend,
            "real_spend": round(total_real, 2),
            "bot_spend": round(total_bot, 2),
            "unaccounted": round(billed_spend - logged_spend, 2) if billed_spend is not None else None,
            "chat_scope_is_app_wide": _chat_scope_is_app_wide(),
            "cost_source_error": cost_err,
        }
        if not cost_err:
            cache.set(key, payload, CURRENT_TTL)
        return JsonResponse(payload)

    current = current_month_start()

    month_start = current
    if month_param:
        parsed = parse_month_param(month_param)
        if parsed is None:
            return JsonResponse({"error": "invalid month; expected YYYY-MM"}, status=400)
        month_start = parsed

    key = f"cost_reconciliation:{month_start:%Y-%m}"
    if refresh:
        cache.delete(key)
        cache.delete(f"spend_for:{month_start:%Y-%m}")
        cache.delete(f"model_rates:{month_start:%Y-%m}")
    else:
        cached = cache.get(key)
        if cached is not None:
            return JsonResponse({**cached, "cached": True})

    # Same one-fetch-per-month sharing as _build_stats above -- get_cost
    # needs this month's whole-org rate regardless, and _logged_spend_split
    # below needs the identical rate for its own pricing.
    rates_resp = _rates_resp_for(month_start)
    if month_start == current and (rates_resp.get("error") or not rates_resp.get("rates")):
        prev_rates_resp = _rates_resp_for(prev_month(month_start))
        if prev_rates_resp.get("rates"):
            rates_resp = prev_rates_resp
    spend, _, cost_err = _spend_for(month_start, rates_resp)
    rates = _rates_from_resp(rates_resp) if spend is not None else {}
    split = _logged_spend_split(month_start, rates)
    logged_spend = round(split["real"] + split["bot"], 2)

    payload = {
        "month": month_start.strftime("%Y-%m"),
        "billed_spend": spend,
        "logged_spend": logged_spend,
        "real_spend": round(split["real"], 2),
        "bot_spend": round(split["bot"], 2),
        "unaccounted": round(spend - logged_spend, 2) if spend is not None else None,
        "chat_scope_is_app_wide": _chat_scope_is_app_wide(),
        "cost_source_error": cost_err,
    }
    if not cost_err:
        cache.set(key, payload, CURRENT_TTL if month_start == current else PAST_TTL)
    return JsonResponse(payload)


@api_login_required
def cache_economics(request):
    """Prompt-cache economics for the chat surface this month: a reads-per-
    write reuse ratio (a plain token-count ratio, so it degrades gracefully
    without rate data) plus an estimated $ actually spent on that input vs.
    a baseline of pricing the same tokens as though none of them had ever
    been cached -- the "no caching at all" comparison the break-even framing
    needs, not a different-TTL comparison. Cache reads bill at Anthropic's
    steep discount and cache writes at a premium over plain input, so this
    prices each token type at its own rate via _rates_for's whole-org unit
    rates, same as cost_reconciliation/usage_by_key. Same chat_api_key_ids()
    scope as cost_reconciliation, so it carries the same chat_scope_is_app_wide
    caveat when ANTHROPIC_CHAT_API_KEY_IDS is unset.

    Also returns `roi_multiple` ($ returned via the read discount per $1
    "invested" via the write premium -- see the inline comment above the
    loop) and a deterministic `verdict` ("helping"/"hurting"/"no_data") so
    the frontend can render a plain-language take for a non-technical
    reader instead of just the raw numbers. The verdict is a clean
    savings>0 check, not a judgment call, so it's computed here rather
    than routed through the LLM-generated cost commentary elsewhere on
    the dashboard."""
    refresh = request.GET.get("refresh", "").lower() in ("1", "true", "yes")
    month_param = request.GET.get("month")

    if month_param == "lifetime":
        key = "cache_economics:lifetime"
        if refresh:
            cache.delete(key)
        else:
            cached = cache.get(key)
            if cached is not None:
                return JsonResponse({**cached, "cached": True})

        months = lifetime_months()
        total_creation = 0
        total_read = 0
        actual_cost = 0.0
        baseline_cost = 0.0
        investment = 0.0
        returned = 0.0
        priced_any = False
        usage_err = None

        for m in months:
            buckets, m_err = _chat_cache_buckets(m)
            if m_err and not usage_err:
                usage_err = m_err
            rates = _rates_for(m)
            for model, tok in buckets.items():
                creation = tok.get("cache_creation_tokens", 0)
                read = tok.get("cache_read_tokens", 0)
                uncached = tok.get("uncached_input_tokens", 0)
                total_creation += creation
                total_read += read
                rate = rates.get(model, {})
                input_rate = rate.get("input")
                if not input_rate:
                    continue
                priced_any = True
                cache_creation_rate = rate.get("cache_creation")
                cache_read_rate = rate.get("cache_read")
                actual_cost += uncached * input_rate
                actual_cost += creation * (cache_creation_rate or 0)
                actual_cost += read * (cache_read_rate or 0)
                baseline_cost += (uncached + creation + read) * input_rate
                if cache_creation_rate is not None:
                    investment += creation * max(cache_creation_rate - input_rate, 0)
                if cache_read_rate is not None:
                    returned += read * max(input_rate - cache_read_rate, 0)

        reads_per_write = round(total_read / total_creation, 2) if total_creation else None
        savings = round(baseline_cost - actual_cost, 2) if priced_any else None
        savings_pct = round(savings / baseline_cost * 100, 1) if priced_any and baseline_cost else None
        roi_multiple = round(returned / investment, 2) if investment else None

        if total_creation == 0 or not priced_any:
            verdict = "no_data"
        elif savings is not None and savings > 0:
            verdict = "helping"
        else:
            verdict = "hurting"

        payload = {
            "month": "lifetime",
            "is_lifetime": True,
            "cache_read_tokens": total_read,
            "cache_creation_tokens": total_creation,
            "reads_per_write": reads_per_write,
            "actual_cost": round(actual_cost, 2) if priced_any else None,
            "baseline_cost": round(baseline_cost, 2) if priced_any else None,
            "savings": savings,
            "savings_pct": savings_pct,
            "roi_multiple": roi_multiple,
            "verdict": verdict,
            "chat_scope_is_app_wide": _chat_scope_is_app_wide(),
            "cost_source_error": usage_err,
        }
        if not usage_err:
            cache.set(key, payload, CURRENT_TTL)
        return JsonResponse(payload)

    current = current_month_start()

    month_start = current
    if month_param:
        parsed = parse_month_param(month_param)
        if parsed is None:
            return JsonResponse({"error": "invalid month; expected YYYY-MM"}, status=400)
        month_start = parsed

    key = f"cache_economics:{month_start:%Y-%m}"
    if refresh:
        cache.delete(key)
        cache.delete(f"raw_usage_by_key:{month_start:%Y-%m}")
        cache.delete(f"model_rates:{month_start:%Y-%m}")
    else:
        cached = cache.get(key)
        if cached is not None:
            return JsonResponse({**cached, "cached": True})

    buckets, usage_err = _chat_cache_buckets(month_start)
    rates = _rates_for(month_start)

    total_creation = sum(b["cache_creation_tokens"] for b in buckets.values())
    total_read = sum(b["cache_read_tokens"] for b in buckets.values())
    reads_per_write = round(total_read / total_creation, 2) if total_creation else None

    actual_cost = 0.0
    baseline_cost = 0.0
    # "Investment" is the write premium over plain input (what you paid extra
    # to put content in the cache); "return" is the read discount off plain
    # input (what you got back for reading it). roi_multiple = return /
    # investment, so a non-technical reader can read it as "$X back for
    # every $1 spent enabling caching" instead of a token-count ratio.
    investment = 0.0
    returned = 0.0
    priced_any = False
    for model, tok in buckets.items():
        rate = rates.get(model, {})
        input_rate = rate.get("input")
        if not input_rate:
            continue
        priced_any = True
        uncached = tok["uncached_input_tokens"]
        creation = tok["cache_creation_tokens"]
        read = tok["cache_read_tokens"]
        cache_creation_rate = rate.get("cache_creation")
        cache_read_rate = rate.get("cache_read")
        actual_cost += uncached * input_rate
        actual_cost += creation * (cache_creation_rate or 0)
        actual_cost += read * (cache_read_rate or 0)
        baseline_cost += (uncached + creation + read) * input_rate
        if cache_creation_rate is not None:
            investment += creation * max(cache_creation_rate - input_rate, 0)
        if cache_read_rate is not None:
            returned += read * max(input_rate - cache_read_rate, 0)

    savings = round(baseline_cost - actual_cost, 2) if priced_any else None
    savings_pct = round(savings / baseline_cost * 100, 1) if priced_any and baseline_cost else None
    roi_multiple = round(returned / investment, 2) if investment else None

    # A deterministic, plain-language verdict for a non-technical reader --
    # "is this worth it" is a clean number here, not a judgment call, so no
    # LLM commentary is warranted (unlike CostCommentaryPanel's headline).
    if total_creation == 0 or not priced_any:
        verdict = "no_data"
    elif savings is not None and savings > 0:
        verdict = "helping"
    else:
        verdict = "hurting"

    payload = {
        "month": month_start.strftime("%Y-%m"),
        "cache_read_tokens": total_read,
        "cache_creation_tokens": total_creation,
        "reads_per_write": reads_per_write,
        "actual_cost": round(actual_cost, 2) if priced_any else None,
        "baseline_cost": round(baseline_cost, 2) if priced_any else None,
        "savings": savings,
        "savings_pct": savings_pct,
        "roi_multiple": roi_multiple,
        "verdict": verdict,
        "chat_scope_is_app_wide": _chat_scope_is_app_wide(),
        "cost_source_error": usage_err,
    }
    if not usage_err:
        cache.set(key, payload, CURRENT_TTL if month_start == current else PAST_TTL)
    return JsonResponse(payload)
