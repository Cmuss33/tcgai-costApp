"""Per-conversation outcome labels: how far did this shopper get?

Every real conversation is categorized into exactly one bucket, by
precedence (behaviour beats signals, the same rule the cart funnel uses):

1. ``purchased``        -- an AttributedOrder links to this chat.
2. ``cart_no_purchase`` -- a CartEvent names this conversation, no order.
3. ``purchase_intent``  -- no cart/order, but the shopper's words show
                           pre-purchase diligence: store-policy questions
                           (shipping, returns, payment, pickup, preorders)
                           or explicit buying language.
4. ``no_buying_intent`` -- none of the above. Post-purchase *service*
                           chats (order tracking, refund chasing) land
                           here too -- that shopper already bought; the
                           receipt says so instead of pretending intent.

Intent derivation (bucket 3) is deliberately local and deterministic:
the chatbot's own intent feed into ``Chat.intent`` has been inert since
the Feb 2026 GDPR payload change (see the note in views.log_message --
every production chat carries "NOT FOUND"), so the funnel's label set
(``policy`` / ``store_info`` vs ``order_tracking``) is re-derived here
from the shopper messages the app already stores. If the router feed is
ever revived, this is the single place to swap the source.

Every label carries a receipt (``outcome_reason``): the attributed
order id and revenue, the latest cart value and date, or the shopper's
verbatim words that triggered the intent label -- the same receipts
discipline as the evaluator's scores. Labels are recomputed whenever a
new signal lands (message ingest, cart event, attribution) and by the
``label_chat_outcomes`` management command for backfills/re-derivation.
"""
import logging
import re
from decimal import Decimal

from django.db.models import Sum
from django.utils import timezone

from .models import AttributedOrder, CartEvent, Chat, Message

logger = logging.getLogger(__name__)

PURCHASED = "purchased"
CART_NO_PURCHASE = "cart_no_purchase"
PURCHASE_INTENT = "purchase_intent"
NO_BUYING_INTENT = "no_buying_intent"
ALL_OUTCOMES = (PURCHASED, CART_NO_PURCHASE, PURCHASE_INTENT, NO_BUYING_INTENT)

# Pre-purchase diligence: the funnel taxonomy's policy / store_info
# families, as patterns over the shopper's own words.
_POLICY_PATTERNS = [
    r"shipping (policy|cost|fee|fees|rate|rates|option|options|time|times|charge|charges)",
    r"how much (is|does|would) (shipping|delivery)",
    r"how long (does|will) (shipping|delivery)",
    r"do you (ship|offer delivery|deliver)",
    r"\bship(s|ping)? to\b",
    r"free shipping",
    r"delivery (policy|cost|fee|fees|option|options|time|times)",
    r"return policy",
    r"can i return",
    r"do you (accept|take) returns",
    r"exchange policy",
    r"can i exchange",
    r"refund policy",
    r"\bwarranty\b",
    r"payment (methods|options|plans)",
    r"do you accept",
    r"can i pay",
    r"pay in (installments|instalments|4\b)",
    r"\bpick ?up\b",
    r"price match",
    r"gift cards?",
    r"pre-?order (policy|window|date|dates|info)",
    r"where are you located",
    r"your (store )?(address|location)",
    r"store hours",
    r"when do you (open|close)",
    r"are you open",
]

# Explicit buying language.
_BUYING_PATTERNS = [
    r"i (want|wanna|would like|'d like) to (buy|purchase|order|pre-?order|get)",
    r"how (do|can) i (buy|order|purchase|check ?out|pre-?order)",
    r"can i (reserve|hold|pre-?order)",
    r"put me down for",
    r"i'?ll take (it|one|that|them)",
    r"let me (buy|order|get) ",
    r"add (it|that|one|this) to (my )?cart",
    r"\bpre-?order\b",
    r"sign me up for .*pre-?order",
]

# Post-purchase service: already bought -- explicitly NOT purchase intent.
_SERVICE_PATTERNS = [
    r"where('?s| is) my (order|package|refund|parcel)",
    r"track(ing)? (my|the) order",
    r"order status",
    r"my order (hasn'?t|has not|never|didn'?t|still hasn'?t) (arriv|come|ship)",
    r"still waiting (for|on) my",
    r"my refund",
    r"refund (status|hasn'?t|has not|never)",
    r"charged (twice|me twice)",
    r"wrong item (arrived|was sent|in my order)",
    r"(arrived|was delivered) damaged",
    r"damaged (item|card|package|box)",
    r"missing (item|card|product) (from|in) my order",
    r"cancel my order",
]

_POLICY_RE = [re.compile(p, re.IGNORECASE) for p in _POLICY_PATTERNS]
_BUYING_RE = [re.compile(p, re.IGNORECASE) for p in _BUYING_PATTERNS]
_SERVICE_RE = [re.compile(p, re.IGNORECASE) for p in _SERVICE_PATTERNS]


