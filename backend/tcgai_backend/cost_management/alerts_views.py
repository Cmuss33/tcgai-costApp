"""C4b: visible preference memory -- dashboard settings surface API.

Alert rules (C4a) are DB-backed so thresholds are editable without a deploy;
operator preferences (dashboard scope memory) are shown and editable here,
never silent cookies. All routes are logged-in dashboard endpoints.
"""
import json
import logging

from django.http import JsonResponse
from django.views.decorators.csrf import csrf_exempt
from django.views.decorators.http import require_http_methods

from .alerts import RULE_META
from .api_auth import api_login_required
from .models import AlertRule, AlertFiring, OperatorPreference

logger = logging.getLogger(__name__)


def _rule_payload(rule):
    meta = RULE_META.get(rule.rule_type, {})
    last = rule.firings.order_by("-fired_at").first()
    return {
        "id": rule.id,
        "rule_type": rule.rule_type,
        "type_label": meta.get("label", rule.rule_type),
        "unit": meta.get("unit", ""),
        "hint": meta.get("hint", ""),
        "name": rule.name,
        "threshold": rule.threshold,
        "cooldown_hours": rule.cooldown_hours,
        "enabled": rule.enabled,
        "breached": rule.breached,
        "last_fired_at": last.fired_at.isoformat() if last else None,
    }


def _firing_payload(firing):
    return {
        "id": firing.id,
        "rule_id": firing.rule_id,
        "rule_name": firing.rule.name,
        "fired_at": firing.fired_at.isoformat(),
        "metric_value": firing.metric_value,
        "headline": firing.headline,
        "evidence": firing.evidence,
        "acknowledged_at": firing.acknowledged_at.isoformat() if firing.acknowledged_at else None,
    }


def _read_json(request):
    try:
        return json.loads(request.body.decode() or "{}")
    except (ValueError, UnicodeDecodeError):
        return None


def _validate_rule_fields(data, for_create):
    """Returns (cleaned, error). cleaned has name/threshold/cooldown_hours/enabled."""
    if data is None:
        return None, "invalid JSON body"
    if for_create:
        rule_type = data.get("rule_type")
        if rule_type not in RULE_META:
            return None, f"rule_type must be one of {sorted(RULE_META)}"
    try:
        threshold = float(data.get("threshold"))
    except (TypeError, ValueError):
        return None, "threshold must be a number"
    if threshold <= 0:
        return None, "threshold must be positive"
    try:
        cooldown_hours = int(data.get("cooldown_hours", 24))
    except (TypeError, ValueError):
        return None, "cooldown_hours must be an integer"
    if cooldown_hours < 1:
        return None, "cooldown_hours must be at least 1"
    name = (data.get("name") or "").strip()
    enabled = data.get("enabled", True)
    if not isinstance(enabled, bool):
        return None, "enabled must be true/false"
    return {
        "name": name,
        "threshold": threshold,
        "cooldown_hours": cooldown_hours,
        "enabled": enabled,
    }, None


@api_login_required
@csrf_exempt
@require_http_methods(["GET", "POST"])
def alert_rules(request):
    if request.method == "GET":
        rules = AlertRule.objects.prefetch_related("firings").order_by("id")
        firings = AlertFiring.objects.select_related("rule").order_by("-fired_at")[:20]
        return JsonResponse({
            "rules": [_rule_payload(r) for r in rules],
            "recent_firings": [_firing_payload(f) for f in firings],
            "rule_types": {
                k: {"label": v["label"], "unit": v["unit"], "hint": v["hint"]}
                for k, v in RULE_META.items()
            },
        })
    # POST: create a rule.
    data = _read_json(request)
    cleaned, error = _validate_rule_fields(data, for_create=True)
    if error:
        return JsonResponse({"error": error}, status=400)
    rule = AlertRule.objects.create(
        rule_type=data["rule_type"],
        name=cleaned["name"] or RULE_META[data["rule_type"]]["label"],
        threshold=cleaned["threshold"],
        cooldown_hours=cleaned["cooldown_hours"],
        enabled=cleaned["enabled"],
    )
    logger.info(
        "[alerts] alert_rule_created rule=%s type=%s threshold=%s cooldown_h=%s",
        rule.id, rule.rule_type, rule.threshold, rule.cooldown_hours,
    )
    return JsonResponse({"rule": _rule_payload(rule)}, status=201)


@api_login_required
@csrf_exempt
@require_http_methods(["PUT", "DELETE"])
def alert_rule_detail(request, rule_id):
    try:
        rule = AlertRule.objects.get(id=rule_id)
    except AlertRule.DoesNotExist:
        return JsonResponse({"error": "not found"}, status=404)
    if request.method == "DELETE":
        rule.delete()
        logger.info("[alerts] alert_rule_deleted rule=%s", rule_id)
        return JsonResponse({"deleted": rule_id})
    data = _read_json(request)
    cleaned, error = _validate_rule_fields(data, for_create=False)
    if error:
        return JsonResponse({"error": error}, status=400)
    # rule_type is immutable: changing it changes what the threshold means.
    if cleaned["name"]:
        rule.name = cleaned["name"]
    rule.threshold = cleaned["threshold"]
    rule.cooldown_hours = cleaned["cooldown_hours"]
    rule.enabled = cleaned["enabled"]
    rule.save()
    logger.info(
        "[alerts] alert_rule_updated rule=%s threshold=%s cooldown_h=%s enabled=%s",
        rule.id, rule.threshold, rule.cooldown_hours, rule.enabled,
    )
    return JsonResponse({"rule": _rule_payload(rule)})


@api_login_required
@csrf_exempt
@require_http_methods(["POST"])
def alert_firing_acknowledge(request, firing_id):
    """C4b: visible dismissal -- acknowledging records when the operator saw
    the alert; it does not clear the breach itself."""
    from django.utils import timezone

    try:
        firing = AlertFiring.objects.select_related("rule").get(id=firing_id)
    except AlertFiring.DoesNotExist:
        return JsonResponse({"error": "not found"}, status=404)
    if firing.acknowledged_at is None:
        firing.acknowledged_at = timezone.now()
        firing.save(update_fields=["acknowledged_at"])
        logger.info(
            "[alerts] alert_acknowledged firing=%s rule=%s",
            firing.id, firing.rule_id,
        )
    return JsonResponse({"firing": _firing_payload(firing)})


@api_login_required
@csrf_exempt
@require_http_methods(["GET", "PUT"])
def preferences(request):
    if request.method == "GET":
        prefs = {p.key: p.value for p in OperatorPreference.objects.all()}
        # Internal bookkeeping keys are not operator preferences; keep the
        # surface to what the dashboard settings UI shows.
        prefs.pop("alerts_last_eval_month", None)
        return JsonResponse({"preferences": prefs})
    data = _read_json(request)
    if data is None:
        return JsonResponse({"error": "invalid JSON body"}, status=400)
    key = (data.get("key") or "").strip()
    if not key or key == "alerts_last_eval_month":
        return JsonResponse({"error": "key is required"}, status=400)
    if "value" not in data:
        return JsonResponse({"error": "value is required"}, status=400)
    pref, _ = OperatorPreference.objects.update_or_create(
        key=key, defaults={"value": data["value"]}
    )
    return JsonResponse({"key": pref.key, "value": pref.value})


@api_login_required
@csrf_exempt
@require_http_methods(["DELETE"])
def preference_detail(request, key):
    deleted, _ = OperatorPreference.objects.filter(key=key).exclude(
        key="alerts_last_eval_month"
    ).delete()
    if not deleted:
        return JsonResponse({"error": "not found"}, status=404)
    return JsonResponse({"deleted": key})
