import json
import logging

from django.conf import settings
from django.contrib.auth.decorators import login_required
from django.db import transaction
from django.http import JsonResponse
from django.utils.timezone import now
from django.views.decorators.csrf import csrf_exempt
from django.views.decorators.http import require_http_methods

from .issue_trackers import IssueRef, IssueTrackerError, github_tracker, linear_tracker
from .models import Chat, Message

logger = logging.getLogger(__name__)

REQUIRED_SETTINGS = [
    "GITHUB_TOKEN",
    "GITHUB_ISSUE_REPO",
    "LINEAR_API_KEY",
    "LINEAR_TEAM_ID",
    "LINEAR_PROJECT_ID",
    "COST_APP_PUBLIC_URL",
]


def _missing_settings():
    return [name for name in REQUIRED_SETTINGS if not getattr(settings, name, "")]


def _build_title(chat_id, reason):
    flat = " ".join(reason.split())
    excerpt = flat[:60] + ("…" if len(flat) > 60 else "")
    return f"Investigate chat {chat_id}: {excerpt}"


def _build_issue_body(chat, messages, reason, username, flag_time):
    lines = [
        "## Flag reason",
        reason,
        "",
        "## Chat metadata",
        f"- chat_id: {chat.chat_id}",
        f"- intent: {chat.intent}",
        f"- eval score: {chat.evaluation_score if chat.evaluation_score is not None else 'not evaluated'}",
        f"- tokens in / out: {chat.tokens_in} / {chat.tokens_out}",
        f"- model: {chat.model}",
        f"- first seen: {chat.timestamp.isoformat() if chat.timestamp else 'unknown'}",
        f"- flagged by: {username} at {flag_time.isoformat()}",
        f"- Cost app: {settings.COST_APP_PUBLIC_URL}/chats?chat={chat.chat_id}",
        "",
        "## Transcript",
    ]
    for msg in messages:
        user_text = msg.content.strip() if (msg.content and msg.content.strip()) else "*(no user text recorded)*"
        lines.append(f"**User:** {user_text}")
        lines.append(f"**Assistant:** {msg.returned_content}")
        lines.append("")
    return "\n".join(lines)


def _persist_flag(chat, reason, username, flag_time, gh_ref, linear_ref, flag_error):
    with transaction.atomic():
        locked = Chat.objects.select_for_update().get(pk=chat.pk)
        locked.investigation_status = "flagged"
        locked.flag_reason = reason
        if not locked.flagged_at:
            locked.flagged_at = flag_time
            locked.flagged_by = username
        locked.github_issue_number = gh_ref.number
        locked.github_issue_url = gh_ref.url
        if linear_ref is not None:
            locked.linear_issue_id = linear_ref.id
            locked.linear_issue_url = linear_ref.url
        locked.flag_error = flag_error
        locked.save(update_fields=[
            "investigation_status", "flag_reason", "flagged_at", "flagged_by",
            "github_issue_number", "github_issue_url", "linear_issue_id",
            "linear_issue_url", "flag_error",
        ])


