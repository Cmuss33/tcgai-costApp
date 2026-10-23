# Taxonomy v3 stamps (Intent Alignment Pack §6.3A, 2026-10-22).
# Additive only: every v2 field and stamp is preserved untouched.

from django.db import migrations, models


INTENT_V3_CHOICES = [
    ("product_lookup", "Product lookup"),
    ("recommendation_discovery", "Recommendation / discovery"),
    ("market_research", "Market research"),
    ("deck_help", "Deck help"),
    ("sell_trade", "Sell / trade"),
    ("store_policy", "Store policy"),
    ("grading_services", "Grading services"),
    ("store_events", "Store events"),
    ("store_info_contact", "Store info / contact"),
    ("order_tracking", "Order tracking"),
    ("post_purchase_service", "Post-purchase service"),
    ("account_service", "Account service"),
    ("cart_checkout_assistance", "Cart / checkout assistance"),
    ("out_of_bounds_or_manipulation", "Out of bounds / manipulation"),
    ("no_clear_intent", "No clear intent"),
    ("unknown_unclassified", "Unknown — unclassified"),
]


class Migration(migrations.Migration):

    dependencies = [
        ("cost_management", "0032_cartevent_is_bundle_cartevent_surface"),
    ]

    operations = [
        migrations.AddField(
            model_name="chat",
            name="intent_primary_v3",
            field=models.CharField(blank=True, choices=INTENT_V3_CHOICES, db_index=True, max_length=40, null=True),
        ),
        migrations.AddField(
            model_name="chat",
            name="intent_secondary_v3",
            field=models.JSONField(blank=True, null=True),
        ),
        migrations.AddField(
            model_name="chat",
            name="intent_confidence_v3",
            field=models.FloatField(blank=True, null=True),
        ),
        migrations.AddField(
            model_name="chat",
            name="intent_classifier_version_v3",
            field=models.CharField(blank=True, help_text="Taxonomy version the v3 stamp was made under (2026-10-22.1).", max_length=20, null=True),
        ),
        migrations.AddField(
            model_name="chat",
            name="intent_source_v3",
            field=models.CharField(blank=True, help_text="What produced the v3 stamp: 'rules:v3' or 'llm:<model>:v3' -- verify backfills by reading these stamps, never by job status.", max_length=60, null=True),
        ),
        migrations.AddField(
            model_name="chat",
            name="subject_v3",
            field=models.JSONField(blank=True, null=True),
        ),
        migrations.AddField(
            model_name="chat",
            name="mission_v3",
            field=models.JSONField(blank=True, null=True),
        ),
        migrations.AddField(
            model_name="chat",
            name="surface_v3",
            field=models.CharField(choices=[("chat", "Chat"), ("curator", "Curator")], db_index=True, default="chat", max_length=16),
        ),
        migrations.AddField(
            model_name="chat",
            name="unknown_reason_v3",
            field=models.CharField(blank=True, max_length=40, null=True),
        ),
        migrations.AddField(
            model_name="chat",
            name="outcome_v3",
            field=models.CharField(blank=True, choices=[("purchased", "Purchased"), ("cart_no_purchase", "Added to cart, no purchase"), ("neither", "Neither"), ("not_observed", "Not observed")], db_index=True, max_length=20, null=True),
        ),
        migrations.AddField(
            model_name="chat",
            name="intent_v3_at",
            field=models.DateTimeField(blank=True, null=True),
        ),
        migrations.AddField(
            model_name="chat",
            name="intent_primary_human_v3",
            field=models.CharField(blank=True, choices=INTENT_V3_CHOICES, max_length=40, null=True),
        ),
        migrations.AddField(
            model_name="chat",
            name="intent_correction_source_v3",
            field=models.CharField(blank=True, default="", max_length=120),
        ),
        migrations.AddField(
            model_name="chat",
            name="intent_corrected_at_v3",
            field=models.DateTimeField(blank=True, null=True),
        ),
    ]
