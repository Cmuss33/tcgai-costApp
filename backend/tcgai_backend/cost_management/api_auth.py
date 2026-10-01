"""Auth decorator for the JSON API views.

Django's ``login_required`` answers an unauthenticated request with a 302 to
``/accounts/login/``, which this API doesn't serve -- fetch() follows the
redirect and the frontend sees a misleading 404 instead of "not logged in".
``api_login_required`` returns a plain 401 JSON response the frontend can act on.
"""

from functools import wraps

from django.http import JsonResponse


def api_login_required(view_func):
    @wraps(view_func)
    def wrapper(request, *args, **kwargs):
        if not request.user.is_authenticated:
            return JsonResponse({"error": "unauthenticated"}, status=401)
        return view_func(request, *args, **kwargs)

    return wrapper
