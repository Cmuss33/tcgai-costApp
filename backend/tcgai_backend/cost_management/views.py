from django.http import JsonResponse
from .llm_provider_adapter_implementations import AnthropicAdapter
from .models import Chat, Message
from django.views.decorators.http import require_http_methods
from django.views.decorators.csrf import csrf_exempt
import json
import math
from django.contrib.auth import authenticate, login, logout
import anthropic
import os
from django.db import transaction
from django.db.models import Avg, Count, IntegerField, Q, Sum, Value
from django.db.models.functions import Coalesce, TruncDate
from django.utils.timezone import now
from datetime import datetime, timedelta, timezone as dt_timezone
from .api_auth import api_login_required
from .month_utils import real_chats

llmprovider = AnthropicAdapter()

import re
import random
import threading
from concurrent.futures import ThreadPoolExecutor

DAILY_AUTO_AUDIT_CAP = 30  # Safety budget cap (~$0.01/day or ~$0.30/month)
CONVERSATION_START_DATE = datetime(2026, 6, 1, 0, 0, 0, tzinfo=dt_timezone.utc)


GREETING_REGEX = re.compile(
    r'^\s*(hi|hello|hey|yo|howdy|good\s+(morning|afternoon|evening)|hi\s+there|hey\s+there|help|greetings|hola)\s*[!.,?]*\s*$',
    re.IGNORECASE
)


def score_single_chat(chat):
    """Evaluates a single Chat instance using Claude Haiku and saves evaluation_score.
    Returns the integer score (1-100) or None on failure."""
    messages = Message.objects.filter(chat=chat).order_by("timestamp")
    if not messages.exists():
        return None

    conversation_text = ""
    for msg in messages:
        conversation_text += f"\nUser: {msg.content}\nAssistant: {msg.returned_content}\n"

    prompt = f"""
    Evaluate the following conversation and assign a numeric accuracy score (1-100) for the assistant's responses. Respond with only the number.
    If the assistant's answer is related to the question, regardless of if it is positive or negative (for example, not having required item in stock or not being able to return an item) give 100. 
    If it is not related, or the assistant doesn't know the answer, give a lower number. 
    Do NOT include any text or explanation.

    Conversation:
    {conversation_text}
    """
    api_key = os.environ.get('ANTHROPIC_API_KEY')
    if not api_key:
        return None

    try:
        client = anthropic.Anthropic(api_key=api_key)
        message = client.messages.create(
            model="claude-haiku-4-5",
            max_tokens=1024,
            messages=[{"role": "user", "content": prompt}],
        )
        text = message.content[0].text.strip()
        match = re.search(r'\d+', text)
        if not match:
            return None
        score = int(match.group())
        score = max(1, min(100, score))
        chat.evaluation_score = score
        chat.save(update_fields=['evaluation_score'])
        return score
    except Exception as e:
        print(f"[score_single_chat] Error evaluating {chat.chat_id}: {e}")
        return None


def should_auto_audit_chat(chat, products_shown=None):
    """Determines whether a chat qualifies for smart auto-audit within cost boundaries."""
    # 0. Must have Anthropic API key configured
    if not os.environ.get('ANTHROPIC_API_KEY'):
        return False

    # 1. Never audit bots or shadowtest
    if chat.likely_automated or "shadowtest" in chat.chat_id.lower():
        return False

    # 2. Skip if already scored
    if chat.evaluation_score is not None:
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

    score = score_single_chat(chat)
    if score is None:
        return JsonResponse({"error": "Failed to evaluate chat or no messages found"}, status=400)

    return JsonResponse({"eval_percentage": score})


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

        chats_to_audit = list(unaudited_qs)
        if not chats_to_audit:
            return JsonResponse({
                "status": "success",
                "audited_count": 0,
                "results": [],
                "message": "No unaudited conversations found."
            })

        results = []
        with ThreadPoolExecutor(max_workers=min(5, len(chats_to_audit))) as executor:
            future_to_chat = {executor.submit(score_single_chat, c): c for c in chats_to_audit}
            for future in future_to_chat:
                c = future_to_chat[future]
                try:
                    score = future.result()
                    if score is not None:
                        results.append({"chat_id": c.chat_id, "score": score})
                except Exception as e:
                    print(f"[batch_evaluate] Error auditing {c.chat_id}: {e}")

        estimated_cost = round(len(results) * 0.00035, 4)
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
        content = data.get('content')
        llm_formatted_message = data.get('llm_formatted_message')
        returned_content = data.get('returned_content')
        llm_formatted_returned_message = data.get('llm_formatted_returned_message')
        tokens_in = data.get('tokens_in')
        tokens_out = data.get('tokens_out')
        # ENG-148: real, billed prompt-cache token counts. Default to 0 (not
        # None) so a sender that hasn't deployed the cache-reporting fix yet
        # keeps working exactly as before -- these fields are additive.
        cache_creation_tokens = data.get('cache_creation_tokens') or 0
        cache_read_tokens = data.get('cache_read_tokens') or 0
        model = data.get('model')

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
                chat_id=chat_id, defaults={"model": model, "likely_automated": is_shadowtest}
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

        # Smart Auto-Audit: triggers evaluation in background thread if eligible
        # Runs asynchronously after transaction commits with zero impact on shopper latency
        if should_auto_audit_chat(chat, products_shown=products_shown):
            def _launch_audit(c_pk):
                def _async_audit():
                    try:
                        from django.db import connection
                        connection.close()
                        c = Chat.objects.get(pk=c_pk)
                        score_single_chat(c)
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
        chats = real_chats(Chat.objects.filter(timestamp__gte=CONVERSATION_START_DATE))
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
        visible_chats = real_chats(Chat.objects.filter(timestamp__gte=CONVERSATION_START_DATE))

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
        all_real = real_chats(Chat.objects.filter(timestamp__gte=CONVERSATION_START_DATE))
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
def get_messages_by_chat_id(request, chat_id):
    try:
        chat = Chat.objects.get(chat_id=chat_id)
        messages = Message.objects.filter(chat=chat).order_by('timestamp')
        return JsonResponse(list(messages.values()), safe=False)
    except Chat.DoesNotExist:
        return JsonResponse({'status': 'error', 'message': 'Chat not found'}, status=404)
    except Exception as e:
        return JsonResponse({'status': 'error', 'message': str(e)}, status=400)

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
            real_chats(Chat.objects.filter(timestamp__gte=start_date))
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
            real_chats(Chat.objects.filter(timestamp__gte=start_date))
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
            real_chats(Chat.objects.filter(timestamp__gte=start_date))
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
            real_chats(Chat.objects.filter(timestamp__gte=start_date))
            .annotate(day=TruncDate('timestamp'))
            .values('day')
            .annotate(count=Count('chat_id'))
        )

        total = daily_counts.aggregate(total=Sum("count"))["total"] or 0
        avg_per_day = round(total / num_days, 2)

        return JsonResponse({"average_conversations_per_day": avg_per_day})
    except Exception as e:
        return JsonResponse({'status': 'error', 'message': str(e)}, status=400)