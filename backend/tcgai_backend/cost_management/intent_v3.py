"""Taxonomy v3 intent classification for the cost app (pack §6.3).

Stamps live alongside the v2 outcome label and never touch it
(``chat_outcomes.label_chat`` owns v2; ``label_intent_v3`` owns v3).
The behavioural outcome axis is derived independently from behaviour
records (AttributedOrder / CartEvent), the same facts v2 uses, and is
stored as ``outcome_v3`` — a shopper can hold any intent with any
outcome (pack §1.5).

Two classifiers, mirroring the v2 pattern in chat_outcomes.py:

* an AI classifier (Haiku, temperature 0) that reads the shopper's
  messages and returns the full v3 record, used with ``use_llm=True``
  (backfills); its ids are validated against the canonical vocabulary
  and an unrecognised id lands in ``unknown_unclassified`` with reason
  ``unrecognised_intent`` — never silently mapped to the nearest
  category (pack §5A version-mismatch rule);
* deterministic pattern rules (this module), the fallback whenever the
  classifier is unavailable and the instant label for tests/fixtures.

Every stamp records its source in ``intent_source_v3`` — ``rules:v3``,
``llm:claude-haiku-4-5:v3``, or ``human:<source>`` via the correction
fields — and the taxonomy version in ``intent_classifier_version_v3``,
so verification is by reading stamps in the app, never by job status.
Human corrections supersede the classifier label for reporting
(``Chat.effective_intent_primary_v3``) but never erase the original
stamp or its version (pack §6.3B).
"""
import json
import logging
import os
import re

from django.utils import timezone

from .models import AttributedOrder, CartEvent, Message
from .taxonomy_v3 import (
    LOW_CONFIDENCE_THRESHOLD,
    MISSION_VALUES_V3,
    PRIMARY_INTENTS_V3,
    SUBJECT_VALUES_V3,
    TAXONOMY_VERSION,
    UNKNOWN_REASONS_V3,
)

logger = logging.getLogger(__name__)

INTENT_V3_LLM_MODEL = "claude-haiku-4-5"
INTENT_V3_PROMPT_VERSION = "v3"

_GREETING_RE = re.compile(
    r"^(hi|hello|hey|yo|good (morning|afternoon|evening)|thanks|thank you|ok|okay|bye|just looking|just browsing)[.! ]*$",
    re.IGNORECASE,
)

