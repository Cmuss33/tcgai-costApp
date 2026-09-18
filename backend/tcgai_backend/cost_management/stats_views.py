"""Month-scoped cost / token / engagement stats for the home dashboard."""
import calendar
import os

from django.contrib.auth.decorators import login_required
from django.core.cache import cache
from django.db.models import Avg, Count, Sum
from django.db.models.functions import TruncDate
from django.http import JsonResponse
from django.utils import timezone

from . import views as base_views
from .llm_provider_adapter_implementations import app_api_key_ids, chat_api_key_ids
from .models import Chat, Message
from .month_utils import current_month_start, month_range, parse_month_param, prev_month, real_chats

CURRENT_TTL = 900       # 15 min — the current month's cost figures still move
PAST_TTL = 86400        # a day — past months are effectively fixed


def _pct_delta(current, previous):
    if not previous or current is None:
        return None
    return round((current - previous) / previous * 100, 1)


def _spend_for(month_start):
    """(total_usd, [{day, amount}], error) from the Anthropic cost report,
    scoped to the chat surface's own key(s) -- see chat_api_key_ids -- so
    AI Search Curator/narrative/report spend can't inflate cost/conversation
    against a denominator that only ever counts chat conversations."""
    resp = base_views.llmprovider.get_cost(
        year=month_start.year, month=month_start.month, key_ids=chat_api_key_ids()
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
    return total, daily, None


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
    return total_in, total_out, daily, cache_info, None


def _rates_for(month_start):
    """This month's effective $/token rate per model, from get_model_rates --
    or {} on any error/exception (a missing rate source must degrade the
    cost/conversation KPI to its unadjusted figure, never break the whole
    dashboard). Whole-org, like get_model_rates itself -- see that
    function's docstring for why it must never be key-scoped."""
    try:
        resp = base_views.llmprovider.get_model_rates(year=month_start.year, month=month_start.month)
    except Exception:
        return {}
    if not isinstance(resp, dict) or resp.get("error"):
        return {}
    return resp.get("rates") or {}


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
    resp = base_views.llmprovider.get_usage_by_key(year=month_start.year, month=month_start.month)
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

    # Cost-weighted proration: billed spend this month includes the
    # ENG-149/150 bot's own traffic (same chat API key as real shoppers),
    # but `convs` above already excludes it -- dividing raw billed spend by
    # a bot-free denominator overstates cost/conversation by roughly the
    # bot's share of logged spend. See _real_spend_for/_bot_spend_share.
    # Rates are only fetched when there's a spend figure to prorate --
    # each call hits the admin API.
    rates = _rates_for(month_start) if spend is not None else {}
    prev_rates = _rates_for(previous) if prev_spend is not None else {}
    real_spend, bot_share = _real_spend_for(spend, month_start, rates)
    prev_real_spend, _prev_bot_share = _real_spend_for(prev_spend, previous, prev_rates)

    cost_pc = round(real_spend / convs, 4) if (real_spend is not None and convs) else None
    prev_cost_pc = round(prev_real_spend / prev_convs, 4) if (prev_real_spend is not None and prev_convs) else None

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
            # Leave `spend.total` above (the "Anthropic spend" KPI) whole --
            # the bot's cost was real money. Only cost_pc's numerator is
            # adjusted; these three fields show the adjustment itself.
            "billed_spend": spend,
            "spend_excl_bot": round(real_spend, 2) if real_spend is not None else None,
            "bot_share_pct": round(bot_share * 100, 1) if bot_share is not None else None,
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


@login_required
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
    current = current_month_start()

    month_start = current
    if month_param:
        parsed = parse_month_param(month_param)
        if parsed is None:
            return JsonResponse({"error": "invalid month; expected YYYY-MM"}, status=400)
        month_start = parsed

    key = f"cost_reconciliation:{month_start:%Y-%m}"
    if not refresh:
        cached = cache.get(key)
        if cached is not None:
            return JsonResponse({**cached, "cached": True})

    spend, _, cost_err = _spend_for(month_start)
    rates = _rates_for(month_start) if spend is not None else {}
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
    cache.set(key, payload, CURRENT_TTL if month_start == current else PAST_TTL)
    return JsonResponse(payload)


@login_required
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
    caveat when ANTHROPIC_CHAT_API_KEY_IDS is unset."""
    refresh = request.GET.get("refresh", "").lower() in ("1", "true", "yes")
    month_param = request.GET.get("month")
    current = current_month_start()

    month_start = current
    if month_param:
        parsed = parse_month_param(month_param)
        if parsed is None:
            return JsonResponse({"error": "invalid month; expected YYYY-MM"}, status=400)
        month_start = parsed

    key = f"cache_economics:{month_start:%Y-%m}"
    if not refresh:
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
        actual_cost += uncached * input_rate
        actual_cost += creation * rate.get("cache_creation", 0)
        actual_cost += read * rate.get("cache_read", 0)
        baseline_cost += (uncached + creation + read) * input_rate

    savings = round(baseline_cost - actual_cost, 2) if priced_any else None
    savings_pct = round(savings / baseline_cost * 100, 1) if priced_any and baseline_cost else None

    payload = {
        "month": month_start.strftime("%Y-%m"),
        "cache_read_tokens": total_read,
        "cache_creation_tokens": total_creation,
        "reads_per_write": reads_per_write,
        "actual_cost": round(actual_cost, 2) if priced_any else None,
        "baseline_cost": round(baseline_cost, 2) if priced_any else None,
        "savings": savings,
        "savings_pct": savings_pct,
        "chat_scope_is_app_wide": _chat_scope_is_app_wide(),
        "cost_source_error": usage_err,
    }
    cache.set(key, payload, CURRENT_TTL if month_start == current else PAST_TTL)
    return JsonResponse(payload)