def derive_intent(shopper_texts):
    """Classify shopper messages into the funnel's intent families.

    Returns (family, quote): family is "intent" (policy/store-info
    question or explicit buying language), "service" (post-purchase
    support only), or None. Intent beats service when both appear --
    the pre-purchase question is the signal this label exists for.
    The quote is the verbatim shopper message that decided it.
    """
    service_quote = None
    for text in shopper_texts:
        cleaned = " ".join((text or "").split())
        if not cleaned:
            continue
        if any(p.search(cleaned) for p in _POLICY_RE) or any(
            p.search(cleaned) for p in _BUYING_RE
        ):
            return "intent", cleaned[:140]
        if service_quote is None and any(p.search(cleaned) for p in _SERVICE_RE):
            service_quote = cleaned[:140]
    if service_quote is not None:
        return "service", service_quote
    return None, None


def compute_outcome(chat):
    """Return (outcome, reason, value, currency, signals) for one chat.

    Every signal is evaluated independently: `signals` maps each one
    that fired to its own receipt ({"reason": ...} plus value/currency
    where meaningful), so the Chat Logs Intent column can render one
    icon per fired signal -- a chat that added to cart AND purchased
    shows both. `outcome` stays the single precedence bucket the
    monthly breakdown counts (purchased > cart > intent > none).
    """
    signals = {}
    winner = None

    orders = AttributedOrder.objects.filter(chat_id_raw=chat.chat_id)
    if orders.exists():
        total = orders.aggregate(t=Sum("influenced_revenue"))["t"] or Decimal("0")
        latest = orders.order_by("-id").first()
        count = orders.count()
        if count > 1:
            reason = (
                f"Purchased \u2014 {count} attributed orders "
                f"(latest #{latest.order_id}), {latest.currency} {total:.2f} influenced revenue"
            )
        else:
            reason = (
                f"Purchased \u2014 attributed order #{latest.order_id}, "
                f"{latest.currency} {total:.2f} influenced revenue"
            )
        signals["purchased"] = {
            "reason": reason,
            "value": str(total),
            "currency": latest.currency,
        }
        winner = (PURCHASED, reason, total, latest.currency)

    events = CartEvent.objects.filter(conversation_id=chat.chat_id).order_by(
        "-occurred_at", "-id"
    )
    if events.exists():
        ev = events.first()
        signals["cart"] = {
            "reason": (
                f"Added to cart \u2014 {ev.currency} {ev.cart_value:.2f} "
                f"on {ev.occurred_at:%Y-%m-%d}"
            ),
            "value": str(ev.cart_value),
            "currency": ev.currency,
        }
        if winner is None:
            winner = (
                CART_NO_PURCHASE,
                f"Added to cart \u2014 {ev.currency} {ev.cart_value:.2f} "
                f"on {ev.occurred_at:%Y-%m-%d}, no order followed",
                ev.cart_value,
                ev.currency,
            )

    texts = list(
        Message.objects.filter(chat_id=chat.chat_id)
        .order_by("timestamp")
        .values_list("content", flat=True)
    )
    family, quote = derive_intent(texts)
    if family == "intent":
        reason = f"Purchase-intent signal: \u201c{quote}\u201d"
        signals["purchase_intent"] = {"reason": reason}
        if winner is None:
            winner = (PURCHASE_INTENT, reason, None, "")
    if winner is not None:
        return (*winner, signals)
    if family == "service":
        return (
            NO_BUYING_INTENT,
            f"Post-purchase service question: \u201c{quote}\u201d",
            None,
            "",
            signals,
        )
    return (
        NO_BUYING_INTENT,
        "No cart, order, or purchase-intent signal in this conversation",
        None,
        "",
        signals,
    )


def label_chat(chat):
    """(Re)compute and store a chat's outcome label. Returns the outcome."""
    outcome, reason, value, currency, signals = compute_outcome(chat)
    stored_signals = signals or None
    current = (
        chat.outcome,
        chat.outcome_reason,
        chat.outcome_value,
        chat.outcome_currency,
        chat.outcome_signals,
    )
    if current != (outcome, reason, value, currency, stored_signals):
        chat.outcome = outcome
        chat.outcome_reason = reason
        chat.outcome_value = value
        chat.outcome_currency = currency
        chat.outcome_signals = stored_signals
        chat.outcome_at = timezone.now()
        chat.save(
            update_fields=[
                "outcome",
                "outcome_reason",
                "outcome_value",
                "outcome_currency",
                "outcome_signals",
                "outcome_at",
            ]
        )
    return outcome


def refresh_outcome_for_chat_id(chat_id):
    """Fail-silent label refresh for ingest hooks: a labeling failure
    must never break ingestion of the signal that triggered it."""
    try:
        chat = Chat.objects.filter(chat_id=str(chat_id)).first()
        if chat is not None:
            label_chat(chat)
    except Exception:
        logger.exception("outcome label refresh failed for chat %s", chat_id)
