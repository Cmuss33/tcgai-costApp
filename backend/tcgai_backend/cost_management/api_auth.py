"""Auth decorator for the JSON API views.

Django's ``login_required`` answers an unauthenticated request with a 302 to
``/accounts/login/``, which this API doesn't serve -- fetch() follows the
redirect and the frontend sees a misleading 404 instead of "not logged in".
``api_login_required`` returns a plain 401 JSON response the frontend can act on.
"""

import hmac
from functools import wraps

from django.conf import settings
from django.http import JsonResponse


def api_login_required(view_func):
    @wraps(view_func)
    def wrapper(request, *args, **kwargs):
        if not request.user.is_authenticated:
            return JsonResponse({"error": "unauthenticated"}, status=401)
        return view_func(request, *args, **kwargs)

    return wrapper


def api_login_or_token_required(view_func):
    """Session login OR the machine token (``X-CostApp-Token`` header).

    The token path exists for one server-to-server reader -- AOP's Command
    Center reads the commercial-impact totals for its Money section, and
    ``api_login_required`` can't work cross-service. It admits GET requests
    only, only while ``COSTAPP_API_TOKEN`` is set (unset/empty disables it
    entirely), and compares in constant time. A missing or wrong token gets
    the exact 401 the login path returns -- the response never hints at
    which credential failed. Use this decorator ONLY on the
    commercial-impact endpoints the token is scoped to (the headline view
    and its per-order drill-down).
    """

    @wraps(view_func)
    def wrapper(request, *args, **kwargs):
        if request.user.is_authenticated:
            return view_func(request, *args, **kwargs)
        expected = getattr(settings, "COSTAPP_API_TOKEN", "") or ""
        provided = request.headers.get("X-CostApp-Token") or ""
        if (
            request.method == "GET"
            and expected
            and provided
            and hmac.compare_digest(
                provided.encode("utf-8"), expected.encode("utf-8")
            )
        ):
            return view_func(request, *args, **kwargs)
        return JsonResponse({"error": "unauthenticated"}, status=401)

    return wrapper