# Pattern groups per v3 intent. Order in _PRIORITY is the reporting
# tie-break only: every matched intent is retained (primary + all
# material secondaries), per pack §1.2 — precedence must not erase
# the other labels the way v2 did.
_PATTERNS = {
    "out_of_bounds_or_manipulation": [
        r"ignore your (rules|instructions)",
        r"you can'?t say\b",
        r"give me a discount.*(ignore|override|secret)",
        r"reveal your (instructions|prompt|system)",
        r"what are your (system )?(instructions|rules|prompt)",
    ],
    "sell_trade": [
        r"\bsell (bulk|my|a|this|them|it|cards|singles|sealed)\b",
        r"^sell\b",
        r"\bselling (my|a|this)\b",
        r"(i want|i'?d like|can i|can you) (to )?sell\b",
        r"do you buy\b",
        r"\bbuy ?list\b",
        r"\btrade[ -]?in\b",
        r"\bbuy (percentage|back)\b",
        r"what do you pay for\b",
    ],
    "post_purchase_service": [
        r"cancel (my|this|one|the) order",
        r"two orders by accident|duplicate order",
        r"(arrived|was delivered) damaged",
        r"damaged (item|card|package|box)",
        r"wrong item (arrived|was sent|in my order)",
        r"missing (item|card|product) (from|in) my order",
        r"refund for my order|my refund",
        r"charged (twice|me twice)",
        r"change (my|the) (order|address)",
        r"return this|exchange this",
    ],
    "order_tracking": [
        r"where('?s| is) my (order|package|parcel)",
        r"track(ing)? (my|the) order",
        r"order status",
        r"shipping progress on my order",
        r"my order (hasn'?t|has not|never|didn'?t|still hasn'?t) (arriv|come|ship)",
        r"still waiting (for|on) my (order|package)",
        r"when will my order (arrive|ship|be delivered)",
    ],
    "account_service": [
        r"points (do|in) .*account|how many points",
        r"gift[ -]?card balance",
        r"my account\b",
        r"loyalty|rewards? (points|balance|account)",
        r"account (access|settings|balance)",
    ],
    "grading_services": [
        r"submit .* for grading",
        r"grading (service|submission|process|cost|price|turnaround|take)",
        r"grade my card",
        r"send .* (in )?for grading",
        r"how (long|much) .* grading",
    ],
    "store_events": [
        r"\bevents?\b.*(schedule|listing|next|when|register|registration|entry|locals)",
        r"when is the next .* (locals|tournament|event)",
        r"tournament (schedule|registration|entry)",
        r"\blocals\b",
    ],
    "store_info_contact": [
        r"discord",
        r"your (store )?(address|location|phone|email|hours)",
        r"where are you located",
        r"store hours|when do you (open|close)|are you open",
        r"how (do|can) i (reach|contact) (you|the store|support)",
        r"contact (info|information|the store|support)",
        r"phone number",
        r"store (email|phone|address)",
        r"what is the .* (email|phone number|address)\b",
    ],
    "store_policy": [
        r"shipping (policy|cost|fee|fees|rate|rates|option|options|time|times|charge|charges)",
        r"how much (is|does|would) (shipping|delivery)",
        r"how long (does|will) (shipping|delivery)",
        r"do you (ship|offer delivery|deliver)",
        r"\bship(s|ping)? to\b",
        r"free shipping",
        r"return policy|can i return|do you (accept|take) returns|exchange policy|refund policy",
        r"payment (methods|options|plans)|do you accept|can i pay|pay in (installments|instalments)",
        r"\bpick ?up\b.*(policy|rule|in store)|price match",
        r"tax(es)? (policy|rate|included)|\bwarranty\b",
        r"policy\b",
    ],
    "cart_checkout_assistance": [
        r"add .* to (my )?cart",
        r"remove .* from (my )?cart",
        r"check ?out\b",
        r"apply (this |a )?(discount|promo|coupon) code",
        r"view (my )?cart",
        r"^you do it[.! ]*$",
    ],
    "deck_help": [
        r"build me a .*deck",
        r"deck (list|help|advice|build)",
        r"competitive deck",
        r"what (should i|to) play\b.*deck|deck for the .* leader",
        r"complete my deck|finish my deck",
    ],
    "market_research": [
        r"\bworth\b",
        r"market (value|price|trend)",
        r"growth potential|grow in value|appreciate",
        r"investment|invest in",
        r"most valuable\b",
        r"price trend|going up in (value|price)",
    ],
    "recommendation_discovery": [
        r"recommend|suggest",
        r"what (should|would) (i|you) (buy|get|recommend)",
        r"gift for|present for|for my (son|daughter|kid|child|nephew|niece)",
        r"something (fun|good) for",
        r"ideas for",
    ],
    "product_lookup": [
        r"do you (have|carry|stock|sell)\b",
        r"\bshow me\b",
        r"\blooking for\b",
        r"\bin stock\b",
        r"\bavailable\b",
        r"how much (is|for|does|would)\b",
        r"\bprice (of|for|on)\b",
        r"i want (a|an|the|some) (?!refund\b|return\b)",
        r"i (want|wanna|would like|'d like) to (buy|purchase|order|pre[ -]?order|get)",
        r"\bpre[ -]?order\b",
        r"\bbinders?\b|\bsleeves?\b|\bbooster (box|pack|packs)\b|\betb\b",
    ],
}

_PRIORITY = [
    "out_of_bounds_or_manipulation",
    "sell_trade",
    "post_purchase_service",
    "order_tracking",
    "account_service",
    "grading_services",
    "store_events",
    "store_info_contact",
    "store_policy",
    "cart_checkout_assistance",
    "deck_help",
    "market_research",
    "recommendation_discovery",
    "product_lookup",
]

_COMPILED = {
    intent: [re.compile(p, re.IGNORECASE) for p in pats]
    for intent, pats in _PATTERNS.items()
}