@login_required
@csrf_exempt
@require_http_methods(["POST"])
def flag_chat(request):
    missing = _missing_settings()
    if missing:
        return JsonResponse(
            {"error": "investigation integration not configured", "missing": missing},
            status=503,
        )

    try:
        data = json.loads(request.body)
    except (ValueError, TypeError):
        return JsonResponse({"error": "invalid JSON body"}, status=400)

    chat_id = data.get("chat_id")
    reason = (data.get("reason") or "").strip()

    if not reason:
        return JsonResponse({"error": "reason is required"}, status=400)

    try:
        chat = Chat.objects.get(chat_id=chat_id)
    except Chat.DoesNotExist:
        return JsonResponse({"error": "Chat not found"}, status=404)

    # Linear-retry branch: GitHub issue exists but Linear never got created.
    if chat.github_issue_number and not chat.linear_issue_id:
        return _create_linear_only(chat)

    if chat.investigation_status in ("flagged", "resolved"):
        return JsonResponse({"error": "chat already flagged"}, status=409)

    messages = list(Message.objects.filter(chat=chat).order_by("timestamp"))
    username = request.user.get_username()
    flag_time = now()
    title = _build_title(chat_id, reason)
    body = _build_issue_body(chat, messages, reason, username, flag_time)

    soft_errors = []

    try:
        gh_ref = github_tracker.create_issue(title, body)
    except IssueTrackerError as exc:
        logger.error("[investigation] chat=%s github create_issue failed: %s", chat_id, exc)
        return JsonResponse(
            {"error": "failed to create GitHub issue", "detail": exc.detail}, status=502
        )
    logger.info("[investigation] chat=%s github issue #%s created", chat_id, gh_ref.number)

    try:
        github_tracker.add_label(gh_ref, settings.GITHUB_TRIGGER_LABEL)
    except IssueTrackerError as exc:
        logger.warning("[investigation] chat=%s github add_label failed: %s", chat_id, exc)
        soft_errors.append(f"trigger label failed: {exc.detail}")

    linear_body = f"GitHub issue: {gh_ref.url}\n\n{body}"
    try:
        linear_ref = linear_tracker.create_issue(title, linear_body)
    except IssueTrackerError as exc:
        logger.error("[investigation] chat=%s linear create_issue failed: %s", chat_id, exc)
        _persist_flag(
            chat, reason, username, flag_time, gh_ref, None,
            "; ".join(soft_errors + [f"linear create failed: {exc.detail}"]),
        )
        return JsonResponse(
            {"investigation_status": "flagged", "github_issue_url": gh_ref.url,
             "linear_error": exc.detail},
            status=200,
        )
    logger.info("[investigation] chat=%s linear issue %s created", chat_id, linear_ref.id)

    try:
        github_tracker.add_comment(gh_ref, f"Linked Linear issue: {linear_ref.url}")
    except IssueTrackerError as exc:
        logger.warning("[investigation] chat=%s github add_comment failed: %s", chat_id, exc)
        soft_errors.append(f"back-link comment failed: {exc.detail}")

    flag_error = "; ".join(soft_errors)
    _persist_flag(chat, reason, username, flag_time, gh_ref, linear_ref, flag_error)

    return JsonResponse(
        {
            "investigation_status": "flagged",
            "github_issue_url": gh_ref.url,
            "linear_issue_url": linear_ref.url,
            "flag_error": flag_error,
        },
        status=200,
    )


def _create_linear_only(chat):
    messages = list(Message.objects.filter(chat=chat).order_by("timestamp"))
    flag_time = chat.flagged_at or now()
    title = _build_title(chat.chat_id, chat.flag_reason or "flagged for investigation")
    body = _build_issue_body(
        chat, messages, chat.flag_reason or "(no reason recorded)", chat.flagged_by or "unknown", flag_time
    )
    linear_body = f"GitHub issue: {chat.github_issue_url}\n\n{body}"
    try:
        linear_ref = linear_tracker.create_issue(title, linear_body)
    except IssueTrackerError as exc:
        logger.error("[investigation] chat=%s linear retry failed: %s", chat.chat_id, exc)
        return JsonResponse(
            {"investigation_status": "flagged", "linear_error": exc.detail}, status=200
        )

    chat.linear_issue_id = linear_ref.id
    chat.linear_issue_url = linear_ref.url
    chat.flag_error = ""
    chat.save(update_fields=["linear_issue_id", "linear_issue_url", "flag_error"])

    try:
        github_tracker.add_comment(
            IssueRef(id="", number=chat.github_issue_number, url=chat.github_issue_url),
            f"Linked Linear issue: {linear_ref.url}",
        )
    except IssueTrackerError as exc:
        logger.warning("[investigation] chat=%s github add_comment (retry) failed: %s", chat.chat_id, exc)

    logger.info("[investigation] chat=%s linear issue %s created (retry)", chat.chat_id, linear_ref.id)
    return JsonResponse(
        {"investigation_status": "flagged", "linear_issue_url": linear_ref.url, "flag_error": ""},
        status=200,
    )
