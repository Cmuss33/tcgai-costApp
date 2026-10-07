"""C3: deterministic verdict cards (ENG-201) -- "Help Me Decide" for the
person running the bot.

Each card is COMPUTED, never LLM-generated: thresholds over the same
single-month computations the stats endpoints serve, with copy templates
that fill in the numbers. Candidates with insufficient data are omitted
(never invented); an empty verdicts list with all_clear=True reproduces
the dashboard's all-clear state.

Verdict shape per card:
    {
        "id": "cache-hurting",
        "kind": "cache",            # cache | reconciliation | eval | bot-share
        "tone": "bad",             # good | bad | flat
        "headline": "...",
        "reason": "...",
        "primary": {"label": "...", "detail": "..."},
        "alternatives": [{"label": "...", "detail": "..."}],
        "evidence": [              # C1 citation idiom: structured stat citations
            {"kind": "stat", "metric": "cache.savings", "value": -0.42,
             "source": "cache_economics"},
        ],
        "deep_link": "panel-cache-economics",   # frontend anchor id, or None
    }
"""
from django.core.cache import cache
from django.http import JsonResponse

from .api_auth import api_login_required
from .month_utils import current_month_start, parse_month_param, prev_month
from .stats_views import (
    CURRENT_TTL,
    PAST_TTL,
    _bot_spend_share,
    _cache_economics_for,
    _chat_qs,
    _cost_reconciliation_for,
    _eval_avg,
    _rates_from_resp,
    _rates_resp_for,
    _real_spend_for,
    _spend_for,
)

# Deterministic thresholds -- computed, not judged. Changing any of these
# changes what the dashboard flags, so they live here as named constants.
RECON_UNACCOUNTED_PCT = 5.0   # matches the dashboard's old reconBad rule
EVAL_DROP_POINTS = 5.0         # absolute score points, month over month
EVAL_MIN_SCORED = 10           # need enough scored chats for the avg to mean anything
BOT_SHARE_MIN_PCT = 10.0       # bot share of logged spend worth flagging
BOT_SHARE_DRIFT_PP = 5.0       # month-over-month rise in percentage points


def _usd(value):
    """-$0.42 style formatting for tight mobile copy."""
    if value is None:
        return "—"
    sign = "-" if value < 0 else ""
    return f"{sign}${abs(value):,.2f}"


def _stat(metric, value, source):
    """One C1-idiom evidence citation."""
    return {"kind": "stat", "metric": metric, "value": value, "source": source}


def _bot_share_pct(month_start):
    """Bot share of LOGGED spend as a percentage, or None when it can't be
    computed (no billed figure, no rates, or nothing priceable logged --
    _real_spend_for degrades to (spend, None) in those cases)."""
    rates_resp = _rates_resp_for(month_start)
    spend, _, _ = _spend_for(month_start, rates_resp)
    rates = _rates_from_resp(rates_resp)
    _, share = _real_spend_for(spend, month_start, rates)
    return round(share * 100, 1) if share is not None else None


def _build_context(month_start):
    """The small context dict every evaluator reads from."""
    previous = prev_month(month_start)
    return {
        "month_start": month_start,
        "cache": _cache_economics_for(month_start),
        "recon": _cost_reconciliation_for(month_start),
        "eval_avg": _eval_avg(month_start),
        "prev_eval_avg": _eval_avg(previous),
        "scored": _chat_qs(month_start).filter(evaluation_score__isnull=False).count(),
        "bot_share_pct": _bot_share_pct(month_start),
        "prev_bot_share_pct": _bot_share_pct(previous),
    }


def _cache_verdict(ctx):
    """Prompt caching is costing money instead of saving it -- reuse the
    existing deterministic verdict; "no_data" (or "helping") means skip."""
    c = ctx["cache"]
    if c.get("verdict") != "hurting":
        return None
    savings = c.get("savings")
    savings_pct = c.get("savings_pct")
    rpw = c.get("reads_per_write")
    return {
        "id": "cache-hurting",
        "kind": "cache",
        "tone": "bad",
        "headline": "Prompt caching is costing you money this month.",
        "reason": (
            f"You'd have spent {_usd(abs(savings) if savings is not None else None)} less "
            f"pricing the same tokens uncached. Reads-per-write is only {rpw} -- cached "
            "content isn't being reused enough to earn back the write premium."
        ),
        "primary": {
            "label": "Flag it to your developer",
            "detail": "Review what's being cached or shorten the cache TTL so writes stop outrunning reads.",
        },
        "alternatives": [
            {
                "label": "Wait a month",
                "detail": "Traffic patterns shift -- caching can flip back to helping on its own.",
            },
        ],
        "evidence": [
            _stat("cache.savings", savings, "cache_economics"),
            _stat("cache.savings_pct", savings_pct, "cache_economics"),
            _stat("cache.reads_per_write", rpw, "cache_economics"),
            _stat("cache.roi_multiple", c.get("roi_multiple"), "cache_economics"),
        ],
        "deep_link": "panel-cache-economics",
    }


