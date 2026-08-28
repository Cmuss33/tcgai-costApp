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


#TODO: use a unique message_id gotten from claude instead of djagno's
class Message(models.Model):
    chat = models.ForeignKey(Chat, on_delete=models.CASCADE, to_field='chat_id')
    content = models.TextField()
    llm_formatted_message = models.TextField()
    returned_content = models.TextField()
    llm_formatted_returned_message = models.TextField()
    tokens_in = models.IntegerField()
    tokens_out = models.IntegerField()
    model = models.TextField()
    products_shown = models.JSONField(null=True, blank=True)
    timestamp = models.DateTimeField(auto_now_add=True)

    def __str__(self):
        return f"Message in Chat {self.chat.chat_id}"
