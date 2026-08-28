from django.contrib import admin

from .models import Chat


@admin.register(Chat)
class ChatAdmin(admin.ModelAdmin):
    list_display = (
        "chat_id",
        "investigation_status",
        "flagged_by",
        "flagged_at",
        "github_issue_number",
        "evaluation_score",
        "timestamp",
    )
    list_editable = ("investigation_status",)
    list_filter = ("investigation_status", "model")
    search_fields = ("chat_id", "flag_reason", "github_issue_url", "linear_issue_url")
    readonly_fields = ("chat_id", "timestamp")