_SUBJECT_PATTERNS = {
    "singles": r"\bsingles?\b",
    "sealed_products": r"booster (box|pack)|sealed|\betb\b|elite trainer box",
    "graded_products": r"\bpsa\b|\bbgs\b|graded|slab",
    "storage_protection": r"binder|sleeve|toploader|storage|protect(ion|or)|one[ -]?touch|case",
    "decks_and_starter_decks": r"starter deck|precon|deck box",
    "lego": r"\blego\b",
    "other_collectibles": r"funko|figure|plush",
}
_MISSION_PATTERNS = {
    "gifting": r"gift|present for|for my (son|daughter|kid|child|nephew|niece)|birthday",
    "deck_building": r"deck",
    "protection": r"binder|sleeve|toploader|protect",
}


def _detect_subjects(texts):
    joined = " ".join(texts)
    found = [
        name
        for name, pat in _SUBJECT_PATTERNS.items()
        if re.search(pat, joined, re.IGNORECASE)
    ]
    return found or ["none"]


def _detect_missions(texts, primary, subjects):
    joined = " ".join(texts)
    found = []
    for name, pat in _MISSION_PATTERNS.items():
        if re.search(pat, joined, re.IGNORECASE):
            found.append(name)
    if "deck_building" in found and primary != "deck_help" and not re.search(
        r"build|complete|finish", joined, re.IGNORECASE
    ):
        found.remove("deck_building")
    if "protection" in found and "storage_protection" not in subjects:
        found.remove("protection")
    if len(found) > 1:
        return ["multiple"] if set(found) == set(_MISSION_PATTERNS) else found
    return found or ["none"]


def classify_intent_v3_rules(shopper_texts):
    """Deterministic v3 classification. Returns a dict with primary,
    secondary, subject, mission, confidence, unknown_reason."""
    texts = [" ".join((t or "").split()) for t in shopper_texts]
    texts = [t for t in texts if t]
    if not texts:
        return {
            "primary": "unknown_unclassified",
            "secondary": [],
            "subject": ["unknown"],
            "mission": ["unknown"],
            "confidence": 0.0,
            "unknown_reason": "missing_transcript",
        }
    if all(_GREETING_RE.match(t) for t in texts):
        return {
            "primary": "no_clear_intent",
            "secondary": [],
            "subject": ["none"],
            "mission": ["none"],
            "confidence": 0.9,
            "unknown_reason": None,
        }

    matched = []  # intents in first-match order across the conversation
    for text in texts:
        lowered = text.lower()
        # "do you sell X?" is the shopper buying, never sell_trade.
        is_buy_question = bool(re.search(r"do you sell\b", lowered))
        for intent in _PRIORITY:
            if intent == "sell_trade" and is_buy_question:
                continue
            if intent == "market_research" and re.search(r"\bin stock\b", lowered):
                continue  # store-price-in-stock wins over market (pack §1.1 boundary)
            if any(p.search(text) for p in _COMPILED[intent]):
                if intent not in matched:
                    matched.append(intent)
    if not matched:
        return {
            "primary": "unknown_unclassified",
            "secondary": [],
            "subject": _detect_subjects(texts),
            "mission": ["unknown"],
            "confidence": 0.0,
            "unknown_reason": "novel_request",
        }
    # Primary is the conversation's opening operative goal (within its
    # first message, _PRIORITY breaks the tie); every other material
    # goal is retained as a secondary (pack §1.2 worked examples, e.g.
    # d06e0520…: product_lookup primary, market_research secondary).
    primary = matched[0]
    secondary = matched[1:]
    subjects = _detect_subjects(texts)
    if primary == "deck_help" and "decks_and_starter_decks" not in subjects and "singles" not in subjects:
        subjects = ["singles"] if subjects == ["none"] else subjects
    return {
        "primary": primary,
        "secondary": secondary,
        "subject": subjects,
        "mission": _detect_missions(texts, primary, subjects),
        "confidence": 0.85 if not secondary else 0.75,
        "unknown_reason": None,
    }


# ---------------------------------------------------------------------------
# AI classifier (v3). Same fail-soft contract as v2: any failure returns
# None and callers fall back to the rules.

