from django.conf import settings
from django.contrib import admin, messages

from .models import Chat, CostMethodologyChange


@admin.register(Chat)
class ChatAdmin(admin.ModelAdmin):
    list_display = (
        "chat_id",
        "shop",
        "likely_automated",
        "investigation_status",
        "flagged_by",
        "flagged_at",
        "github_issue_number",
        "evaluation_score",
        "timestamp",
    )
    list_editable = ("investigation_status", "likely_automated")
    list_filter = ("likely_automated", "investigation_status", "model", "shop")
    search_fields = ("chat_id", "flag_reason", "github_issue_url", "linear_issue_url")
    readonly_fields = ("chat_id", "timestamp")
    actions = ("attribute_to_production_shop",)

    @admin.action(description="Attribute selected chats to production shop")
    def attribute_to_production_shop(self, request, queryset):
        """Bulk-attribute historical shop="" ("Unknown") chats to the
        production shop. Only rows with an empty shop are touched --
        already-tagged rows are never overwritten. No-ops with an error
        message when PRODUCTION_SHOPS is not configured."""
        production_shops = list(getattr(settings, "PRODUCTION_SHOPS", []) or [])
        if not production_shops:
            self.message_user(
                request,
                "PRODUCTION_SHOPS is not configured -- no chats were updated. "
                "Set the PRODUCTION_SHOPS env var first.",
                level=messages.ERROR,
            )
            return
        updated = queryset.filter(shop="").update(shop=production_shops[0])
        self.message_user(
            request,
            f"Attributed {updated} chat(s) to {production_shops[0]}.",
            level=messages.SUCCESS,
        )


@admin.register(CostMethodologyChange)
class CostMethodologyChangeAdmin(admin.ModelAdmin):
    list_display = ("date", "category", "description")
    list_filter = ("category",)
    search_fields = ("description",)
    ordering = ("-date",)
