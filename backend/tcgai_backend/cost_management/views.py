from django.http import JsonResponse
from .llm_provider_adapter_implementations import AnthropicAdapter
from .models import AdvisorTelemetry, AttributedOrder, CartEvent, Chat, Message
from django.views.decorators.http import require_http_methods
from django.views.decorators.csrf import csrf_exempt
import json
import logging
import math
from decimal import Decimal, InvalidOperation
from django.contrib.auth import authenticate, login, logout
import anthropic
import os
from django.db import transaction
from django.db.models import Avg, Count, IntegerField, Q, Sum, Value
from django.db.models.functions import Coalesce, TruncDate, TruncMonth
from django.utils.dateparse import parse_datetime
from django.utils.timezone import now
from datetime import datetime, timedelta, timezone as dt_timezone
from .api_auth import api_login_required
from .chat_outcomes import ALL_OUTCOMES, refresh_outcome_for_chat_id
from .month_utils import CONVERSATION_START_DATE, apply_shop_filter, real_chats
from django.conf import settings

llmprovider = AnthropicAdapter()

logger = logging.getLogger(__name__)

import re
import random
import threading
from concurrent.futures import ThreadPoolExecutor

DAILY_AUTO_AUDIT_CAP = 30  # Safety budget cap (~$0.01/day or ~$0.30/month)


GREETING_REGEX = re.compile(
    r'^\s*(hi|hello|hey|yo|howdy|good\s+(morning|afternoon|evening)|hi\s+there|hey\s+there|help|greetings|hola)\s*[!.,?]*\s*$',
    re.IGNORECASE
)


