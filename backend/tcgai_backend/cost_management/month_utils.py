from datetime import datetime, timedelta, timezone as dt_timezone

from django.utils import timezone

from .models import Chat

CONVERSATION_START_DATE = datetime(2026, 6, 1, 0, 0, 0, tzinfo=dt_timezone.utc)


def current_month_start():
    return timezone.now().date().replace(day=1)


def lifetime_months():
    """Yield all months (first-of-month dates) from June 1, 2026 to current_month_start()."""
    start = CONVERSATION_START_DATE.date().replace(day=1)
    return list(month_iter(start, current_month_start()))


def parse_month_param(value):
    try:
        return datetime.strptime(value, "%Y-%m").date().replace(day=1)
    except (ValueError, TypeError):
        return None


def month_range(month_start):
    """(aware start, aware end) bracketing the calendar month `month_start` sits in."""
    start = timezone.make_aware(datetime(month_start.year, month_start.month, 1))
    if month_start.month == 12:
        end = timezone.make_aware(datetime(month_start.year + 1, 1, 1))
    else:
        end = timezone.make_aware(datetime(month_start.year, month_start.month + 1, 1))
    return start, end


def next_month(month_start):
    if month_start.month == 12:
        return month_start.replace(year=month_start.year + 1, month=1)
    return month_start.replace(month=month_start.month + 1)


def prev_month(month_start):
    return (month_start.replace(day=1) - timedelta(days=1)).replace(day=1)


def month_iter(first, last):
    month = first
    while month <= last:
        yield month
        month = next_month(month)


def real_chats(qs):
    """Excludes automated/bot traffic (ENG-149/150), test traffic (chat_ids
    containing 'shadowtest'), and non-chat surfaces (advisor/curator/
    narrative/report synthetic IDs like advisor_1699999999) from a Chat
    queryset. None represents genuine shopper demand -- letting them through
    corrupts conversation counts, cost-per-conversation proration, average
    accuracy evaluation scores, and AI monthly insights. See models.py's
    Chat.likely_automated and Chat.surface."""
    return qs.filter(likely_automated=False, surface="chat").exclude(chat_id__icontains="shadowtest")


def apply_shop_filter(qs, shops):
    """Restrict a Chat queryset to the given shop domains.

    `shops` is a list of shop domain strings (from ?shop=, repeatable);
    None/empty means no filtering. Composes with real_chats() -- apply it
    to the already-real-only queryset so bot exclusions are never lost.
    An empty-string entry matches unattributed (pre-migration) rows."""
    if not shops:
        return qs
    return qs.filter(shop__in=list(shops))


def conversation_count(month_start, shops=None):
    start_dt, end_dt = month_range(month_start)
    return apply_shop_filter(
        real_chats(Chat.objects.filter(timestamp__gte=start_dt, timestamp__lt=end_dt)),
        shops,
    ).count()
