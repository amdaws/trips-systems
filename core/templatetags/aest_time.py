from django import template

from core.changelog import aest_time as _aest_time

register = template.Library()


@register.filter
def aest_time(moment):
    """{{ when|aest_time }} → 'Mon 26 Oct 2026, 12:21 am AEST'."""
    return _aest_time(moment) if moment else ""