def score_single_chat(chat):
    """Evaluates a single Chat instance using Claude Haiku and saves evaluation_score.
    Returns the integer score (1-100) or None on failure.

    Hardened 2026-10-14 after chat fd6ef56b scored 25 on auto-audit and 92
    on manual re-evaluation with the identical judge: the call now runs at
    temperature 0, the rubric defines its score bands (an honest negative
    answer is explicitly a HIGH score), and the judge must justify itself
    in one sentence before the number -- a bare sampled integer at default
    temperature was judge noise, not measurement.

    Extended 2026-10-15: the judge is sampled EVAL_SAMPLE_COUNT times and
    the MEDIAN sample wins (a wild roll can no longer outvote the rest),
    and every score carries receipts -- the median sample's justification,
    all samples, the rubric version, and the graded-at time. A re-grade
    appends the superseded score to evaluation_history instead of
    destroying it."""
    messages = Message.objects.filter(chat=chat).order_by("timestamp")
    if not messages.exists():
        return None

    conversation_text = ""
    for msg in messages:
        assistant_text = (msg.returned_content or "").strip()
        if not assistant_text:
            # A turn whose model call ended in a tool call logs no text.
            # Render what actually happened instead of an empty reply --
            # the judge otherwise reads the turn as the assistant saying
            # nothing at all (chat ef6efb16 scored 1 partly on that
            # illusion, 2026-10-17). The tool's name is not recorded in
            # the cost log, so the marker names none.
            raw_returned = msg.llm_formatted_returned_message or ""
            if re.search(r"stop_reason\W{1,6}tool_use", raw_returned):
                assistant_text = (
                    "[assistant made a tool call in this turn and produced no text]"
                )
            else:
                assistant_text = "[no reply recorded for this turn]"
        conversation_text += f"\nUser: {msg.content}\nAssistant: {assistant_text}\n"

    prompt = f"""
    Evaluate the following conversation and assign a numeric accuracy score (1-100) for the assistant's responses.

    Grade against the shopper's INFERRED intent. The shopper may write with dialect, slang, abbreviations, typos, code-switching, or non-standard grammar -- NEVER lower the score for the shopper's writing style. If you can understand what the shopper is asking for, grade the assistant's response against that inferred intent, not against the polish of the shopper's writing.

    Score bands:
    - 90-100: The assistant addressed the intent correctly and helpfully. An honest negative answer earns this band when it is true and helpful: the item is not in stock, a preorder is not open yet, or the request is something the assistant genuinely cannot do (sign the shopper up for a newsletter, change an account) -- provided it says so plainly and offers a useful alternative or next step.
    - 70-89: Addressed the intent but incompletely: partially helpful, a missed follow-up, or a minor inaccuracy that did not change the outcome.
    - 40-69: Partially related but misses the main ask, gives an evasive or generic non-answer, or gets a material fact wrong.
    - 1-39: Unrelated to the intent, invents products/prices/policies, refuses something the assistant could have done, or does not engage at all.

    First write ONE sentence justifying the score, naming the strongest evidence for it. Then, on the final line, output exactly: SCORE: <number>

    Conversation:
    {conversation_text}
    """
    api_key = os.environ.get('ANTHROPIC_API_KEY')
    if not api_key:
        return None

    try:
        client = anthropic.Anthropic(api_key=api_key)
        samples = []
        for _ in range(EVAL_SAMPLE_COUNT):
            try:
                judged = _judge_once(client, prompt)
            except Exception as sample_error:
                # One failed sample must not fail the grade -- the median
                # of the survivors is still a sound score.
                print(f"[score_single_chat] Sample failed for {chat.chat_id}: {sample_error}")
                judged = None
            if judged is not None:
                samples.append(judged)
        if not samples:
            return None

        # Median sample: the stored score is a score the judge actually
        # gave, carrying its own justification.
        samples.sort(key=lambda pair: pair[0])
        score, justification = samples[len(samples) // 2]

        if chat.evaluation_score is not None:
            history = list(chat.evaluation_history or [])
            history.append({
                "score": chat.evaluation_score,
                "rubric_version": chat.evaluation_rubric_version,
                "graded_at": chat.evaluation_at.isoformat() if chat.evaluation_at else None,
            })
            chat.evaluation_history = history
        chat.evaluation_score = score
        chat.evaluation_justification = justification
        chat.evaluation_samples = [s for s, _ in samples]
        chat.evaluation_rubric_version = RUBRIC_VERSION
        chat.evaluation_at = now()
        chat.save(update_fields=[
            'evaluation_score', 'evaluation_justification',
            'evaluation_samples', 'evaluation_rubric_version',
            'evaluation_at', 'evaluation_history',
        ])
        return score
    except Exception as e:
        print(f"[score_single_chat] Error evaluating {chat.chat_id}: {e}")
        return None


def score_and_classify_chat(chat):
    """Grade a chat, then upgrade its outcome label with the AI intent
    classifier: a scored chat is a completed chat, which is exactly when
    intent is clearest. Returns the score (or None). Labeling is
    fail-silent and never runs inside score_single_chat itself, whose
    contract stays grade-only."""
    score = score_single_chat(chat)
    if score is not None:
        refresh_outcome_for_chat_id(chat.chat_id, use_llm=True)
    return score


# The rubric in score_single_chat's prompt is version "v3" (v1 was the
# pre-2026-10-14 number-only prompt; v2 added bands + receipts, 2026-10-15;
# v3 renders tool-call turns in the transcript as a marker instead of an
# empty reply, 2026-10-17). Bump this whenever the prompt's bands,
# instructions, or transcript rendering change, so trend lines never
# silently mix incomparable scores.
RUBRIC_VERSION = "v3"

# Judge samples per chat. A single sampled integer was the fd6ef56b
# failure (25 vs 92 on the identical transcript); the median of 5 makes a
# wild roll structurally unable to win -- it would take 3 of 5 samples
# missing the same way. Cost stays trivial (~$0.0018/chat all-in).
EVAL_SAMPLE_COUNT = 5


def _judge_once(client, prompt):
    """One judge call. Returns (score, justification) or None on failure."""
    message = client.messages.create(
        model="claude-haiku-4-5",
        max_tokens=1024,
        temperature=0,
        messages=[{"role": "user", "content": prompt}],
    )
    text = message.content[0].text.strip()
    match = re.search(r'SCORE:\s*(\d{1,3})', text, re.IGNORECASE)
    if match:
        score = int(match.group(1))
        justification = re.sub(
            r'SCORE:\s*\d{1,3}\s*$', '', text, flags=re.IGNORECASE).strip()
    else:
        # Fallback for a judge that ignored the format: take the LAST
        # integer in the reply (the justification may cite counts).
        numbers = re.findall(r'\d+', text)
        if not numbers:
            return None
        score = int(numbers[-1])
        justification = text
    justification = " ".join(justification.split())[:500] or None
    return max(1, min(100, score)), justification


# A conversation counts as completed once its latest message is this old.
# Scoring mid-conversation grades a fragment: the fd6ef56b auto-audit fired
# after the first exchange (a stock refusal) and scored 25 on a chat that
# read fine in full.
CONVERSATION_QUIET_PERIOD = timedelta(minutes=30)


def _conversation_completed(chat):
    """True when the chat's latest message is older than the quiet period
    (or the chat itself is, when it somehow has no messages)."""
    last_msg_ts = (
        Message.objects.filter(chat=chat)
        .order_by("-timestamp")
        .values_list("timestamp", flat=True)
        .first()
    )
    last_activity = last_msg_ts or chat.timestamp
    if last_activity is None:
        return False
    return now() - last_activity >= CONVERSATION_QUIET_PERIOD


def should_auto_audit_chat(chat, products_shown=None):
    """Determines whether a chat qualifies for smart auto-audit within cost boundaries."""
    # 0. Must have Anthropic API key configured
    if not os.environ.get('ANTHROPIC_API_KEY'):
        return False

    # 1. Never audit bots or shadowtest
    if chat.likely_automated or "shadowtest" in chat.chat_id.lower():
        return False

    # 1b. Never audit non-chat surfaces (advisor/curator/narrative/report,
    # chatbot PR #553): they log token usage only, no transcript to grade.
    if chat.surface != "chat":
        return False

    # 2. Skip if already scored
    if chat.evaluation_score is not None:
        return False

    # 2b. Never audit a conversation that may still be in progress --
    # grading a fragment punishes chats whose later turns resolve the
    # request (see _conversation_completed / fd6ef56b).
    if not _conversation_completed(chat):
        return False

    # 3. Check daily budget cap
    today_start = now().replace(hour=0, minute=0, second=0, microsecond=0)
    today_scored_count = Chat.objects.filter(
        timestamp__gte=today_start,
        evaluation_score__isnull=False
    ).count()
    if today_scored_count >= DAILY_AUTO_AUDIT_CAP:
        return False

    # 4. Check high-value indicators
    has_products = bool(
        products_shown and isinstance(products_shown, dict) and (
            products_shown.get('primary') or products_shown.get('complementary')
        )
    )
    has_oos = False
    if products_shown and isinstance(products_shown, dict):
        ps_str = json.dumps(products_shown)
        if '"available": false' in ps_str or '"available":false' in ps_str:
            has_oos = True

    msg_count = Message.objects.filter(chat=chat).count()
    is_multi_turn = msg_count >= 2

    # High-value triggers: product recommendations, out of stock, multi-turn
    if has_products or has_oos or is_multi_turn:
        return True

    # 10% random sample for baseline coverage on single-turn simple inquiries
    return random.random() < 0.10


@api_login_required
@csrf_exempt
@require_http_methods(["POST"])
def evaluate_chat(request):
    data = json.loads(request.body)
    chat_id = data.get('chat_id')

    try:
        chat = Chat.objects.get(chat_id=chat_id)
    except Chat.DoesNotExist:
        return JsonResponse({"error": "Chat not found"}, status=404)

    score = score_and_classify_chat(chat)
    if score is None:
        return JsonResponse({"error": "Failed to evaluate chat or no messages found"}, status=400)

    chat.refresh_from_db()
    return JsonResponse({
        "eval_percentage": score,
        "justification": chat.evaluation_justification,
        "samples": chat.evaluation_samples,
        "rubric_version": chat.evaluation_rubric_version,
    })


@api_login_required
@csrf_exempt
@require_http_methods(["POST"])
def batch_evaluate(request):
    """Audits up to `limit` unaudited real shopper conversations in parallel."""
    try:
        data = json.loads(request.body.decode('utf-8') or "{}") if request.body else {}
        limit = min(int(data.get("limit", 25)), 50)

        unaudited_qs = real_chats(
            Chat.objects.filter(timestamp__gte=CONVERSATION_START_DATE, evaluation_score__isnull=True)
        ).order_by('-timestamp')[:limit]

        # Same completion gate as auto-audit: batch runs newest-first, so
        # without it the batch would grade live conversations mid-flight.
        chats_to_audit = [c for c in unaudited_qs if _conversation_completed(c)]
        if not chats_to_audit:
            return JsonResponse({
                "status": "success",
                "audited_count": 0,
                "results": [],
                "message": "No unaudited conversations found."
            })

        results = []
        with ThreadPoolExecutor(max_workers=min(5, len(chats_to_audit))) as executor:
            future_to_chat = {executor.submit(score_and_classify_chat, c): c for c in chats_to_audit}
            for future in future_to_chat:
                c = future_to_chat[future]
                try:
                    score = future.result()
                    if score is not None:
                        results.append({"chat_id": c.chat_id, "score": score})
                except Exception as e:
                    print(f"[batch_evaluate] Error auditing {c.chat_id}: {e}")

        estimated_cost = round(len(results) * 0.0018, 4)
        return JsonResponse({
            "status": "success",
            "audited_count": len(results),
            "results": results,
            "estimated_cost_usd": estimated_cost
        })
    except Exception as e:
        return JsonResponse({"status": "error", "message": str(e)}, status=400)

@api_login_required
def get_cost(request):
    year = request.GET.get("year")
    month = request.GET.get("month")
    response = llmprovider.get_cost(year=year, month=month)
    return JsonResponse(response, safe=False)

@api_login_required
def get_tokens(request):
    year = request.GET.get("year")
    month = request.GET.get("month")
    response = llmprovider.get_tokens(year=year, month=month)
    return JsonResponse(response, safe=False)

@csrf_exempt
@require_http_methods(["POST"])
def log_message(request):
    try:
        data = json.loads(request.body)
        chat_id = data.get('chat_id')
        # Surface payloads (chatbot PR #553: advisor/curator/narrative/report)
        # carry token counts only, no message text, and Message's text columns
        # are NOT NULL -- so store "" instead of failing the whole log with a
        # 400. Keep it "", never a placeholder: flag_automated_chats skips an
        # empty opening message but would flag a repeated one as a bot.
        content = data.get('content') or ""
        llm_formatted_message = data.get('llm_formatted_message') or ""
        returned_content = data.get('returned_content') or ""
        llm_formatted_returned_message = data.get('llm_formatted_returned_message') or ""
        tokens_in = data.get('tokens_in')
        tokens_out = data.get('tokens_out')
        # ENG-148: real, billed prompt-cache token counts. Default to 0 (not
        # None) so a sender that hasn't deployed the cache-reporting fix yet
        # keeps working exactly as before -- these fields are additive.
        cache_creation_tokens = data.get('cache_creation_tokens') or 0
        cache_read_tokens = data.get('cache_read_tokens') or 0
        model = data.get('model')
        # Store attribution (chatbot sends its Shopify domain; separate
        # chatbot change). Missing/empty => "" (unattributed, "Unknown" in
        # the dashboard). Never overwritten: first write wins, like `model`.
        shop = data.get('shop') or ""
        # Surface attribution (chatbot PR #553): 'chat' | 'advisor' |
        # 'curator' | 'narrative' | 'report'. Missing => 'chat' (default for
        # all pre-existing rows). First write wins, like `model` and `shop`.
        surface = data.get('surface') or "chat"

        if content == 'hi this is the probe':
            return JsonResponse({'status': 'error', 'message': 'this was a probe message'}, status=400)

        products_shown = None
        if isinstance(llm_formatted_message, dict):
            products_shown = llm_formatted_message.get('products_shown')

        # select_for_update() + get_or_create() locks the Chat row for the
        # duration of this transaction, so concurrent log_message calls for
        # the same chat_id (the chatbot logs each LLM call in a multi-step
        # tool-use turn separately, sometimes within the same second) can't
        # race on the tokens_in/tokens_out increment below and silently drop
        # one side's update.
        is_shadowtest = bool(chat_id and "shadowtest" in chat_id.lower())

        with transaction.atomic():
            chat, created = Chat.objects.select_for_update().get_or_create(
                chat_id=chat_id,
                defaults={"model": model, "likely_automated": is_shadowtest, "shop": shop, "surface": surface},
            )
            if not created and is_shadowtest and not chat.likely_automated:
                chat.likely_automated = True
                chat.save(update_fields=['likely_automated'])

            Message.objects.create(
                chat=chat,
                content=content,
                llm_formatted_message=llm_formatted_message,
                returned_content=returned_content,
                llm_formatted_returned_message=llm_formatted_returned_message,
                tokens_in=tokens_in,
                tokens_out=tokens_out,
                cache_creation_tokens=cache_creation_tokens,
                cache_read_tokens=cache_read_tokens,
                model=model,
                products_shown=products_shown,
            )

            chat.tokens_in += tokens_in
            chat.tokens_out += tokens_out

            # Only update intent if it is currently "NOT FOUND"
            # NOTE: inert since the sender stopped including `content` on message
            # entries (GDPR change, ~Feb 2026) — messages are now role-only, so
            # content_list below is always [] and this never sets chat.intent.
            if chat.intent == "NOT FOUND" and llm_formatted_message:
                try:
                    parsed_msg = llm_formatted_message

                    messages = parsed_msg.get("messages", [])
                    for msg in messages:
                        if msg.get("role") == "assistant":
                            content_list = msg.get("content", [])
                            for content_item in content_list:
                                if content_item.get("type") == "tool_use":
                                    context = content_item.get("input", {}).get("context")
                                    if context:
                                        chat.intent = context
                                        break  # stop after first found context
                            if chat.intent != "NOT FOUND":
                                break  # stop outer loop if intent was set
                except Exception as e:
                    print("Error parsing llm_formatted_message for intent:", e)

            chat.save(update_fields=['tokens_in', 'tokens_out', 'intent'])
            refresh_outcome_for_chat_id(chat.chat_id)

        # Smart Auto-Audit: triggers evaluation in background thread if eligible
        # Runs asynchronously after transaction commits with zero impact on shopper latency
        if should_auto_audit_chat(chat, products_shown=products_shown):
            def _launch_audit(c_pk):
                def _async_audit():
                    try:
                        from django.db import connection
                        connection.close()
                        c = Chat.objects.get(pk=c_pk)
                        score_and_classify_chat(c)
                    except Exception as ex:
                        print(f"[auto_audit] Background audit failed for {c_pk}: {ex}")
                    finally:
                        from django.db import connection
                        connection.close()

                threading.Thread(target=_async_audit, daemon=True).start()

            transaction.on_commit(lambda: _launch_audit(chat.pk))

        return JsonResponse({'status': 'success'})
    except Exception as e:
        return JsonResponse({'status': 'error', 'message': str(e)}, status=400)

@api_login_required
def get_messages(request):
    try:
        chats = apply_shop_filter(
            real_chats(Chat.objects.filter(timestamp__gte=CONVERSATION_START_DATE)),
            request.GET.getlist("shop"),
        )
        result = []
        for chat in chats:
            messages = Message.objects.filter(chat=chat).order_by('-timestamp')
            chat_data = {
                'chat_id': chat.chat_id,
                'messages': list(messages.values())
            }
            result.append(chat_data)
        return JsonResponse(result, safe=False)
    except Exception as e:
        return JsonResponse({'status': 'error', 'message': str(e)}, status=400)

@api_login_required
def get_chat_ids(request):
    try:
        try:
            limit = max(1, int(request.GET.get("limit", 10)))
        except (ValueError, TypeError):
            limit = 10
        try:
            offset = max(0, int(request.GET.get("offset", 0)))
        except (ValueError, TypeError):
            offset = 0

        filter_type = request.GET.get("filter")
        search_query = request.GET.get("search", "").strip()

        # ENG-149/150: exclude flagged bot chats -- this list isn't
        # month-scoped like monthly_stats/insights_summary, so without this
        # they'd be the overwhelming majority of every page (see
        # flag_automated_chats and month_utils.real_chats).
        # Only include real conversations from June 1, 2026 onwards.
        shops = request.GET.getlist("shop")
        visible_chats = apply_shop_filter(
            real_chats(Chat.objects.filter(timestamp__gte=CONVERSATION_START_DATE)),
            shops,
        )

        # Optional single-month scope (YYYY-MM), used by the Chat Logs
        # outcome breakdown to drill into one month's bucket. Unparseable
        # values are ignored, like a bad limit/offset above.
        month_param = (request.GET.get("month") or "").strip()
        if month_param:
            try:
                _year, _month = month_param.split("-")
                visible_chats = visible_chats.filter(
                    timestamp__year=int(_year), timestamp__month=int(_month)
                )
            except (ValueError, AttributeError):
                pass

        if filter_type == "needs_attention":
            visible_chats = visible_chats.filter(
                Q(evaluation_score__lt=75) | Q(investigation_status="flagged")
            )
        elif filter_type == "out_of_stock":
            chat_ids_with_oos = Message.objects.filter(
                products_shown__isnull=False
            ).filter(
                Q(products_shown__icontains='"available": false') | Q(products_shown__icontains='"available":false')
            ).values_list('chat_id', flat=True).distinct()
            visible_chats = visible_chats.filter(chat_id__in=chat_ids_with_oos)
        elif filter_type == "unaudited":
            visible_chats = visible_chats.filter(evaluation_score__isnull=True)
        elif filter_type in (
            "purchased", "cart_no_purchase", "purchase_intent",
            "sell_to_store", "policy_intent", "out_of_bounds",
            "no_buying_intent",
        ):
            # Outcome buckets (chat_outcomes.py). The stored label IS the
            # category, so the filter is a straight equality -- and the
            # precedence is already baked in (an intent-labeled chat has,
            # by construction, no cart event and no order).
            visible_chats = visible_chats.filter(outcome=filter_type)

        if search_query:
            matching_chat_ids = Message.objects.filter(
                Q(content__icontains=search_query) | Q(returned_content__icontains=search_query)
            ).values_list('chat_id', flat=True).distinct()
            visible_chats = visible_chats.filter(
                Q(chat_id__icontains=search_query) | Q(chat_id__in=matching_chat_ids)
            )

        total = visible_chats.count()
        page = (offset // limit) + 1
        total_pages = math.ceil(total / limit) if total > 0 else 1

        chats = visible_chats.order_by('-timestamp')[offset:offset + limit]
        results = list(chats.values())

        products_shown_counts = {chat["chat_id"]: 0 for chat in results}
        page_messages = Message.objects.filter(
            chat_id__in=products_shown_counts.keys(), products_shown__isnull=False
        ).values_list('chat_id', 'products_shown')
        for chat_id, products_shown in page_messages:
            products_shown_counts[chat_id] += len(
                products_shown.get('primary', [])
            ) + len(products_shown.get('complementary', []))

        # ENG-148: cache tokens are derived live from Message rows, not a
        # cached Chat field like tokens_in/tokens_out -- avoids adding a
        # second field to the same incrementally-cached-total pattern that
        # needed a concurrency fix (see the log_message transaction above).
        cache_totals = {
            row["chat_id"]: row
            for row in Message.objects.filter(chat_id__in=products_shown_counts.keys())
            .values("chat_id")
            .annotate(
                cache_creation_total=Coalesce(Sum("cache_creation_tokens"), Value(0, output_field=IntegerField())),
                cache_read_total=Coalesce(Sum("cache_read_tokens"), Value(0, output_field=IntegerField())),
            )
        }

        # All-time / all-up KPIs across all real conversations since June 1, 2026
        all_real = apply_shop_filter(
            real_chats(Chat.objects.filter(timestamp__gte=CONVERSATION_START_DATE)),
            shops,
        )
        all_up = all_real.aggregate(
            audited_count=Count('chat_id', filter=Q(evaluation_score__isnull=False)),
            avg_score=Avg('evaluation_score'),
            needs_attention_count=Count('chat_id', filter=Q(evaluation_score__lt=75) | Q(investigation_status="flagged")),
            total_count=Count('chat_id'),
        )
        avg_score_val = round(all_up["avg_score"], 1) if all_up["avg_score"] is not None else None

        # Customer inquiry extraction: select the first substantive message per chat,
        # skipping opening greetings like 'hello' or 'hi' when subsequent messages exist.
        first_messages = {}
        for m in Message.objects.filter(chat_id__in=products_shown_counts.keys()).order_by('timestamp'):
            text = (m.content or "").strip()
            if not text:
                continue
            curr = first_messages.get(m.chat_id)
            if curr is None:
                first_messages[m.chat_id] = text[:140]
            elif GREETING_REGEX.match(curr) and not GREETING_REGEX.match(text):
                first_messages[m.chat_id] = text[:140]

        for chat in results:
            chat["products_shown_count"] = products_shown_counts[chat["chat_id"]]
            chat["preview"] = first_messages.get(chat["chat_id"], "")
            totals = cache_totals.get(chat["chat_id"], {"cache_creation_total": 0, "cache_read_total": 0})
            chat["cache_creation_tokens"] = totals["cache_creation_total"]
            chat["cache_read_tokens"] = totals["cache_read_total"]

        return JsonResponse({
            "results": results,
            "has_next": offset + limit < total,
            "total": total,
            "page": page,
            "total_pages": total_pages,
            "limit": limit,
            "offset": offset,
            "kpis": {
                "audited_count": all_up["audited_count"] or 0,
                "avg_score": avg_score_val,
                "needs_attention_count": all_up["needs_attention_count"] or 0,
                "total_conversations": all_up["total_count"] or 0,
            }
        }, safe=False)

    except Exception as e:
        return JsonResponse({'status': 'error', 'message': str(e)}, status=400)

@api_login_required
def chat_outcomes_by_month(request):
    """Monthly outcome breakdown for Chat Logs: every real conversation
    since CONVERSATION_START_DATE in exactly one bucket (see
    chat_outcomes.py), counted by the month the conversation happened
    (a later order still credits its chat's month). Unlabeled rows count
    as no_buying_intent so the buckets always sum to the month's total.
    Months are UTC, like every other monthly cut in this app.
    """
    try:
        shops = request.GET.getlist("shop")
        qs = apply_shop_filter(
            real_chats(Chat.objects.filter(timestamp__gte=CONVERSATION_START_DATE)),
            shops,
        )
        rows = (
            qs.annotate(month=TruncMonth("timestamp"))
            .values("month", "outcome")
            .annotate(n=Count("chat_id"))
        )
        buckets = {}
        for r in rows:
            if r["month"] is None:
                continue
            key = r["month"].strftime("%Y-%m")
            bucket = buckets.setdefault(
                key,
                {k: 0 for k in ALL_OUTCOMES},
            )
            outcome = r["outcome"] if r["outcome"] in bucket else "no_buying_intent"
            bucket[outcome] += r["n"]

        start = CONVERSATION_START_DATE
        end = now()
        months = []
        totals = {**{k: 0 for k in ALL_OUTCOMES}, "total": 0}
        year, month = start.year, start.month
        while (year, month) <= (end.year, end.month):
            key = f"{year:04d}-{month:02d}"
            bucket = dict(buckets.get(key, {k: 0 for k in ALL_OUTCOMES}))
            bucket["month"] = key
            bucket["total"] = sum(bucket[k] for k in ALL_OUTCOMES)
            months.append(bucket)
            for k in totals:
                totals[k] += bucket[k]
            month += 1
            if month > 12:
                year, month = year + 1, 1
        return JsonResponse({"months": months, "totals": totals})
    except Exception as e:
        return JsonResponse({'status': 'error', 'message': str(e)}, status=400)


@api_login_required
def get_messages_by_chat_id(request, chat_id):
    try:
        chat = Chat.objects.get(chat_id=chat_id)
        messages = Message.objects.filter(chat=chat).order_by('timestamp')
        return JsonResponse(list(messages.values()), safe=False)
    except Chat.DoesNotExist:
        return JsonResponse({'status': 'error', 'message': 'Chat not found'}, status=404)
    except Exception as e:
        return JsonResponse({'status': 'error', 'message': str(e)}, status=400)


@api_login_required
def low_score_export(request):
    """One-shot JSON export of the low-scoring population for offline
    review: every real chat since CONVERSATION_START_DATE graded under
    rubric v2 below ?below= (default 70), with evaluator receipts and
    full transcripts, served as a file download. The failure analysis
    runs offline (there is no other read path out of the service), so
    this is the extraction door for the post-retro-grade backlog
    review."""
    try:
        below = float(request.GET.get("below", 70))
    except (TypeError, ValueError):
        return JsonResponse({"error": "below must be a number"}, status=400)
    chats = (
        real_chats(Chat.objects.filter(
            timestamp__gte=CONVERSATION_START_DATE,
            evaluation_rubric_version="v2",
            evaluation_score__lt=below,
        ))
        .order_by("evaluation_score", "timestamp")
    )
    payload = {
        "exported_at": now().isoformat(),
        "below": below,
        "count": chats.count(),
        "chats": [
            {
                "chat_id": c.chat_id,
                "shop": c.shop,
                "timestamp": c.timestamp.isoformat(),
                "evaluation_score": c.evaluation_score,
                "evaluation_samples": c.evaluation_samples,
                "evaluation_justification": c.evaluation_justification,
                "messages": list(
                    Message.objects.filter(chat=c)
                    .order_by("timestamp")
                    .values("content", "returned_content")
                ),
            }
            for c in chats
        ],
    }
    resp = JsonResponse(payload)
    resp["Content-Disposition"] = 'attachment; filename="low-score-review.json"'
    return resp

@csrf_exempt
def login_view(request):
    if request.method != "POST":
        return JsonResponse({"error": "POST required"}, status=400)

    data = json.loads(request.body)
    username = data.get("username")
    password = data.get("password")

    user = authenticate(request, username=username, password=password)

    if user is not None:
        login(request, user)
        return JsonResponse({"success": True})

    return JsonResponse({"success": False}, status=401)

@api_login_required
def get_config(request):
    """Dashboard bootstrap config: production shop designation for the
    store filter. Driven by the PRODUCTION_SHOPS env var (comma-separated
    shop domains); empty list = no designation, dashboard behaves as before."""
    return JsonResponse({"production_shops": list(settings.PRODUCTION_SHOPS)})


@api_login_required
def get_shops(request):
    """Distinct shops with real-conversation counts, for the dashboard's
    store filter dropdown. Unattributed rows (shop="") are included so the
    UI can offer them as "Unknown". Bot traffic excluded via real_chats."""
    rows = (
        real_chats(Chat.objects.filter(timestamp__gte=CONVERSATION_START_DATE))
        .values("shop")
        .annotate(chat_count=Count("chat_id"))
        .order_by("-chat_count")
    )
    return JsonResponse(
        {"shops": [{"shop": r["shop"], "chat_count": r["chat_count"]} for r in rows]}
    )


def auth_check(request):
    return JsonResponse({
        "authenticated": request.user.is_authenticated
    })


@csrf_exempt
def logout_view(request):
    """Logs out the user and clears the session."""
    logout(request)
    return JsonResponse({"success": True})

# HELPER FUNCTION, DON'T LIMIT ACCESS
def get_period_start(period: str):
    """Return datetime for start of period based on 'daily', '7days', '30days'."""
    today = now()
    if period == "daily":
        return 1
    elif period == "7_days":
        return 7
    else:  # default 30 days
        return 30

@api_login_required
def get_avg_eval_score(request):
    try:
        period = request.GET.get("period", "30_days")
        num_days = get_period_start(period)
        start_date = now() - timedelta(days=num_days)

        daily_counts = (
            apply_shop_filter(
                real_chats(Chat.objects.filter(timestamp__gte=start_date)),
                request.GET.getlist("shop"),
            )
            .annotate(day=TruncDate('timestamp'))
            .values('day')
            .annotate(avg_day_score=Avg('evaluation_score'))
        )

        avg_score = daily_counts.aggregate(avg_eval=Avg('avg_day_score'))["avg_eval"]
        avg_score = round(avg_score, 2) if avg_score else 'N/A'

        return JsonResponse({"average_eval_score": avg_score})
    except Exception as e:
        return JsonResponse({'status': 'error', 'message': str(e)}, status=400)

@api_login_required
def get_avg_tokens_in(request):
    try:
        period = request.GET.get("period", "30_days")
        num_days = get_period_start(period)
        start_date = now() - timedelta(days=num_days)

        daily_counts = (
            apply_shop_filter(
                real_chats(Chat.objects.filter(timestamp__gte=start_date)),
                request.GET.getlist("shop"),
            )
            .annotate(day=TruncDate('timestamp'))
            .values('day')
            .annotate(tok_in=Avg('tokens_in'))
        )

        avg_tokens_in = daily_counts.aggregate(avg_in=Avg('tok_in'))["avg_in"]
        avg_tokens_in = round(avg_tokens_in, 2) if avg_tokens_in else 0

        return JsonResponse({"average_tokens_in": avg_tokens_in})
    except Exception as e:
        return JsonResponse({'status': 'error', 'message': str(e)}, status=400)

@api_login_required
def get_avg_tokens_out(request):
    try:
        period = request.GET.get("period", "30_days")
        num_days = get_period_start(period)
        start_date = now() - timedelta(days=num_days)

        daily_counts = (
            apply_shop_filter(
                real_chats(Chat.objects.filter(timestamp__gte=start_date)),
                request.GET.getlist("shop"),
            )
            .annotate(day=TruncDate('timestamp'))
            .values('day')
            .annotate(tok_out=Avg('tokens_out'))
        )

        avg_tokens_out = daily_counts.aggregate(avg_out=Avg('tok_out'))["avg_out"]
        avg_tokens_out = round(avg_tokens_out, 2) if avg_tokens_out else 0

        return JsonResponse({"average_tokens_out": avg_tokens_out})
    except Exception as e:
        return JsonResponse({'status': 'error', 'message': str(e)}, status=400)

@api_login_required
def get_avg_conversations_per_day(request):
    try:
        period = request.GET.get("period", "30_days")
        num_days = get_period_start(period)
        start_date = now() - timedelta(days=num_days)

        daily_counts = (
            apply_shop_filter(
                real_chats(Chat.objects.filter(timestamp__gte=start_date)),
                request.GET.getlist("shop"),
            )
            .annotate(day=TruncDate('timestamp'))
            .values('day')
            .annotate(count=Count('chat_id'))
        )

        total = daily_counts.aggregate(total=Sum("count"))["total"] or 0
        avg_per_day = round(total / num_days, 2)

        return JsonResponse({"average_conversations_per_day": avg_per_day})
    except Exception as e:
        return JsonResponse({'status': 'error', 'message': str(e)}, status=400)

def _notify_attributed_revenue(order):
    """Post a Slack note for a newly attributed order: the amount, the
    order, and the surfaces that drove it, via the C4 alerts webhook
    (#p-agent-operations-platform). Best-effort by design -- a Slack
    hiccup must never fail or block attribution ingestion, and webhook
    replays / rescoring (created=False) don't re-post."""
    try:
        from .alerts import _post_slack

        lines = [f"Order {order.order_id} · {order.shop or 'unknown shop'}"]
        if order.surfaces:
            lines.append(f"Journey: {' + '.join(order.surfaces)}")
        lines.append(f"Order total: {order.currency} {order.order_total:.2f}")
        _post_slack(
            f"💰 Attributed revenue: {order.currency} {order.influenced_revenue:.2f}",
            lines,
            "",
        )
    except Exception:  # noqa: BLE001 -- notification is best-effort
        pass


@csrf_exempt
@require_http_methods(["POST"])
def log_attribution(request):
    """Fire-and-forget revenue attribution ingestion from the chatbot's ENG-161
    pipeline (AttributionService.createAttribution, called when an order is
    scored). Same unauthenticated shape as log_message: the chatbot posts
    server-side with no session, so this route is csrf_exempt and login-free
    by design -- it only ever writes AttributedOrder rows.

    Idempotent on (shop, order_id): Shopify webhook replays / rescoring update
    the row instead of double-counting revenue. Attribution for a chat_id with
    no Chat row is still stored (chat left null) -- commercial_impact surfaces
    those rows as `unlinked_orders`, a canary for join-key drift.
    """
    try:
        data = json.loads(request.body)
        chat_id = data.get("chat_id")
        shop = str(data.get("shop") or "")
        order_id = data.get("order_id")
        if not chat_id or not order_id:
            return JsonResponse(
                {"status": "error", "message": "chat_id and order_id are required"},
                status=400,
            )

        def _dec(value):
            try:
                return Decimal(str(value)) if value is not None else Decimal("0")
            except (InvalidOperation, ValueError, TypeError):
                return Decimal("0")

        order_created_at = data.get("order_created_at")
        if isinstance(order_created_at, str):
            try:
                order_created_at = parse_datetime(order_created_at)
            except (ValueError, TypeError):
                order_created_at = None

        surfaces = data.get("surfaces")
        if not isinstance(surfaces, list):
            surfaces = None

        influence_score = data.get("influence_score")
        try:
            influence_score = float(influence_score) if influence_score is not None else None
        except (ValueError, TypeError):
            influence_score = None

        chat = Chat.objects.filter(chat_id=str(chat_id)).first()

        with transaction.atomic():
            obj, created = AttributedOrder.objects.update_or_create(
                shop=shop,
                order_id=str(order_id),
                defaults={
                    "chat": chat,
                    "chat_id_raw": str(chat_id),
                    "attribution_type": str(data.get("attribution_type") or "influenced")[:20],
                    "influenced_revenue": _dec(data.get("influenced_revenue")),
                    "order_total": _dec(data.get("order_total")),
                    "currency": str(data.get("currency") or "USD")[:3].upper(),
                    "order_created_at": order_created_at,
                    "surfaces": surfaces,
                    "influence_score": influence_score,
                },
            )
        if created and obj.influenced_revenue > 0:
            _notify_attributed_revenue(obj)
        refresh_outcome_for_chat_id(chat_id)
        return JsonResponse({"status": "success", "created": created})
    except Exception as e:
        return JsonResponse({"status": "error", "message": str(e)}, status=400)


@csrf_exempt
@require_http_methods(["POST"])
def log_cart_event(request):
    """Fire-and-forget cart-event ingestion from the chatbot (cart funnel,
    Step 2). Same unauthenticated shape as log_attribution: the chatbot
    posts server-side with no session, so this route is csrf_exempt and
    login-free by design -- it only ever writes CartEvent rows.

    Idempotent on the natural key (shop, session_id, source, occurred_at):
    the chatbot retries emits up to 3x, and a redelivery updates the row
    instead of double-counting the funnel. occurred_at is therefore
    required and must parse -- a retry that lost it could never dedupe.
    conversation_id is nullable by design: search-only sessions have no
    conversation, and their cart events still count.
    """
    try:
        data = json.loads(request.body)
        shop = str(data.get("shop") or "")
        session_id = data.get("session_id")
        source = data.get("source")
        items = data.get("items")
        if (
            not shop
            or not session_id
            or not source
            or not isinstance(items, list)
            or not items
        ):
            return JsonResponse(
                {
                    "status": "error",
                    "message": "shop, session_id, source and a non-empty items list are required",
                },
                status=400,
            )
        if source not in {key for key, _label in CartEvent.SOURCE_CHOICES}:
            return JsonResponse(
                {"status": "error", "message": f"unknown source: {source}"},
                status=400,
            )

        def _dec(value):
            try:
                return Decimal(str(value)) if value is not None else Decimal("0")
            except (InvalidOperation, ValueError, TypeError):
                return Decimal("0")

        occurred_at = data.get("occurred_at")
        if isinstance(occurred_at, str):
            try:
                occurred_at = parse_datetime(occurred_at)
            except (ValueError, TypeError):
                occurred_at = None
        if occurred_at is None:
            return JsonResponse(
                {"status": "error", "message": "occurred_at is required (ISO 8601)"},
                status=400,
            )

        conversation_id = data.get("conversation_id")
        conversation_id = str(conversation_id) if conversation_id else None

        with transaction.atomic():
            obj, created = CartEvent.objects.update_or_create(
                shop=shop,
                session_id=str(session_id),
                source=source,
                occurred_at=occurred_at,
                defaults={
                    "conversation_id": conversation_id,
                    "items": items,
                    "cart_value": _dec(data.get("cart_value")),
                    "currency": str(data.get("currency") or "USD")[:3].upper(),
                },
            )
        if conversation_id:
            refresh_outcome_for_chat_id(conversation_id)
        return JsonResponse({"status": "success", "created": created})
    except Exception as e:
        return JsonResponse({"status": "error", "message": str(e)}, status=400)


@csrf_exempt
@require_http_methods(["POST"])
def mark_probe(request):
    """Runtime probe marking from the AOP's Plan 10 verification probes.

    Same unauthenticated shape as log_message / log_attribution: the AOP
    posts server-side with no session, so this route is csrf_exempt and
    login-free by design -- it only ever sets is_probe / probe_marked_at
    on an existing Chat row.

    A probe conversation (the AOP replaying a fixed issue's original
    shopper prompt against the live store) is not shopper demand, so
    month_utils.real_chats() excludes is_probe chats from conversation
    counts, insights, and conversion denominators. The AOP knows the
    probe's conversation id the moment the probe completes; marking it
    here, at runtime, is exact -- unlike the heuristic sweep
    (probe_chats.py / flag_probe_chats) kept for probes nobody announced.

    Idempotent: re-marking an already-probe chat succeeds again and
    refreshes probe_marked_at. A chat_id with no Chat row yet returns
    found:false with HTTP 200 -- the probe's own log_message ingest can
    lag the mark, and a missing row must not fail the caller; the AOP
    logs it and a backfill over its marked ids covers stragglers.
    """
    try:
        data = json.loads(request.body)
        chat_id = data.get("chat_id")
        if not chat_id:
            return JsonResponse(
                {"status": "error", "message": "chat_id is required"},
                status=400,
            )
        chat = Chat.objects.filter(chat_id=str(chat_id)).first()
        if chat is None:
            return JsonResponse({"status": "success", "found": False})
        chat.is_probe = True
        chat.probe_marked_at = now()
        chat.save(update_fields=["is_probe", "probe_marked_at"])
        return JsonResponse({"status": "success", "found": True})
    except Exception as e:
        return JsonResponse({"status": "error", "message": str(e)}, status=400)


@csrf_exempt
@require_http_methods(["POST"])
def log_advisor_telemetry(request):
    """Fire-and-forget advisor budget telemetry ingestion from the chatbot's
    Sales Advisor (emitted next to its `advisor_shown` events, both surfaces).
    Same unauthenticated shape as log_attribution: the chatbot posts
    server-side with no session, so this route is csrf_exempt and login-free
    by design -- it only ever writes AdvisorTelemetry rows.

    Idempotent on (chat_id, surface, emitted_at): re-emits of the same advisor
    run update the row instead of duplicating it. Telemetry for a chat_id with
    no Chat row is still stored (chat left null) -- the budget audit surfaces
    those rows as `unlinked_telemetry`, a canary for join-key drift.
    """
    try:
        data = json.loads(request.body)
        chat_id = data.get("chat_id")
        shop = str(data.get("shop") or "")
        picks = data.get("picks")
        if not chat_id or not shop or not isinstance(picks, list) or not picks:
            return JsonResponse(
                {"status": "error",
                 "message": "chat_id, shop, and a non-empty picks list are required"},
                status=400,
            )

        def _dec(value):
            try:
                return Decimal(str(value)) if value is not None else None
            except (InvalidOperation, ValueError, TypeError):
                return None

        stated_budget = _dec(data.get("stated_budget"))

        clean_picks = []
        for p in picks:
            if not isinstance(p, dict):
                continue
            price = _dec(p.get("price"))
            if price is None:
                continue
            clean_picks.append(
                {
                    "key": str(p.get("key") or "")[:255],
                    "title": str(p.get("title") or "")[:500],
                    "price": float(price),
                    "is_hero": bool(p.get("is_hero")),
                }
            )
        if not clean_picks:
            return JsonResponse(
                {"status": "error", "message": "picks contained no priced entries"},
                status=400,
            )

        surface = str(data.get("surface") or "chat")[:20]
        if surface not in ("chat", "ai_curator"):
            surface = "chat"

        emitted_at = data.get("emitted_at")
        if isinstance(emitted_at, str):
            try:
                emitted_at = parse_datetime(emitted_at)
            except (ValueError, TypeError):
                emitted_at = None

        chat = Chat.objects.filter(chat_id=str(chat_id)).first()

        with transaction.atomic():
            obj, created = AdvisorTelemetry.objects.update_or_create(
                chat_id_raw=str(chat_id),
                surface=surface,
                emitted_at=emitted_at,
                defaults={
                    "chat": chat,
                    "shop": shop,
                    "stated_budget": stated_budget,
                    "currency": str(data.get("currency") or "USD")[:3].upper(),
                    "picks": clean_picks,
                },
            )
        logger.info(
            "[telemetry] advisor_telemetry_ingested chat=%s shop=%s surface=%s picks=%d created=%s",
            obj.chat_id_raw, shop, surface, len(clean_picks), created,
        )
        return JsonResponse({"status": "success", "created": created})
    except Exception as e:
        return JsonResponse({"status": "error", "message": str(e)}, status=400)
