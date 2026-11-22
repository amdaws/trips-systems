"""Australian formats with a 24-hour clock: Django's own en-AU formats write '2:30 p.m.', and this site
always writes times as 14:30. Anything not set here comes from Django's en-AU formats."""

TIME_FORMAT = "H:i"  # '14:30'
DATETIME_FORMAT = "j M Y, H:i"  # '25 Oct 2006, 14:30'
SHORT_DATETIME_FORMAT = "d/m/Y H:i"  # '25/10/2006 14:30'