def _reconciliation_verdict(ctx):
    """Too much of the Anthropic bill can't be matched to logged calls.
    Billed spend None means the cost source is down -- skip, don't invent."""
    r = ctx["recon"]
    billed = r.get("billed_spend")
    unacc = r.get("unaccounted")
    if billed is None or unacc is None or billed <= 0:
        return None
    pct = unacc / billed * 100
    if pct <= RECON_UNACCOUNTED_PCT:
        return None
    alternatives = [
        {
            "label": "Audit probe traffic",
            "detail": "Rejected probes and failed API calls bill but never log a chat.",
        },
    ]
    if r.get("chat_scope_is_app_wide"):
        alternatives.insert(0, {
            "label": "Scope the chat key",
            "detail": (
                "Set ANTHROPIC_CHAT_API_KEY_IDS so the billed figure covers chat traffic "
                "only, not every key this app has issued."
            ),
        })
    return {
        "id": "recon-unaccounted",
        "kind": "reconciliation",
        "tone": "bad",
        "headline": f"{pct:.1f}% of your Anthropic bill can't be matched to logged calls.",
        "reason": (
            f"{_usd(unacc)} of {_usd(billed)} billed this month is unaccounted -- rejected "
            "probes, failed requests, or calls this app never logged."
        ),
        "primary": {
            "label": "Compare against the Anthropic dashboard",
            "detail": "If billed spikes without matching traffic, look for rejected or unlogged API calls.",
        },
        "alternatives": alternatives,
        "evidence": [
            _stat("recon.billed_spend", billed, "cost_reconciliation"),
            _stat("recon.unaccounted", unacc, "cost_reconciliation"),
            _stat("recon.unaccounted_pct", round(pct, 1), "cost_reconciliation"),
        ],
        "deep_link": "panel-cost-reconciliation",
    }


def _eval_verdict(ctx, drop_points=EVAL_DROP_POINTS):
    """Average evaluation score dropped meaningfully vs last month, on a
    big-enough scored sample. Missing previous average means skip.
    drop_points is overridable so C4 alert rules can reuse this exact signal
    with an operator-editable threshold (default: the C3 verdict bar)."""
    avg = ctx["eval_avg"]
    prev_avg = ctx["prev_eval_avg"]
    scored = ctx["scored"]
    if avg is None or prev_avg is None or scored < EVAL_MIN_SCORED:
        return None
    drop = prev_avg - avg
    if drop < drop_points:
        return None
    return {
        "id": "eval-drop",
        "kind": "eval",
        "tone": "bad",
        "headline": f"Bot quality dropped {drop:.0f} points vs last month.",
        "reason": (
            f"Average evaluation score {avg} vs {prev_avg} last month, "
            f"across {scored} scored conversations."
        ),
        "primary": {
            "label": "See where the bot struggled",
            "detail": "The quality themes panel lists this month's low-scored conversations.",
        },
        "alternatives": [
            {
                "label": "Check for a traffic shift",
                "detail": "New question types can drag the average without any model change.",
            },
        ],
        "evidence": [
            _stat("eval.avg", avg, "monthly_stats"),
            _stat("eval.prev_avg", prev_avg, "monthly_stats"),
            _stat("eval.scored", scored, "monthly_stats"),
        ],
        "deep_link": "panel-quality-themes",
    }


def _bot_share_verdict(ctx):
    """Automated traffic's share of logged spend is both large and rising.
    None on either side means skip -- a missing rate source is not evidence
    of zero bot spend."""
    cur = ctx["bot_share_pct"]
    prev = ctx["prev_bot_share_pct"]
    if cur is None or prev is None:
        return None
    if cur < BOT_SHARE_MIN_PCT or (cur - prev) < BOT_SHARE_DRIFT_PP:
        return None
    return {
        "id": "bot-share-drift",
        "kind": "bot-share",
        "tone": "flat",
        "headline": f"Automated traffic now eats {cur:.0f}% of logged spend.",
        "reason": (
            f"Its share rose from {prev:.1f}% to {cur:.1f}% of logged spend -- automated "
            "traffic uses the same API key as real shoppers, so it inflates the bill."
        ),
        "primary": {
            "label": "Audit the automated traffic",
            "detail": "Check whether bot usage grew or real-shopper traffic shrank this month.",
        },
        "alternatives": [
            {
                "label": "Give the bot its own API key",
                "detail": "A dedicated key would separate bot spend in usage-by-key.",
            },
        ],
        "evidence": [
            _stat("bot_share.pct", cur, "monthly_stats"),
            _stat("bot_share.prev_pct", prev, "monthly_stats"),
        ],
        "deep_link": None,
    }


_EVALUATORS = (_cache_verdict, _reconciliation_verdict, _eval_verdict, _bot_share_verdict)


def build_verdicts(month_start):
    """Run every evaluator over one shared context; candidates with
    insufficient data are omitted, never invented."""
    ctx = _build_context(month_start)
    cards = [card for card in (ev(ctx) for ev in _EVALUATORS) if card is not None]
    return {
        "month": month_start.strftime("%Y-%m"),
        "verdicts": cards,
        "all_clear": not cards,
    }


@api_login_required
def verdict_cards(request):
    """C3 verdict cards for the dashboard's attention section: deterministic,
    computed cards (never LLM-generated) with a primary recommendation and
    labeled alternatives per card. Logged-in dashboard endpoint -- not the
    secret-gated report pattern."""
    refresh = request.GET.get("refresh", "").lower() in ("1", "true", "yes")
    month_param = request.GET.get("month")
    current = current_month_start()

    month_start = current
    if month_param:
        parsed = parse_month_param(month_param)
        if parsed is None:
            return JsonResponse({"error": "invalid month; expected YYYY-MM"}, status=400)
        month_start = parsed

    key = f"verdict_cards:{month_start:%Y-%m}"
    if refresh:
        cache.delete(key)
        # The verdicts read through the shared single-month computations --
        # a refresh must bypass their caches too, like the sibling views do.
        cache.delete(f"spend_for:{month_start:%Y-%m}")
        cache.delete(f"model_rates:{month_start:%Y-%m}")
        cache.delete(f"raw_usage_by_key:{month_start:%Y-%m}")
    else:
        cached = cache.get(key)
        if cached is not None:
            return JsonResponse({**cached, "cached": True})

    payload = build_verdicts(month_start)
    cache.set(key, payload, CURRENT_TTL if month_start == current else PAST_TTL)
    return JsonResponse(payload)
