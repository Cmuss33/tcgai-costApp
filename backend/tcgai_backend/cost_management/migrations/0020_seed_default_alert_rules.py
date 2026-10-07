"""C4a: seed the four conservative default alert rules.

Thresholds sit well above the C3 verdict bars (alert fatigue is the
called-out risk). Every field stays editable from the dashboard settings
surface without a deploy; rules can be disabled or deleted there too.
"""
from django.db import migrations


DEFAULT_RULES = [
    {
        "rule_type": "cost_per_conversation",
        "name": "Cost per conversation above $0.20",
        "threshold": 0.20,
        "cooldown_hours": 24,
    },
    {
        "rule_type": "spend_anomaly",
        "name": "Spend anomaly (2x trailing baseline)",
        "threshold": 2.0,
        "cooldown_hours": 24,
    },
    {
        "rule_type": "cache_hit_rate_drop",
        "name": "Cache savings-rate drop (15pp)",
        "threshold": 15.0,
        "cooldown_hours": 24,
    },
    {
        "rule_type": "eval_score_drop",
        "name": "Eval score drop (8 pts)",
        "threshold": 8.0,
        "cooldown_hours": 24,
    },
]


def seed_rules(apps, schema_editor):
    AlertRule = apps.get_model("cost_management", "AlertRule")
    for spec in DEFAULT_RULES:
        AlertRule.objects.get_or_create(
            rule_type=spec["rule_type"],
            name=spec["name"],
            defaults={
                "threshold": spec["threshold"],
                "cooldown_hours": spec["cooldown_hours"],
                "enabled": True,
            },
        )


def unseed_rules(apps, schema_editor):
    AlertRule = apps.get_model("cost_management", "AlertRule")
    AlertRule.objects.filter(
        rule_type__in=[s["rule_type"] for s in DEFAULT_RULES],
        name__in=[s["name"] for s in DEFAULT_RULES],
    ).delete()


class Migration(migrations.Migration):

    dependencies = [
        ("cost_management", "0019_alertrule_operatorpreference_and_more"),
    ]

    operations = [
        migrations.RunPython(seed_rules, unseed_rules),
    ]