_INTENT_V3_SYSTEM = (
    "You classify a shopper's intent in a conversation with a trading-card "
    "store's chat assistant, under Intent Taxonomy v3 (version "
    + TAXONOMY_VERSION
    + "). You are given ONLY the shopper's messages, in order. Choose "
    "exactly one primary intent from: "
    + ", ".join(PRIMARY_INTENTS_V3)
    + ". Also list every other material goal as secondary intents, the "
    "subjects (from: "
    + ", ".join(SUBJECT_VALUES_V3)
    + "), and missions (from: "
    + ", ".join(MISSION_VALUES_V3)
    + "). Boundaries: PvpShoppe's own price/stock is product_lookup, "
    "external market value is market_research; submitting a card for "
    "grading is grading_services, buying a graded card is product_lookup; "
    "order status is order_tracking, an order problem is "
    "post_purchase_service; Discord/address/hours/contact is "
    "store_info_contact, not store_policy. Use unknown_unclassified when "
    "no primary is justified — never fold it into no_clear_intent. "
    "Respond with ONLY a JSON object: {\"primary\": \"<id>\", "
    "\"secondary\": [\"<id>\"], \"subject\": [\"<id>\"], "
    "\"mission\": [\"<id>\"], \"confidence\": <0..1>, "
    "\"unknown_reason\": \"<reason or null>\"}."
)


def _call_intent_v3_llm(texts):
    api_key = os.environ.get("ANTHROPIC_API_KEY")
    if not api_key or not texts:
        return None
    try:
        import anthropic

        client = anthropic.Anthropic(api_key=api_key)
        numbered = "\n".join(f"{i + 1}. {t}" for i, t in enumerate(texts))
        message = client.messages.create(
            model=INTENT_V3_LLM_MODEL,
            max_tokens=400,
            temperature=0,
            system=_INTENT_V3_SYSTEM,
            messages=[{"role": "user", "content": f"Shopper messages:\n{numbered}"}],
        )
        raw = message.content[0].text.strip()
        if raw.startswith("```"):
            raw = raw.strip("`")
            if raw.lower().startswith("json"):
                raw = raw[4:].strip()
        data = json.loads(raw)
        if isinstance(data, dict) and "primary" in data:
            return data
    except Exception:
        logger.exception("intent v3 classifier call failed; falling back to rules")
    return None


def _validate_llm_v3(data):
    """Validate an LLM v3 payload against the canonical vocabulary.
    Unrecognised ids / low confidence become unknown_unclassified with
    the pack's reasons — never a silent nearest-category mapping."""
    primary = data.get("primary")
    if primary not in PRIMARY_INTENTS_V3:
        return {
            "primary": "unknown_unclassified",
            "secondary": [],
            "subject": ["unknown"],
            "mission": ["unknown"],
            "confidence": 0.0,
            "unknown_reason": "unrecognised_intent",
        }
    try:
        confidence = float(data.get("confidence"))
    except (TypeError, ValueError):
        confidence = 0.0
    secondary = [s for s in (data.get("secondary") or []) if s in PRIMARY_INTENTS_V3 and s != primary]
    subject = [s for s in (data.get("subject") or []) if s in SUBJECT_VALUES_V3] or ["unknown"]
    mission = [m for m in (data.get("mission") or []) if m in MISSION_VALUES_V3] or ["unknown"]
    reason = data.get("unknown_reason")
    if reason not in UNKNOWN_REASONS_V3:
        reason = None
    if primary != "unknown_unclassified" and confidence < LOW_CONFIDENCE_THRESHOLD:
        return {
            "primary": "unknown_unclassified",
            "secondary": secondary,
            "subject": subject,
            "mission": mission,
            "confidence": confidence,
            "unknown_reason": "low_confidence",
        }
    if primary == "unknown_unclassified" and reason is None:
        reason = "unrecognised_intent"
    return {
        "primary": primary,
        "secondary": secondary,
        "subject": subject,
        "mission": mission,
        "confidence": confidence,
        "unknown_reason": reason,
    }


def classify_intent_v3(texts, use_llm=False):
    if use_llm:
        data = _call_intent_v3_llm(texts)
        if data is not None:
            return _validate_llm_v3(data), f"llm:{INTENT_V3_LLM_MODEL}:{INTENT_V3_PROMPT_VERSION}"
    return classify_intent_v3_rules(texts), "rules:v3"


def derive_outcome_v3(chat):
    """The behavioural outcome axis, from behaviour records only."""
    if AttributedOrder.objects.filter(chat_id_raw=chat.chat_id).exists():
        return "purchased"
    if CartEvent.objects.filter(conversation_id=chat.chat_id).exists():
        return "cart_no_purchase"
    return "neither"


