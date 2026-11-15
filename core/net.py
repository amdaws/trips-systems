def client_ip(request):
    """Return the visitor's real IP address.

    In production, gunicorn only listens on 127.0.0.1, so every request comes
    through Caddy, which sets X-Real-IP from Cloudflare's CF-Connecting-IP
    header (trusted only from Cloudflare's IP ranges).
    """
    return request.META.get("HTTP_X_REAL_IP") or request.META.get("REMOTE_ADDR")
