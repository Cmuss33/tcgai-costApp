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

Intent derivation (bucket 3) starts from the shopper messages the app
already stores, because the chatbot's own intent feed into ``Chat.intent``
has been inert since the Feb 2026 GDPR payload change (see the note in
views.log_message -- every production chat carries "NOT FOUND"). Two
sources, in order: an AI classifier (Haiku, temperature 0) that reads
the messages in context and picks a family -- shopping / policy /
service / sell_to_store / browsing -- used when a label is computed with
``use_llm=True`` (backfills, and the moment a chat is scored); and the
deterministic pattern rules below, which label fresh chats instantly at
ingest and are the fallback whenever the classifier is unavailable.
Each classification records its source (``Chat.intent_source``) and
family (``Chat.intent_family``) so model drift is visible, never silent.
If the router feed is ever revived, this is the place to swap it in.

Every label carries a receipt (``outcome_reason``): the attributed
order id and revenue, the latest cart value and date, or the shopper's
verbatim words that triggered the intent label -- the same receipts
discipline as the evaluator's scores. Labels are recomputed whenever a
new signal lands (message ingest, cart event, attribution) and by the
``label_chat_outcomes`` management command for backfills/re-derivation.
"""
import json
import logging
import os
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
    r"i (want|wanna|would like|'d like) to (buy|purchase|order|pre[ -]?order|get)",
    r"how (do|can) i (buy|order|purchase|check ?out|pre[ -]?order)",
    r"can i (reserve|hold|pre[ -]?order|buy|order|purchase|get)",
    r"put me down for",
    r"i'?ll take (it|one|that|them)",
    r"let me (buy|order|get) ",
    r"add .* to (my )?cart",
    r"\bpre[ -]?order\b",
    r"sign me up for .*pre[ -]?order",
]

# Seeking: the language shoppers actually use in a card store --
# availability, price, and wanting a product. Still purchase intent
# (it is the chatbot router's product_search flow), but weaker than
# the transactional patterns above: a message that is itself a
# post-purchase service ask never earns intent from this family.
_SEEKING_PATTERNS = [
    r"do you (have|carry|stock)\b",
    r"\blooking for\b",
    r"\bin stock\b",
    r"\bavailable\b",
    r"how much (is|for|does|would)\b",
    r"\bprice (of|for|on)\b",
    r"i want (a|an|the|some) (?!refund\b|return\b)",
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
_SEEKING_RE = [re.compile(p, re.IGNORECASE) for p in _SEEKING_PATTERNS]


def derive_intent(shopper_texts):
    """Classify shopper messages into the funnel's intent families.

    Returns (family, quote): family is "intent" (policy/store-info
    question or explicit buying language), "service" (post-purchase
    support only), or None. Intent beats service when both appear --
    the pre-purchase question is the signal this label exists for.
    The quote is the verbatim shopper message that decided it.
    """
    service_quote = None
    seeking_quote = None
    for text in shopper_texts:
        cleaned = " ".join((text or "").split())
        if not cleaned:
            continue
        if any(p.search(cleaned) for p in _POLICY_RE) or any(
            p.search(cleaned) for p in _BUYING_RE
        ):
            return "intent", cleaned[:140]
        if any(p.search(cleaned) for p in _SERVICE_RE):
            if service_quote is None:
                service_quote = cleaned[:140]
            continue
        if seeking_quote is None and any(p.search(cleaned) for p in _SEEKING_RE):
            seeking_quote = cleaned[:140]
    if seeking_quote is not None:
        return "intent", seeking_quote
    if service_quote is not None:
        return "service", service_quote
    return None, None


# ---------------------------------------------------------------------------
# AI intent classification (2026-10-16). Behaviour tiers never use the
# model -- orders and cart events are facts. For the intent tier the
# classifier reads the shopper's messages in context and picks a family;
# its quote is validated verbatim against the transcript before it may
# appear in a receipt, and any failure falls back to the rules above.

INTENT_LLM_MODEL = "claude-haiku-4-5"
INTENT_PROMPT_VERSION = "v1"
INTENT_FAMILIES = ("shopping", "policy", "service", "sell_to_store", "browsing")
INTENT_FAMILIES_WITH_INTENT = ("shopping", "policy")

_INTENT_SYSTEM = (
    "You classify a shopper's intent in a conversation with a trading-card "
    "store's chat assistant. You are given ONLY the shopper's messages, in "
    "order. Choose exactly one family:\n"
    "- shopping: wants to buy, find, price, reserve, or pre-order products. "
    "Asking if something is in stock, how much it costs, or saying they want "
    "a product all count.\n"
    "- policy: asks about store policies or store information (shipping, "
    "returns, hours, location, pickup, trade-in terms) -- still purchase intent.\n"
    "- service: already bought something and needs help with that purchase "
    "(order status, tracking, refunds, problems with an order).\n"
    "- sell_to_store: wants to sell their own cards or products TO the store.\n"
    "- browsing: anything else -- chatting, nonsense, unclear, no discernible goal.\n"
    'Respond with ONLY a JSON object: {"family": "<family>", "quote": "<the '
    'single shopper message that best shows it, copied exactly>"}.'
)


def _call_intent_llm(texts):
    """One classifier call. Returns the parsed dict, or None on any
    failure (no API key, API error, unparseable reply) -- callers fall
    back to the deterministic rules."""
    api_key = os.environ.get("ANTHROPIC_API_KEY")
    if not api_key or not texts:
        return None
    try:
        import anthropic

        client = anthropic.Anthropic(api_key=api_key)
        numbered = "\n".join(f"{i + 1}. {t}" for i, t in enumerate(texts))
        message = client.messages.create(
            model=INTENT_LLM_MODEL,
            max_tokens=300,
            temperature=0,
            system=_INTENT_SYSTEM,
            messages=[{"role": "user", "content": f"Shopper messages:\n{numbered}"}],
        )
        raw = message.content[0].text.strip()
        if raw.startswith("```"):
            raw = raw.strip("`")
            if raw.lower().startswith("json"):
                raw = raw[4:].strip()
        data = json.loads(raw)
        if isinstance(data, dict) and data.get("family") in INTENT_FAMILIES:
            return data
    except Exception:
        logger.exception("intent classifier call failed; falling back to rules")
    return None


def classify_intent_llm(texts):
    """Classify shopper texts with the AI classifier. Returns
    (family, quote) with the quote validated verbatim against the
    texts (dropped when the model paraphrased), or None when the
    classifier is unavailable."""
    data = _call_intent_llm(texts)
    if data is None:
        return None
    cleaned = [" ".join((t or "").split()) for t in texts]
    quote = " ".join((data.get("quote") or "").split())
    if quote and quote in cleaned:
        return data["family"], quote[:140]
    return data["family"], None


def compute_outcome(chat, use_llm=False):
    """Return (outcome, reason, value, currency, signals, family, source)
    for one chat. With use_llm=True the intent tier is decided by the AI
    classifier when it is available; the rules decide otherwise.``

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

    # Intent tier: the AI classifier when asked and available, else the
    # rules. Behaviour winners above never consult either source.
    family = None
    source = "rules"
    intent_reason = None
    no_intent_reason = None
    llm = classify_intent_llm(texts) if use_llm else None
    if llm is not None:
        fam, quote = llm
        family = fam
        source = f"llm:{INTENT_LLM_MODEL}:{INTENT_PROMPT_VERSION}"
        if fam in INTENT_FAMILIES_WITH_INTENT:
            intent_reason = (
                f"Purchase-intent signal (AI): \u201c{quote}\u201d"
                if quote
                else f"Purchase-intent (AI): {fam}"
            )
        elif fam == "service":
            no_intent_reason = (
                f"Post-purchase service question (AI): \u201c{quote}\u201d"
                if quote
                else "Post-purchase service question (AI)"
            )
        elif fam == "sell_to_store":
            no_intent_reason = (
                f"Selling to the store (AI): \u201c{quote}\u201d"
                if quote
                else "Selling to the store (AI)"
            )
    else:
        fam2, quote2 = derive_intent(texts)
        if fam2 == "intent":
            intent_reason = f"Purchase-intent signal: \u201c{quote2}\u201d"
        elif fam2 == "service":
            family = "service"
            no_intent_reason = f"Post-purchase service question: \u201c{quote2}\u201d"
    if intent_reason is not None:
        signal = {"reason": intent_reason}
        if llm is not None:
            signal["family"] = family
            signal["source"] = source
        signals["purchase_intent"] = signal
        if winner is None:
            winner = (PURCHASE_INTENT, intent_reason, None, "")
    if winner is not None:
        return (*winner, signals, family, source)
    if no_intent_reason is not None:
        return (NO_BUYING_INTENT, no_intent_reason, None, "", signals, family, source)
    return (
        NO_BUYING_INTENT,
        "No cart, order, or purchase-intent signal in this conversation",
        None,
        "",
        signals,
        family,
        source,
    )


