from django.db import models

class Cost(models.Model):
    name = models.CharField(max_length=255)
    amount = models.DecimalField(max_digits=10, decimal_places=2)
    currency = models.CharField(max_length=3)
    timestamp = models.DateTimeField(auto_now_add=True)

    def __str__(self):
        return f"{self.name} - {self.amount} {self.currency}"

class Chat(models.Model):
    INVESTIGATION_STATUS_CHOICES = [
        ("unflagged", "Unflagged"),
        ("flagged", "Flagged"),
        ("resolved", "Resolved"),
    ]

    chat_id = models.CharField(max_length=255, primary_key=True)
    model = models.TextField()
    # Store attribution: the chatbot's Shopify domain for this conversation.
    # Sent by the chatbot in the log_message payload (separate chatbot change);
    # "" = unattributed (rows logged before the chatbot started sending it),
    # shown as "Unknown" in the dashboard. First write wins, like `model`.
    shop = models.CharField(max_length=255, db_index=True, default="")
    # Surface attribution: which product surface generated this row.
    # 'chat' = main chat widget (default for all pre-existing rows);
    # 'advisor' | 'curator' | 'narrative' | 'report' = synthetic IDs like
    # advisor_1699999999 logged by those surfaces via log_message.
    # First write wins, like `model` and `shop`.
    surface = models.CharField(max_length=32, db_index=True, default="chat")
    tokens_in = models.IntegerField(default=0)
    tokens_out = models.IntegerField(default=0)
    intent = models.TextField(default='NOT FOUND')
    timestamp = models.DateTimeField(auto_now_add=True)
    evaluation_score = models.IntegerField(null=True, blank=True)

    # Evaluator receipts (2026-10-15): why the score is what it is.
    # evaluation_score is the MEDIAN of the judge samples listed in
    # evaluation_samples; evaluation_justification is the median sample's
    # one-sentence rationale; evaluation_rubric_version tags which rubric
    # produced the score (scores from different rubric versions are not
    # trend-comparable). evaluation_history holds superseded scores
    # ([{score, rubric_version, graded_at}]) so a re-grade -- manual,
    # batch, or a bulk retro-grade -- never silently destroys the score
    # it replaced. All nullable: every pre-receipts row simply has none.
    evaluation_justification = models.TextField(null=True, blank=True)
    evaluation_samples = models.JSONField(null=True, blank=True)
    evaluation_rubric_version = models.CharField(max_length=16, null=True, blank=True)
    evaluation_at = models.DateTimeField(null=True, blank=True)
    evaluation_history = models.JSONField(null=True, blank=True)

    # Outcome labels (2026-10-15): every conversation is categorized by how
    # far the shopper got -- purchased / cart_no_purchase / purchase_intent
    # / no_buying_intent -- derived in chat_outcomes.py from behaviour
    # records (AttributedOrder, CartEvent) first and, failing those, from
    # purchase-intent signals in the shopper's own messages. outcome_reason
    # is the receipt: the order id, the cart value, or the shopper's words
    # that earned the label, so a label can be audited like a score.
    # outcome_value/outcome_currency carry the money where the label has
    # one (influenced revenue for purchased, latest cart total for cart).
    # Null outcome = not yet labeled (pre-label rows, pre-backfill).
    OUTCOME_CHOICES = [
        ("purchased", "Purchased"),
        ("cart_no_purchase", "Added to cart, no purchase"),
        ("purchase_intent", "Purchase intent"),
        ("no_buying_intent", "No buying intent"),
    ]
    outcome = models.CharField(
        max_length=20, choices=OUTCOME_CHOICES, null=True, blank=True, db_index=True
    )
    outcome_reason = models.TextField(null=True, blank=True)
    outcome_value = models.DecimalField(max_digits=12, decimal_places=2, null=True, blank=True)
    outcome_currency = models.CharField(max_length=3, default="")
    outcome_at = models.DateTimeField(null=True, blank=True)
    outcome_signals = models.JSONField(
        null=True,
        blank=True,
        help_text="Every outcome signal that fired (purchased / cart / purchase_intent), each with its own receipt; the Chat Logs Intent column renders one icon per signal. outcome remains the single precedence bucket.",
    )
    intent_family = models.CharField(
        max_length=20,
        null=True,
        blank=True,
        help_text="AI intent family: shopping / policy / service / sell_to_store / browsing. Set when the AI classifier labeled this chat; null for rules-only labels.",
    )
    intent_source = models.CharField(
        max_length=60,
        null=True,
        blank=True,
        help_text="What produced the intent classification: 'rules' or 'llm:<model>:<prompt version>' -- makes classifier drift visible instead of silent.",
    )

    # ENG-149/150: a scripted caller pinging the live chat endpoint with the
    # same message on a schedule (a fresh chat_id each time) is not a real
    # conversation. Set by the `flag_automated_chats` management command,
    # which flags chats whose opening message matches an hourly spike of
    # identical text across many different chat_ids -- exactly this bot's
    # signature, not something a genuine batch of shoppers produces. Excluded
    # from conversation counts and AI-generated insights (see month_utils.py's
    # real_chats()) so a bot can no longer skew either. Manually editable in
    # the admin for the rare misclassification.
    likely_automated = models.BooleanField(default=False, db_index=True)

    # Plan 10 (AOP probe verification): a conversation the AOP itself
    # created by replaying a fixed issue's original shopper prompt against
    # the live store to verify the fix. Not shopper demand -- excluded from
    # conversation counts, insights, and conversion denominators via
    # month_utils.real_chats(), same as likely_automated. Set at runtime by
    # the mark_probe ingestion endpoint the moment a probe completes (the
    # heuristic identification in probe_chats.py remains for probes nobody
    # announced). Manually editable in the admin for the rare
    # misclassification.
    is_probe = models.BooleanField(default=False, db_index=True)
    probe_marked_at = models.DateTimeField(null=True, blank=True)

    investigation_status = models.CharField(
        max_length=20,
        choices=INVESTIGATION_STATUS_CHOICES,
        default="unflagged",
    )
    flag_reason = models.TextField(blank=True, default="")
    flagged_at = models.DateTimeField(null=True, blank=True)
    flagged_by = models.CharField(max_length=150, blank=True, default="")
    github_issue_number = models.IntegerField(null=True, blank=True)
    github_issue_url = models.URLField(blank=True, default="")
    linear_issue_id = models.CharField(max_length=64, blank=True, default="")
    linear_issue_url = models.URLField(blank=True, default="")
    flag_error = models.TextField(blank=True, default="")

    def __str__(self):
        return self.chat_id


