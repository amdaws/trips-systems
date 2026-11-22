"""What's booked and what's still to book: the Transport page's summary and filter, and the Booked or
To book mark on flights and trains on the trip page (for the owner and the secret link, never visitors).

A leg is booked when its status is Booked (adding a confirmation books it). Metro rides and walks are
paid on the day, so they're never "to book" and aren't counted. A planned train also says when its
tickets go on sale: trip.train_ticket_window days before it leaves (the same rule as the trip check).
"""

from datetime import timedelta

from django.utils import timezone

from .models import BookingStatus, Leg

PAID_ON_THE_DAY = (Leg.Mode.METRO, Leg.Mode.WALK)
GROUPS = (("flight", "Flights", "flight"), ("rail", "Trains", "train"), ("road", "Boats and road", "road"))  # key, label, swatch
GROUP_OF = {Leg.Mode.FLIGHT: "flight", Leg.Mode.TRAIN: "rail"}  # anything else: "road"


def _day(value):
    return f"{value:%a} {value.day} {value:%b}"


def mark(legs, trip, today=None, modes=None):
    """Set leg.booking on each leg (and return the legs): None for a metro ride or walk, or one not in
    `modes` when given; else {"booked", "opens" (a planned train's sale date, or None), "on_sale", "sale"}."""
    today = today or timezone.localdate()
    for leg in legs:
        if leg.mode in PAID_ON_THE_DAY or (modes is not None and leg.mode not in modes):
            leg.booking = None
            continue
        booked = leg.status == BookingStatus.BOOKED
        opens = leg.depart_date - timedelta(days=trip.train_ticket_window) if leg.mode == Leg.Mode.TRAIN and not booked else None
        on_sale = bool(opens and opens <= today)
        sale = ""
        if opens:
            sale = f"On sale since {_day(opens)}" if on_sale else f"On sale {_day(opens)}"
        leg.booking = {"booked": booked, "opens": opens, "on_sale": on_sale, "sale": sale}
    return legs


def summary(legs):
    """Counts for marked legs: total, booked, to_book, on_sale (planned trains on sale now), percent
    booked, groups [{key, label, swatch, booked, total}] for the flights, trains and the rest, and skipped
    (metro rides and walks, not counted)."""
    counted = [leg for leg in legs if leg.booking]
    booked = sum(1 for leg in counted if leg.booking["booked"])
    groups = []
    for key, label, swatch in GROUPS:
        group = [leg for leg in counted if GROUP_OF.get(leg.mode, "road") == key]
        if group:
            groups.append({"key": key, "label": label, "swatch": swatch, "total": len(group),
                           "booked": sum(1 for leg in group if leg.booking["booked"])})
    return {
        "total": len(counted), "booked": booked, "to_book": len(counted) - booked,
        "on_sale": sum(1 for leg in counted if leg.booking["on_sale"]),
        "percent": round(100 * booked / len(counted)) if counted else 0,
        "groups": groups, "skipped": len(legs) - len(counted),
    }


SHOW = {"to-book": lambda leg: leg.booking and not leg.booking["booked"], "booked": lambda leg: leg.booking and leg.booking["booked"]}


def only(legs, show):
    """The legs a Transport page filter shows: "to-book", "booked", or anything else for all of them."""
    keep = SHOW.get(show)
    return [leg for leg in legs if keep(leg)] if keep else list(legs)
