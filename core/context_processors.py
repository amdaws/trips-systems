from django.conf import settings


def site(request):
    context = {"site": {"name": settings.SITE_NAME, "tagline": settings.SITE_TAGLINE, "admin_path": settings.ADMIN_URL}}
    user = getattr(request, "user", None)
    if user is not None and user.is_authenticated:
        name = user.get_full_name() or user.get_username()
        context["user_initials"] = "".join(word[0] for word in name.split()[:2]).upper() or "?"
    return context
