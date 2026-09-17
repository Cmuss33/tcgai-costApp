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
    tokens_in = models.IntegerField(default=0)
    tokens_out = models.IntegerField(default=0)
    intent = models.TextField(default='NOT FOUND')
    timestamp = models.DateTimeField(auto_now_add=True)
    evaluation_score = models.IntegerField(null=True, blank=True)

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
    surface, a config change, a traffic incident. Feeds cost_commentary.py's
    grounded narrative so it can say "this delta lines up with a known
    change on this date" instead of inventing a cause -- the same discipline
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
