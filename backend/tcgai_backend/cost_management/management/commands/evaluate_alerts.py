"""C4a: evaluate operator alert rules and fire Slack alerts on new breaches.

Run hourly via the Render cron job (see render.yaml). Rules are DB-backed
(AlertRule) so thresholds are editable from the dashboard without a deploy;
each rule fires once per breach episode, gated by its cooldown. Delivery is
the AOP Slack channel via SLACK_ALERTS_WEBHOOK_URL -- fail-silent with a
logged warning when unset (firings are still recorded in the DB).

Usage:
    python manage.py evaluate_alerts
"""
from django.core.management.base import BaseCommand

from cost_management.alerts import evaluate_all_rules


class Command(BaseCommand):
    help = "Evaluate C4 alert rules for the current month; fire Slack alerts on new breaches."

    def handle(self, *args, **options):
        summary = evaluate_all_rules()
        self.stdout.write(
            f"Evaluated {summary['evaluated']} rules for {summary['month']}: "
            f"{summary['fired']} fired, {summary['skipped']} skipped."
        )
