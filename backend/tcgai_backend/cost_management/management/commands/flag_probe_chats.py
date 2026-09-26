"""
Flags chats created by the AOP monitor's retired synthetic probe as
likely_automated, hiding them from the chat summary, conversation counts and
insights (ENG-164). See cost_management/probe_chats.py for how a probe chat
is recognized and why they are flagged rather than deleted.

Migration 0016 runs this once on deploy. Re-run it if any probe chats
arrived after that migration (for example, if this app deployed before the
probe was removed from AOP). Idempotent and additive only: never un-flags.

Usage:
    python manage.py flag_probe_chats [--dry-run]
"""
from django.core.management.base import BaseCommand

from cost_management.models import Chat, Message
from cost_management.probe_chats import unflagged_probe_chat_ids


class Command(BaseCommand):
    help = "Flags chats created by the retired AOP synthetic probe as likely_automated."

    def add_arguments(self, parser):
        parser.add_argument(
            "--dry-run", action="store_true", default=False,
            help="Report what would be flagged without writing anything.",
        )

    def handle(self, *args, **options):
        chat_ids = unflagged_probe_chat_ids(Chat, Message)
        self.stdout.write(f"Probe chats not yet flagged: {len(chat_ids)}")

        if options["dry_run"]:
            self.stdout.write("Dry run — no changes made. Re-run without --dry-run to apply.")
            return

        if chat_ids:
            Chat.objects.filter(chat_id__in=chat_ids).update(likely_automated=True)
            self.stdout.write(self.style.SUCCESS(f"Flagged {len(chat_ids)} probe chats as likely_automated."))
        else:
            self.stdout.write("Nothing new to flag.")
