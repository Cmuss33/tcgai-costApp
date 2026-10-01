"""
Pre-generates monthly AI conversation insights snapshots off the request path.
Can be run via cron / scheduled jobs (e.g. at month turnover or nightly) so that
users viewing the dashboard experience instant load times (< 50ms) instead of
waiting ~1 minute for on-demand LLM generation.

Usage:
    python manage.py generate_insights
    python manage.py generate_insights --month 2026-09
    python manage.py generate_insights --month 2026-09 --force
    python manage.py generate_insights --all
"""
from datetime import datetime
from django.core.management.base import BaseCommand, CommandError

from cost_management.models import InsightsSnapshot
from cost_management.month_utils import (
    conversation_count,
    current_month_start,
    parse_month_param,
)
from cost_management.insights_views import (
    MIN_CONVERSATIONS,
    _available_months,
    _generate_and_store,
)


class Command(BaseCommand):
    help = "Pre-generates monthly AI conversation insights snapshots in the background."

    def add_arguments(self, parser):
        parser.add_argument(
            "--month",
            type=str,
            help="Target month in YYYY-MM format. Defaults to current month.",
        )
        parser.add_argument(
            "--force",
            action="store_true",
            help="Regenerate even if a snapshot already exists.",
        )
        parser.add_argument(
            "--all",
            action="store_true",
            help="Generate snapshots for all available months missing snapshots.",
        )

    def handle(self, *args, **options):
        month_arg = options.get("month")
        force = options.get("force", False)
        all_months = options.get("all", False)
        current = current_month_start()

        if all_months:
            available = _available_months()
            self.stdout.write(f"Checking {len(available)} available months...")
            processed = 0
            for item in available:
                m_start = datetime.strptime(item["value"], "%Y-%m").date().replace(day=1)
                is_cur = m_start == current
                snap = InsightsSnapshot.objects.filter(month=m_start).first()
                if snap and not force and not is_cur:
                    self.stdout.write(f"  [{item['value']}] Snapshot already exists. Skipping.")
                    continue
                count = conversation_count(m_start)
                if count < MIN_CONVERSATIONS:
                    self.stdout.write(f"  [{item['value']}] Only {count} conversations (< {MIN_CONVERSATIONS}). Skipping.")
                    continue
                self.stdout.write(f"  [{item['value']}] Generating insights ({count} conversations)...")
                payload = _generate_and_store(m_start, is_current=is_cur)
                if payload.get("error"):
                    self.stdout.write(self.style.ERROR(f"    Failed: {payload['error']}"))
                else:
                    self.stdout.write(self.style.SUCCESS(f"    Stored snapshot for {item['value']}."))
                    processed += 1
            self.stdout.write(self.style.SUCCESS(f"Finished processing {processed} months."))
            return

        if month_arg:
            target = parse_month_param(month_arg)
            if not target:
                raise CommandError(f"Invalid month format: '{month_arg}'. Expected YYYY-MM.")
        else:
            target = current

        label = target.strftime("%Y-%m")
        is_cur = target == current
        snap = InsightsSnapshot.objects.filter(month=target).first()

        if snap and not force and not is_cur:
            self.stdout.write(
                self.style.WARNING(
                    f"Snapshot for {label} already exists ({snap.conversations_analyzed} conversations). Use --force to regenerate."
                )
            )
            return

        count = conversation_count(target)
        if count < MIN_CONVERSATIONS:
            self.stdout.write(
                self.style.WARNING(
                    f"Only {count} conversations found for {label} (minimum required is {MIN_CONVERSATIONS}). Skipping."
                )
            )
            return

        self.stdout.write(f"Generating insights for {label} ({count} conversations)...")
        payload = _generate_and_store(target, is_current=is_cur)

        if payload.get("error"):
            self.stdout.write(self.style.ERROR(f"Error generating insights for {label}: {payload['error']}"))
        elif payload.get("insufficient_data"):
            self.stdout.write(self.style.WARNING(f"Insufficient data for {label}."))
        else:
            analyzed = payload.get("conversations_analyzed", 0)
            self.stdout.write(
                self.style.SUCCESS(
                    f"Successfully generated and stored insights for {label} ({analyzed} conversations analyzed)."
                )
            )
