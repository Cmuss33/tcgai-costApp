"""
One-off data repair: relink an AttributedOrder row to the Chat it actually
belongs to.

Why this exists (2026-10-13, PvpShoppe order 46259): that order's
AttributedOrder row was created by a manual recovery POST to
log_attribution whose chat_id was the attribution *session* id
(attr_...) instead of the conversation UUID. Chats register under
conversation UUIDs, so the row's chat_id_raw matches no Chat, its chat FK
is null, and commercial_impact counts the row as an `unlinked_orders`
canary -- including its influenced revenue -- even though the conversation
was our own end-to-end test. Since PR #142, commercial_impact excludes
orders whose linked chat is a probe; relinking this row to the real
(now probe-tagged) conversation removes the test revenue through that
existing mechanism. The order itself, its amounts and its history are NOT
deleted or altered -- only the chat linkage changes.

This command is deliberately paranoid: every identifying value must be
stated by the operator and is checked against the row before anything is
written, and the default mode is a dry run.

Usage:
    python manage.py relink_attribution_chat \
        --shop <stored shop string> \
        --order-id 46259 \
        --expect-chat-id-raw attr_1791864956321_kuf3ku1hh \
        --to-chat-id 1edf8bbc-38f6-4310-81fe-7dc751742401 \
        [--commit]

Behaviour notes:
- Dry run (no --commit): prints exactly what would change, writes nothing.
- Already relinked (chat_id_raw AND chat FK both point at --to-chat-id):
  a clean no-op. The command reports "already relinked" and exits 0 in
  BOTH modes, so re-running it to confirm state is safe and never an
  error. This check runs before the --expect-chat-id-raw guard, which
  would otherwise (correctly) refuse a stale expectation.
- If the target chat's is_probe is False the relink still happens --
  linkage correctness stands alone -- but the command warns prominently,
  because the commercial_impact revenue exclusion only applies once the
  linked chat is probe-tagged.
"""
from django.core.management.base import BaseCommand, CommandError
from django.db import transaction

from cost_management.models import AttributedOrder, Chat


class Command(BaseCommand):
    help = (
        "Relink one AttributedOrder row to a different Chat (guarded, "
        "dry-run by default; see module docstring)."
    )

    def add_arguments(self, parser):
        parser.add_argument(
            "--shop", required=True,
            help="The row's stored shop string, exactly as on the AttributedOrder row.",
        )
        parser.add_argument(
            "--order-id", required=True,
            help="The row's order_id, exactly as stored.",
        )
        parser.add_argument(
            "--expect-chat-id-raw", required=True,
            help=(
                "The chat_id_raw the row must currently carry. The command "
                "refuses if the row has anything else -- this is the guard "
                "against relinking the wrong row."
            ),
        )
        parser.add_argument(
            "--to-chat-id", required=True,
            help="The chat_id of the Chat the row should link to. The Chat must exist.",
        )
        parser.add_argument(
            "--commit", action="store_true", default=False,
            help="Actually write the change. Without this flag the command is a dry run.",
        )

    def handle(self, *args, **options):
        shop = options["shop"]
        order_id = options["order_id"]
        expect_raw = options["expect_chat_id_raw"]
        to_chat_id = options["to_chat_id"]
        commit = options["commit"]

        matches = list(
            AttributedOrder.objects.filter(shop=shop, order_id=order_id)
        )
        if not matches:
            raise CommandError(
                f"Refusing: no AttributedOrder matches shop={shop!r} "
                f"order_id={order_id!r}."
            )
        if len(matches) > 1:
            raise CommandError(
                f"Refusing: {len(matches)} AttributedOrder rows match "
                f"shop={shop!r} order_id={order_id!r}; expected exactly one."
            )
        order = matches[0]

        # Already in the target state: clean no-op in both modes (see
        # module docstring). Checked before the expect guard so that a
        # confirmation re-run with the original arguments succeeds.
        if order.chat_id_raw == to_chat_id and order.chat_id == to_chat_id:
            self.stdout.write(
                f"Order {order.order_id} (shop={order.shop!r}) is already "
                f"relinked: chat_id_raw={order.chat_id_raw!r}, "
                f"chat FK -> {order.chat_id!r}. Nothing to do."
            )
            return

        if order.chat_id_raw != expect_raw:
            raise CommandError(
                f"Refusing: order {order.order_id} (shop={order.shop!r}) "
                f"currently has chat_id_raw={order.chat_id_raw!r}, not the "
                f"expected {expect_raw!r}. Nothing was changed."
            )

        target = Chat.objects.filter(chat_id=to_chat_id).first()
        if target is None:
            raise CommandError(
                f"Refusing: no Chat exists with chat_id={to_chat_id!r}. "
                f"Nothing was changed."
            )

        self.stdout.write(f"Order:        {order.order_id} (shop={order.shop!r})")
        self.stdout.write(f"  chat_id_raw: {order.chat_id_raw!r} -> {to_chat_id!r}")
        self.stdout.write(f"  chat FK:     {order.chat_id!r} -> {to_chat_id!r}")
        self.stdout.write(f"Target chat is_probe: {target.is_probe}")
        if not target.is_probe:
            self.stdout.write(
                self.style.WARNING(
                    "WARNING: the target chat is NOT tagged is_probe. The "
                    "relink will still be applied (the linkage is the "
                    "correct one), but commercial_impact will keep counting "
                    "this order's revenue until the chat is tagged as a "
                    "probe/test chat."
                )
            )

        if not commit:
            self.stdout.write(
                "Dry run: nothing was written. Re-run with --commit to apply."
            )
            return

        with transaction.atomic():
            order.chat_id_raw = to_chat_id
            order.chat = target
            order.save(update_fields=["chat_id_raw", "chat"])

        order.refresh_from_db()
        self.stdout.write(
            self.style.SUCCESS(
                f"Relinked: order {order.order_id} (shop={order.shop!r}) "
                f"now has chat_id_raw={order.chat_id_raw!r} and "
                f"chat FK -> {order.chat_id!r}."
            )
        )
