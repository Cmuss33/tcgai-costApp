"""Calendar-month helpers shared by the insights and stats views."""
from datetime import datetime, timedelta

from django.utils import timezone

from .models import Chat


def current_month_start():
    return timezone.now().date().replace(day=1)


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


def conversation_count(month_start):
    start_dt, end_dt = month_range(month_start)
    return Chat.objects.filter(timestamp__gte=start_dt, timestamp__lt=end_dt).count()