def surface_v3_for(chat):
    if (chat.surface or "") in ("curator", "ai_curator") or str(chat.chat_id).startswith("attr_"):
        return "curator"
    return "chat"


def label_intent_v3(chat, use_llm=False):
    """(Re)compute and store a chat's v3 stamps. Never writes any v2
    field. Returns the v3 primary intent."""
    texts = list(
        Message.objects.filter(chat_id=chat.chat_id)
        .order_by("timestamp")
        .values_list("content", flat=True)
    )
    result, source = classify_intent_v3(texts, use_llm=use_llm)
    chat.intent_primary_v3 = result["primary"]
    chat.intent_secondary_v3 = result["secondary"] or None
    chat.intent_confidence_v3 = result["confidence"]
    chat.intent_classifier_version_v3 = TAXONOMY_VERSION
    chat.intent_source_v3 = source
    chat.subject_v3 = result["subject"]
    chat.mission_v3 = result["mission"]
    chat.surface_v3 = surface_v3_for(chat)
    chat.unknown_reason_v3 = result["unknown_reason"]
    chat.outcome_v3 = derive_outcome_v3(chat)
    chat.intent_v3_at = timezone.now()
    chat.save(
        update_fields=[
            "intent_primary_v3",
            "intent_secondary_v3",
            "intent_confidence_v3",
            "intent_classifier_version_v3",
            "intent_source_v3",
            "subject_v3",
            "mission_v3",
            "surface_v3",
            "unknown_reason_v3",
            "outcome_v3",
            "intent_v3_at",
        ]
    )
    return result["primary"]


def refresh_intent_v3_for_chat_id(chat_id, use_llm=False):
    """Fail-silent v3 label refresh for ingest hooks, mirroring
    chat_outcomes.refresh_outcome_for_chat_id: a v3 labeling failure
    must never break ingestion of the signal that triggered it."""
    try:
        from .models import Chat

        chat = Chat.objects.filter(chat_id=str(chat_id)).first()
        if chat is not None:
            label_intent_v3(chat, use_llm=use_llm)
    except Exception:
        logger.exception("intent v3 label refresh failed for chat %s", chat_id)


def apply_human_correction(chat, primary, source):
    """Record a human correction: it supersedes the classifier label
    for reporting but does not erase the original stamp or version."""
    if primary not in PRIMARY_INTENTS_V3:
        raise ValueError(f"unknown v3 intent: {primary}")
    chat.intent_primary_human_v3 = primary
    chat.intent_correction_source_v3 = source
    chat.intent_corrected_at_v3 = timezone.now()
    chat.save(
        update_fields=[
            "intent_primary_human_v3",
            "intent_correction_source_v3",
            "intent_corrected_at_v3",
        ]
    )
    return chat.effective_intent_primary_v3


def build_review_selection(chats):
    """Human-review selection for a backfilled population (pack §6.3C):
    every unknown, every low-confidence primary, a stratified sample
    per intent x month x surface, and every chat already linked to an
    AOP issue/proof. `chats` is an iterable of Chat rows (already
    v3-labelled)."""
    selection = {"unknown": [], "low_confidence": [], "aop_linked": [], "stratified_sample": []}
    strata = {}
    for chat in chats:
        row = {
            "chat_id": chat.chat_id,
            "primary": chat.effective_intent_primary_v3,
            "classifier_primary": chat.intent_primary_v3,
            "confidence": chat.intent_confidence_v3,
            "unknown_reason": chat.unknown_reason_v3,
            "month": chat.timestamp.strftime("%Y-%m") if chat.timestamp else None,
            "surface": chat.surface_v3,
            "intent_source_v3": chat.intent_source_v3,
        }
        if chat.effective_intent_primary_v3 == "unknown_unclassified":
            selection["unknown"].append(row)
        elif (
            chat.intent_confidence_v3 is not None
            and chat.intent_confidence_v3 < 0.75
        ):
            selection["low_confidence"].append(row)
        if chat.github_issue_number:
            selection["aop_linked"].append(row)
        key = (row["primary"], row["month"], row["surface"])
        strata.setdefault(key, []).append(row)
    for key in sorted(strata, key=lambda k: (str(k[0]), str(k[1]), str(k[2]))):
        selection["stratified_sample"].extend(strata[key][:3])
    return selection
