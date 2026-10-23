"""Taxonomy v3 — the shared intent vocabulary (Intent Alignment Pack,
2026-10-22; canonical machine-readable contract mirrored in
``taxonomy_v3.json`` beside this module, taxonomy_version 2026-10-22.1).

The cost app is the third implementation surface of the same contract
(pack decision 13): it stamps conversations with v3 intent fields
*alongside* the preserved v2 outcome stamps, never instead of them
(pack §6.3A, decision 10).

Axes (pack §1): primary intent (exactly one per conversation),
secondary intents (unlimited, non-additive), subject, mission,
surface, and a behavioural outcome axis that never overwrites intent.
``unknown_unclassified`` is a workflow state requiring human review,
not a claim about the shopper (pack §1.7, decision 5), and it is never
folded into ``no_clear_intent``.
"""
import json
from pathlib import Path

_CANONICAL = json.loads(
    (Path(__file__).parent / "taxonomy_v3.json").read_text(encoding="utf-8")
)

TAXONOMY_VERSION = _CANONICAL["taxonomy_version"]

PRIMARY_INTENTS_V3 = tuple(i["id"] for i in _CANONICAL["primary_intents"])
SUBJECT_VALUES_V3 = tuple(_CANONICAL["subject_values"])
MISSION_VALUES_V3 = tuple(_CANONICAL["mission_values"])
SURFACE_VALUES_V3 = tuple(_CANONICAL["surface_values"])
OUTCOME_VALUES_V3 = tuple(_CANONICAL["outcome_values"])
UNKNOWN_REASONS_V3 = tuple(_CANONICAL["unknown_reasons"])

INTENT_LABELS_V3 = {i["id"]: i["id"].replace("_", " ").title() for i in _CANONICAL["primary_intents"]}

# Classifier confidence below this is not a justified primary label:
# the chat lands in unknown_unclassified with reason low_confidence
# (pack §6.3B/C -- every low-confidence primary goes to human review).
LOW_CONFIDENCE_THRESHOLD = 0.5

# ---------------------------------------------------------------------------
# v2 -> v3 bridge (pack §2.1, decision 10). Historical v2 counts remain
# the historical record; v3 primary counts will NOT match them, because
# v2 mixed behavioural outcomes with intent on one precedence axis and
# collapsed several v3 categories into single buckets. This table is
# the published explanation of where each v2 bucket went.
V2_TO_V3_BRIDGE = [
    {
        "v2": "purchased",
        "v3": [],
        "destination": "Outcome axis = purchased; intent must be recovered from the conversation",
        "mapping_problem": "Axis error: an Outcome (a result) is treated as an intent and, by precedence, hides the shopper's actual goal.",
    },
    {
        "v2": "cart_no_purchase",
        "v3": [],
        "destination": "Outcome axis = cart_no_purchase; intent must be recovered from the conversation",
        "mapping_problem": "Axis error. Cart behaviour does not say whether the goal was lookup, recommendation, market research, or checkout help.",
    },
    {
        "v2": "purchase_intent",
        "v3": [
            "product_lookup",
            "recommendation_discovery",
            "market_research",
            "deck_help",
            "cart_checkout_assistance",
        ],
        "destination": "Splits mainly into product_lookup, recommendation_discovery, market_research, deck_help, and cart_checkout_assistance",
        "mapping_problem": "Many categories collapsed into one. Store-price ranking, external market value, deck building, and ordinary lookup were not distinguishable.",
    },
    {
        "v2": "sell_to_store",
        "v3": ["sell_trade"],
        "destination": "sell_trade; sometimes secondary store_policy when the shopper is asking terms rather than seeking a valuation or transaction",
        "mapping_problem": "Closest to one-to-one, but v2 had no corresponding router intent or AOP issue path.",
    },
    {
        "v2": "policy_intent",
        "v3": [
            "store_policy",
            "grading_services",
            "store_events",
            "store_info_contact",
            "sell_trade",
            "account_service",
        ],
        "destination": "Splits into store_policy, grading_services, store_events, store_info_contact, sell_trade, and occasionally account_service",
        "mapping_problem": "Largest knowledge/service collapse. Shipping, Discord, grading, events, tax, and trade rules cannot share one success definition or one proof.",
    },
    {
        "v2": "out_of_bounds",
        "v3": ["out_of_bounds_or_manipulation"],
        "destination": "out_of_bounds_or_manipulation; disputed cases may remap to their real intent or no_clear_intent",
        "mapping_problem": "Mostly aligned, but a disputed label (AOP #601) shows the bucket can contain an ordinary mixed-intent conversation.",
    },
    {
        "v2": "no_buying_intent",
        "v3": [
            "order_tracking",
            "post_purchase_service",
            "account_service",
            "store_info_contact",
            "no_clear_intent",
            "unknown_unclassified",
        ],
        "destination": "Splits into order_tracking, post_purchase_service, account_service, store_info_contact, no_clear_intent, and unknown_unclassified",
        "mapping_problem": "Residue bucket. It combines post-purchase service with genuine no-goal conversations and anything unclassified; the split was not measurable under v2.",
    },
]