def label_chat(chat, use_llm=False):
    """(Re)compute and store a chat's outcome label. Returns the outcome."""
    outcome, reason, value, currency, signals, family, source = compute_outcome(
        chat, use_llm=use_llm
    )
    stored_signals = signals or None
    current = (
        chat.outcome,
        chat.outcome_reason,
        chat.outcome_value,
        chat.outcome_currency,
        chat.outcome_signals,
        chat.intent_family,
        chat.intent_source,
    )
    if current != (outcome, reason, value, currency, stored_signals, family, source):
        chat.outcome = outcome
        chat.outcome_reason = reason
        chat.outcome_value = value
        chat.outcome_currency = currency
        chat.outcome_signals = stored_signals
        chat.intent_family = family
        chat.intent_source = source
        chat.outcome_at = timezone.now()
        chat.save(
            update_fields=[
                "outcome",
                "outcome_reason",
                "outcome_value",
                "outcome_currency",
                "outcome_signals",
                "intent_family",
                "intent_source",
                "outcome_at",
            ]
        )
    return outcome


def refresh_outcome_for_chat_id(chat_id, use_llm=False):
    """Fail-silent label refresh for ingest hooks: a labeling failure
    must never break ingestion of the signal that triggered it."""
    try:
        chat = Chat.objects.filter(chat_id=str(chat_id)).first()
        if chat is not None:
            label_chat(chat, use_llm=use_llm)
    except Exception:
        logger.exception("outcome label refresh failed for chat %s", chat_id)
