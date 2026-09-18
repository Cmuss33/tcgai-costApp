"""Single-sign-on bridge for links arriving from the AOP monitoring dashboard.

AOP's dashboard is itself password-gated. When an operator is looking at a
conversation there and clicks "View in Cost App", we want them dropped
straight into that chat's transcript here -- not a second login screen. AOP
signs a short-lived token server-side (HMAC-SHA256 over an expiry, using a
secret both apps hold -- COSTAPP_SSO_SECRET) and this view verifies it, logs
the browser into a designated Django user via the normal session mechanism,
then redirects into the frontend SPA.

Deliberately NOT used for the plain costapp links that get embedded in
GitHub issues, Slack messages, or email (see AOP's own plan) -- those persist
indefinitely somewhere costapp access shouldn't automatically follow, so
they stay as ordinary links requiring a normal login. This bridge only ever
serves the live, interactive AOP dashboard, where the token is generated
fresh at the moment someone is actually looking at it.
"""

import hashlib
import hmac
import logging
import time

from django.conf import settings
from django.contrib.auth import login
from django.contrib.auth.models import User
from django.http import HttpResponseForbidden, HttpResponseRedirect
from django.views.decorators.http import require_http_methods

logger = logging.getLogger(__name__)


def _verify_token(token, exp):
    """True iff token is a valid, unexpired HMAC-SHA256(secret, exp) hex digest."""
    secret = getattr(settings, "COSTAPP_SSO_SECRET", "")
    if not secret:
        return False
    try:
        exp_int = int(exp)
    except (TypeError, ValueError):
        return False
    if exp_int < int(time.time()):
        return False
    expected = hmac.new(secret.encode(), str(exp_int).encode(), hashlib.sha256).hexdigest()
    return hmac.compare_digest(expected, token or "")


@require_http_methods(["GET"])
def sso_login(request):
    token = request.GET.get("token", "")
    exp = request.GET.get("exp", "")
    redirect_to = request.GET.get("redirect") or "/chats"
    # Only ever redirect within this app's own frontend -- never follow an
    # external URL supplied via the query string.
    if not redirect_to.startswith("/"):
        redirect_to = "/chats"

    if not _verify_token(token, exp):
        logger.warning("[sso] rejected token (invalid or expired)")
        return HttpResponseForbidden("invalid or expired token")

    sso_username = getattr(settings, "COSTAPP_SSO_USERNAME", "")
    if not sso_username:
        logger.error("[sso] COSTAPP_SSO_USERNAME is not configured")
        return HttpResponseForbidden("SSO is not configured")

    try:
        user = User.objects.get(username=sso_username)
    except User.DoesNotExist:
        logger.error("[sso] configured COSTAPP_SSO_USERNAME %r has no matching User", sso_username)
        return HttpResponseForbidden("SSO user not found")

    login(request, user)
    logger.info("[sso] logged in %r via AOP bridge, redirecting to %s", sso_username, redirect_to)

    frontend_origin = settings.COST_APP_PUBLIC_URL.rstrip("/")
    return HttpResponseRedirect(f"{frontend_origin}{redirect_to}")