def bridge_for(v2_bucket):
    for row in V2_TO_V3_BRIDGE:
        if row["v2"] == v2_bucket:
            return row
    return None


# ---------------------------------------------------------------------------
# October count discrepancy (pack §6.3D) — investigated and closed
# 2026-10-22. Verdict: the 232/66/74 split is correct and authoritative;
# 237/59/76 was an INTERIM count. Evidence, all from records that
# predate this module:
#
# * The final v2 record (daily memory 2026-10-16/17, mirrored in the
#   PvpShoppe and Cmuss33 relationship pages): "Outcome taxonomy v2
#   (cost-app PR #133) is fully backfilled and verified by
#   intent_source stamps in the app: final distribution over 391 real
#   chats — purchase_intent 232, policy_intent 66, no_buying_intent 74,
#   sell_to_store 12, out_of_bounds 7, purchased 0, cart 0; zero v1
#   stamps remain. This supersedes the earlier partial figures
#   (237/59/76 with June/September v1 remainders)."
# * The interim figures are exactly what the coverage matrix
#   (intent-coverage-matrix.md, Sources) reports, labelled there as the
#   split "counted 2026-10-16" — i.e. counted while the backfill was
#   still in flight, before the stamp verification closed it.
# * The mechanism is in this repo: the first --llm backfill "died ~240
#   chats in" (label_chat_outcomes.py), and the command iterates ALL
#   Chat rows ascending sliced by --limit, so chunked/windowed passes
#   leave v1-stamped remainders at every boundary — precisely the
#   June/September v1 remainders the final record names. The interim
#   count's excess purchase_intent (+5) and shortfalls in policy (-7)
#   and no_buying (-2 net with sell/oob agreeing) are the signature of
#   v1 labels (which folded policy/service into purchase/no-buying)
#   still standing on the not-yet-relabelled chats.
#
# Standing lesson encoded with it: verify backfills by reading the
# intent_source stamps in the app, never by job status.
COUNT_DISCREPANCY = {
    "total": 391,
    "authoritative": {
        "purchased": 0,
        "cart_no_purchase": 0,
        "purchase_intent": 232,
        "sell_to_store": 12,
        "policy_intent": 66,
        "out_of_bounds": 7,
        "no_buying_intent": 74,
        "basis": (
            "Final taxonomy-v2 backfill record for PR #133, verified by reading "
            "intent_source stamps (llm:claude-haiku-4-5:v2) in the app; zero v1 "
            "stamps remain."
        ),
    },
    "interim": {
        "purchased": 0,
        "cart_no_purchase": 0,
        "purchase_intent": 237,
        "sell_to_store": 12,
        "policy_intent": 59,
        "out_of_bounds": 7,
        "no_buying_intent": 76,
        "basis": (
            "Interim split counted 2026-10-16 while the v2 backfill was still "
            "in flight, with June/September v1-stamped remainders not yet "
            "relabelled. Superseded by the authoritative record."
        ),
    },
    "verdict": (
        "232/66/74 is correct; 237/59/76 was interim, taken mid-backfill. "
        "Sell (12), out-of-bounds (7), purchased (0) and cart (0) agree in "
        "both records, as does the 391 total."
    ),
}
