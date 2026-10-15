"""Attribution tripwire: seed the attribution_gap default alert rule.

Separate from 0020's seed so existing deployments gain the rule on
upgrade. The threshold is a floor on real chats in the trailing 14 days
(10) -- below that, zero attributed orders is a quiet store, not a
broken pipeline. Editable from the dashboard settings surface like
every other rule.
"""
from django.db import migrations


RULE = {
    "rule_type": "attribution_gap",
    "name": "Attribution gap (no attributed orders in 14 days)",
    "threshold": 10.0,
    "cooldown_hours": 24,
}


def seed_rule(apps, schema_editor):
    AlertRule = apps.get_model("cost_management", "AlertRule")
    AlertRule.objects.get_or_create(
        rule_type=RULE["rule_type"],
        name=RULE["name"],
        defaults={
            "threshold": RULE["threshold"],
            "cooldown_hours": RULE["cooldown_hours"],
            "enabled": True,
        },
    )


def unseed_rule(apps, schema_editor):
    AlertRule = apps.get_model("cost_management", "AlertRule")
    AlertRule.objects.filter(rule_type=RULE["rule_type"], name=RULE["name"]).delete()


class Migration(migrations.Migration):

    dependencies = [
        ("cost_management", "0027_alter_attributedorder_chat"),
    ]

    operations = [
        migrations.RunPython(seed_rule, unseed_rule),
    ]
