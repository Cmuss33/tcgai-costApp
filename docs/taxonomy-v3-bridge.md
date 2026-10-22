# Taxonomy v3 in the cost app — v2→v3 bridge and backfill runbook

Intent Alignment Pack §6.3 / decisions 3, 5, 10, 13. Taxonomy version
**2026-10-22.1** (canonical contract: `cost_management/taxonomy_v3.json`,
mirrored from the pack's machine-readable taxonomy file; the ids and
version are also enforced in code in `cost_management/taxonomy_v3.py`).

## What v3 changes

- **Intent is its own axis now.** `intent_primary_v3` is exactly one of
  the 16 canonical intents per chat; `intent_secondary_v3[]` keeps every
  other material goal (secondary counts are **non-additive**).
  `subject_v3[]`, `mission_v3[]`, and `surface_v3` travel with it.
- **Outcome is a separate axis.** `outcome_v3` ∈ purchased /
  cart_no_purchase / neither / not_observed, derived from behaviour
  records only. A shopper can hold any intent with any outcome; neither
  axis overwrites the other.
- **Unknown is a review state, not a bucket.** `unknown_unclassified`
  carries `unknown_reason_v3` and feeds the human review queue
  (`/api/cost/unknown_review_queue/`, Chat Logs → "❓ Unknown (v3
  review)"). It is never folded into `no_clear_intent`.
- **Human corrections supersede, never erase.** A correction lands in
  `intent_primary_human_v3` (+ source + timestamp); reporting uses the
  effective primary, and the original classifier stamp and its
  `intent_classifier_version_v3` stay on the row.

## v2 → v3 bridge (pack §2.1)

| V2 bucket | V3 destination | Why the counts will not match |
|---|---|---|
| `purchased` | Outcome axis = `purchased`; intent recovered from the conversation | Axis error in v2: a result was treated as an intent, hiding the shopper's goal by precedence. |
| `cart_no_purchase` | Outcome axis = `cart_no_purchase`; intent recovered from the conversation | Axis error: cart behaviour says nothing about whether the goal was lookup, recommendation, market research, or checkout help. |
| `purchase_intent` | Splits into `product_lookup`, `recommendation_discovery`, `market_research`, `deck_help`, `cart_checkout_assistance` | Many categories collapsed into one: store-price ranking, external market value, deck building and ordinary lookup were indistinguishable. |
| `sell_to_store` | `sell_trade` (sometimes secondary `store_policy` for terms questions) | Closest to one-to-one. |
| `policy_intent` | Splits into `store_policy`, `grading_services`, `store_events`, `store_info_contact`, `sell_trade`, occasionally `account_service` | Largest collapse: shipping, Discord, grading, events, tax and trade rules shared one bucket. |
| `out_of_bounds` | `out_of_bounds_or_manipulation` (disputed cases remap to their real intent or `no_clear_intent`) | Mostly aligned. |
| `no_buying_intent` | Splits into `order_tracking`, `post_purchase_service`, `account_service`, `store_info_contact`, `no_clear_intent`, `unknown_unclassified` | Residue bucket: post-purchase service, genuine no-goal chats, and anything unclassified were mixed; the split was not measurable under v2. |

Historical v2 counts remain the historical record. v3 primary counts
**will not match them**, for the reasons above — that is the bridge
working, not a backfill error. The same table ships as data in
`taxonomy_v3.V2_TO_V3_BRIDGE` and in the `chat_intents_v3_by_month`
API response.

## The October count discrepancy — reconciled

Two v2 splits over the same 391 real chats circulated in October:

- **Interim (superseded):** purchase 237 / policy 59 / no-buying 76.
- **Authoritative:** purchase **232** / policy **66** / no-buying **74**
  (sell 12, out-of-bounds 7, purchased 0, cart 0 agree in both).

**Verdict: 232/66/74 is correct.** It is the final taxonomy-v2
backfill record for PR #133, verified by reading `intent_source`
stamps (`llm:claude-haiku-4-5:v2`) in the app, with zero v1 stamps
remaining. The 237/59/76 split was counted on 2026-10-16 **while the
backfill was still in flight**, with June/September v1-stamped
remainders not yet relabelled — v1 folded policy/service chats into
purchase/no-buying, which is exactly the direction of the interim
skew. The mechanism is visible in this repo's own backfill command:
the first `--llm` pass died ~240 chats in, and chunked passes over an
ascending, `--limit`-sliced population leave remainders at every
boundary. Also encoded in `taxonomy_v3.COUNT_DISCREPANCY`.

## Backfill runbook (post-deploy; Prof's gate)

Do **not** run this against production until this PR is merged and
deployed, and Prof gives the word.

```bash
# On the backend service (Render shell or a one-off job on the new deploy):
python manage.py backfill_intent_v3 --dry-run            # population count only
python manage.py backfill_intent_v3 --llm \
    --since 2026-06-01 --until 2026-10-22 \
    --review-output review-selection.json
```

- Population: real PvpShoppe chats (`month_utils.real_chats` — probes,
  automated traffic, shadowtest ids, non-chat surfaces excluded).
- The command writes **only** `*_v3` fields; every v2 stamp is preserved.
- **Verify by stamps, never by job status** (the v2 lesson: job
  readouts lied twice). After the run: open Chat Logs / the
  `chat_intents_v3_by_month` endpoint and read the `intent_source_v3`
  values (`llm:claude-haiku-4-5:v3` or `rules:v3`) and
  `intent_classifier_version_v3` (`2026-10-22.1`); confirm the stamped
  count the command prints ("verify by stamps: N chats now carry an
  intent_source_v3 stamp") matches the population, and that the
  by-month primary counts sum to each month's total.
- Human review: work `review-selection.json` — every unknown, every
  low-confidence primary, the stratified sample, and every chat already
  linked to an AOP issue/proof — plus the live unknown queue in
  Chat Logs. Corrections go through `intent_v3.apply_human_correction`
  (or the admin), never by editing the classifier stamp.