class InsightsSnapshot(models.Model):
    """One stored `report_insights` result per calendar month.

    The current month's row is upserted on each fresh generation; once the
    month rolls over the row is never touched again (frozen automatically).
    """
    month = models.DateField(unique=True)  # first day of the month covered
    payload = models.JSONField()
    conversations_analyzed = models.IntegerField(default=0)
    generated_at = models.DateTimeField(auto_now=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-month"]

    def __str__(self):
        return f"InsightsSnapshot {self.month:%Y-%m}"


class CostMethodologyChange(models.Model):
    """A dated, human-maintained log of anything that can move the
    cost/conversation numbers WITHOUT a real change in customer-facing
    behavior -- a pricing/scoping bug fix, a new LLM call site added to a
    surface, a config change, a traffic incident. Consulted when reviewing
    cost moves, so a delta can be lined up with a known change on this
    date instead of inventing a cause -- the same discipline
    insights_views.report_insights already applies to conversation counts
    (see _build_prompt's grounding instruction).

    Add an entry here whenever you ship something that could move these
    numbers on its own -- this is a process step, not something automation
    can infer. See the "Why did cost move?" section of CLAUDE.md."""

    CATEGORY_CHOICES = [
        ("measurement_fix", "Measurement fix"),
        ("new_feature", "New feature"),
        ("config_change", "Config change"),
        ("incident", "Incident"),
    ]

    date = models.DateField(db_index=True)
    description = models.TextField()
    category = models.CharField(max_length=20, choices=CATEGORY_CHOICES)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-date"]

    def __str__(self):
        return f"{self.date:%Y-%m-%d} [{self.category}] {self.description[:60]}"


#TODO: use a unique message_id gotten from claude instead of djagno's
class Message(models.Model):
    chat = models.ForeignKey(Chat, on_delete=models.CASCADE, to_field='chat_id')
    content = models.TextField()
    llm_formatted_message = models.TextField()
    returned_content = models.TextField()
    llm_formatted_returned_message = models.TextField()
    tokens_in = models.IntegerField()
    tokens_out = models.IntegerField()
    # Real, billed prompt-cache token counts from Anthropic's own usage
    # object -- previously not sent by the chatbot at all (ENG-148), so
    # tokens_in/cost here silently excluded every cache-hit turn's true
    # token usage. Default 0 so historical rows (and any sender that hasn't
    # deployed the fix yet) don't need a backfill to remain valid.
    cache_creation_tokens = models.IntegerField(default=0)
    cache_read_tokens = models.IntegerField(default=0)
    model = models.TextField()
    products_shown = models.JSONField(null=True, blank=True)
    timestamp = models.DateTimeField(auto_now_add=True)

    def __str__(self):
        return f"Message in Chat {self.chat.chat_id}"


class AttributedOrder(models.Model):
    """Revenue the upstream chatbot attributes to one of its conversations.

    Ingested via POST /api/cost/log_attribution/ (fire-and-forget, same
    unauthenticated shape as log_message/), emitted by the chatbot's
    AttributionService.createAttribution when its ENG-161 pipeline scores an
    order. Powers the C5 commercial-impact panel: chat-influenced revenue
    against AI spend, in Rufus-style plain numbers.

    The join key is OrderAttribution.agentSessionId == Chat.chat_id (the
    chatbot posts its conversation_id as chat_id). `chat` stays nullable:
    attribution can arrive for a chat_id that was never logged (e.g. logging
    disabled for that turn), and that must not lose the revenue row --
    commercial_impact surfaces those rows as `unlinked_orders` (a canary for
    join-key drift).
    """

    chat = models.ForeignKey(
        Chat, null=True, blank=True, on_delete=models.SET_NULL,
        to_field="chat_id", related_name="attributed_orders",
    )
    chat_id_raw = models.CharField(max_length=255, db_index=True)
    shop = models.CharField(max_length=255, db_index=True, default="")
    order_id = models.CharField(max_length=255)
    attribution_type = models.CharField(max_length=20, default="influenced")
    # Influenced revenue = recommended (main + add-on) revenue at price paid
    # after discounts, per ENG-161. Excludes shipping/tax by construction.
    influenced_revenue = models.DecimalField(max_digits=12, decimal_places=2, default=0)
    order_total = models.DecimalField(max_digits=12, decimal_places=2, default=0)
    currency = models.CharField(max_length=3, default="USD")
    order_created_at = models.DateTimeField(null=True, blank=True, db_index=True)
    surfaces = models.JSONField(null=True, blank=True)
    influence_score = models.FloatField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        constraints = [
            # Webhook replays must be idempotent: the same order scored twice
            # updates the row instead of double-counting revenue.
            models.UniqueConstraint(
                fields=["shop", "order_id"], name="unique_shop_order_attribution"
            ),
        ]
        ordering = ["-order_created_at"]

    def __str__(self):
        return f"{self.shop} order {self.order_id} -> chat {self.chat_id_raw}"


class AdvisorTelemetry(models.Model):
    """Per-advisor-run budget telemetry emitted by the chatbot's Sales Advisor.

    Ingested via POST /api/cost/log_advisor_telemetry/ (fire-and-forget, same
    unauthenticated shape as log_attribution/), emitted next to the chatbot's
    `advisor_shown` events on both surfaces (chat, ai_curator). Powers the C6
    budget-compliance audit: the C2 "Audit advisor budget compliance" mission
    checks that the advisor's BUDGET CONSTRAINT RULE (hero pick at or below
    the shopper's stated budget) holds in production.

    The join key is chatbot conversation_id == Chat.chat_id (posted as
    chat_id). `chat` stays nullable: telemetry can arrive for a chat_id that
    was never logged, and that must not lose the row -- the budget audit
    surfaces those rows as `unlinked_telemetry`, a canary for join-key drift
    (same discipline as C5's unlinked_orders).

    `stated_budget` is null when the shopper stated no budget or the parse was
    ambiguous -- the chatbot never guesses. The audit treats null budget as
    "unknown", never as a violation.
    """

    SURFACE_CHOICES = [("chat", "Chat"), ("ai_curator", "AI Curator")]

    chat = models.ForeignKey(
        Chat, null=True, blank=True, on_delete=models.SET_NULL,
        to_field="chat_id", related_name="advisor_telemetry",
    )
    chat_id_raw = models.CharField(max_length=255, db_index=True)
    shop = models.CharField(max_length=255, db_index=True, default="")
    surface = models.CharField(max_length=20, choices=SURFACE_CHOICES, default="chat")
    stated_budget = models.DecimalField(max_digits=12, decimal_places=2, null=True, blank=True)
    currency = models.CharField(max_length=3, default="USD")
    # [{key, title, price, is_hero}] in advisor order; hero is picks[0].
    picks = models.JSONField(default=list)
    emitted_at = models.DateTimeField(null=True, blank=True, db_index=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        constraints = [
            # Re-emits of the same advisor run are idempotent: the natural
            # dedupe key (chat_id, surface, emitted_at) updates the row.
            models.UniqueConstraint(
                fields=["chat_id_raw", "surface", "emitted_at"],
                name="unique_advisor_telemetry_emit",
            ),
        ]
        indexes = [
            models.Index(fields=["shop", "created_at"]),
        ]
        ordering = ["-created_at"]

    def __str__(self):
        return f"{self.shop} {self.surface} telemetry -> chat {self.chat_id_raw}"


class CartEvent(models.Model):
    """One add-to-cart event against an AI storefront session.

    Ingested via POST /api/cost/log_cart_event/ (fire-and-forget, same
    unauthenticated shape as log_attribution/), emitted by the chatbot
    whenever a shopper with an active PendingAttribution session adds to
    cart -- either the P4 draft-cart confirm in chat (`p4_chat`) or a theme
    Add button the widget's cart interceptor observes (`storefront`).
    Powers the cart funnel (Step 3): shopping sessions -> sessions with a
    cart event -> sessions with an attributed order.

    The session join key is the chatbot's PendingAttribution session id,
    posted as `session_id`; `conversation_id` is the conversation UUID
    (== Chat.chat_id) when the session has one, null for search-only
    sessions -- the row still counts toward that session's funnel step.
    `occurred_at` is the chatbot-side event time and part of the dedupe
    key: the chatbot retries emits up to 3x, and a redelivery carries the
    identical occurred_at, so update_or_create on the natural key absorbs
    it instead of double-counting the funnel.
    """

    SOURCE_CHOICES = [("p4_chat", "P4 Chat"), ("storefront", "Storefront")]

    shop = models.CharField(max_length=255, db_index=True, default="")
    session_id = models.CharField(max_length=255, db_index=True)
    conversation_id = models.CharField(max_length=255, null=True, blank=True, db_index=True)
    source = models.CharField(max_length=20, choices=SOURCE_CHOICES)
    # [{variant_id, quantity, price}] as sent by the chatbot; price in shop
    # currency units. Kept verbatim -- the funnel only needs the event and
    # its cart_value, item detail is for later drill-down.
    items = models.JSONField(default=list)
    cart_value = models.DecimalField(max_digits=12, decimal_places=2, default=0)
    currency = models.CharField(max_length=3, default="USD")
    occurred_at = models.DateTimeField(db_index=True)
    # Received timestamp (row creation), distinct from occurred_at.
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        constraints = [
            # Chatbot emit retries are idempotent: a redelivery of the same
            # event (same shop/session/source/occurred_at) updates the row
            # instead of double-counting the funnel.
            models.UniqueConstraint(
                fields=["shop", "session_id", "source", "occurred_at"],
                name="unique_cart_event_delivery",
            ),
        ]
        indexes = [
            models.Index(fields=["shop", "occurred_at"]),
        ]
        ordering = ["-occurred_at"]

    def __str__(self):
        return f"{self.shop} cart {self.source} session {self.session_id} @ {self.occurred_at}"


class AlertRule(models.Model):
    """C4a: an operator-configured alert rule. Thresholds live in the DB so
    they are editable from the dashboard settings surface without a deploy.

    Threshold units depend on rule_type:
      cost_per_conversation -- USD/conversation; breach when cost > threshold
      spend_anomaly          -- multiple of trailing baseline; breach when
                                month-to-date spend > expected * threshold
      cache_hit_rate_drop    -- percentage points; breach when the cache
                                savings_pct falls > threshold vs last month
      eval_score_drop        -- absolute score points; breach when the avg
                                evaluation score falls > threshold vs last month
    """

    RULE_TYPES = [
        ("cost_per_conversation", "Cost per conversation above $X"),
        ("spend_anomaly", "Spend anomaly vs trailing baseline"),
        ("cache_hit_rate_drop", "Cache savings-rate drop"),
        ("eval_score_drop", "Eval score drop"),
    ]

    rule_type = models.CharField(max_length=32, choices=RULE_TYPES, db_index=True)
    name = models.CharField(max_length=255)
    threshold = models.FloatField()
    cooldown_hours = models.IntegerField(default=24)
    enabled = models.BooleanField(default=True, db_index=True)
    # Rising-edge state: True while the metric is currently breaching, so the
    # evaluator fires once per breach episode (plus the cooldown backstop).
    breached = models.BooleanField(default=False)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["rule_type", "name"]

    def __str__(self):
        state = "breaching" if self.breached else "ok"
        return f"AlertRule {self.name} [{self.rule_type} > {self.threshold}] ({state})"


class AlertFiring(models.Model):
    """C4a: one fired alert -- the durable history behind "fire once per
    breach". acknowledged_at tracks the operator's dismissal (C4b visible
    memory); the most recent firing per rule also drives the cooldown."""

    rule = models.ForeignKey(AlertRule, on_delete=models.CASCADE, related_name="firings")
    fired_at = models.DateTimeField(auto_now_add=True, db_index=True)
    metric_value = models.FloatField(null=True, blank=True)
    headline = models.TextField()
    evidence = models.JSONField(null=True, blank=True)  # C1-idiom stat citations
    acknowledged_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ["-fired_at"]

    def __str__(self):
        return f"AlertFiring {self.rule.name} @ {self.fired_at:%Y-%m-%d %H:%M}"


class OperatorPreference(models.Model):
    """C4b: visible preference memory. Single-operator app, so preferences
    are keyed by name, shown on the dashboard settings surface, and editable
    / deletable there -- never silent cookies. Known keys: dashboard_month
    (last viewed YYYY-MM)."""

    key = models.CharField(max_length=64, unique=True)
    value = models.JSONField()
    updated_at = models.DateTimeField(auto_now=True)

    def __str__(self):
        return f"OperatorPreference {self.key}"
