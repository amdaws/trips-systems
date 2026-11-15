"""What plans cost, kept on the budget page.

A stay, activity or transport leg has at most one budget line of its own (its first
linked BudgetItem), and its form has one Cost field (save_cost):

- While the plan is Planned, the cost is an estimate (the line's `estimate`), counted in
  the budget's estimate. Setting it never changes the status.
- Once it's Booked, the cost is what the booking cost (`actual`). A plan just marked
  Booked with no cost recorded yet takes its estimate as what it cost.
- Adding a confirmation, or importing one, records what was paid and books the plan
  (record).

The budget's estimate is everything paid so far plus the estimates of plans that are
still Planned (estimate_for). A booked plan counts what it cost, or its estimate while what
it cost isn't recorded (cost_unrecorded). Clearing a booked plan's Cost clears both.
"""

import re
from decimal import Decimal, InvalidOperation

from .models import CURRENCIES, BookingStatus, BudgetItem, Item, Leg, Stay

KNOWN = {code for code, _ in CURRENCIES}
# Longest first, so "HK$" wins over "$". A bare "$" isn't guessed at: it could be any dollar.
SYMBOLS = [
    ("AU$", "AUD"), ("A$", "AUD"), ("US$", "USD"), ("HK$", "HKD"), ("NT$", "TWD"), ("MOP$", "MOP"), ("CN¥", "CNY"),
    ("RMB", "CNY"), ("¥", "CNY"), ("￥", "CNY"), ("€", "EUR"), ("£", "GBP"),
]
MONEY_RE = re.compile(r"(?P<before>[A-Z]{3}|[A-Z]{0,3}[$¥￥€£]|RMB)?\s?(?P<amount>\d[\d,]*(?:\.\d{1,2})?)\s?(?P<after>[A-Z]{3})?")


def parse_money(text):
    """(Decimal amount, currency code) from text like "US$ 25.50", "CNY 1,280" or "¥480"; None if the
    amount or a currency this planner handles can't be told."""
    text = str(text or "").strip()
    match = MONEY_RE.search(text)
    if not match:
        return None
    code = ""
    for part in (match["before"], match["after"]):
        if not part:
            continue
        code = next((c for symbol, c in SYMBOLS if part.upper() == symbol.upper()), part.upper() if part.upper() in KNOWN else "")
        if code:
            break
    try:
        amount = Decimal(match["amount"].replace(",", ""))
    except InvalidOperation:
        return None
    if code not in KNOWN or amount <= 0:
        return None
    return amount, code


def owner_kwargs(obj):
    """The budget line's link to its plan: {"stay": …}, {"leg": …} or {"item": …}."""
    return {Stay: "stay", Leg: "leg", Item: "item"}[type(obj)], obj


def budget_line(obj):
    """The plan's own budget line, or None."""
    return obj.budget_items.order_by("id").first() if obj.pk else None


def _category(obj):
    if isinstance(obj, Stay):
        return BudgetItem.Category.ACCOMMODATION
    if isinstance(obj, Item):
        return BudgetItem.Category.ACTIVITY
    return {Leg.Mode.FLIGHT: BudgetItem.Category.FLIGHT, Leg.Mode.TRAIN: BudgetItem.Category.TRAIN}.get(obj.mode, BudgetItem.Category.TRANSPORT)


def _description(obj, site=""):
    if isinstance(obj, Stay):
        return f"Hotel: {obj.label}"
    if isinstance(obj, Item):
        return f"{site or 'Activity'}: {obj.title}"
    return f"{site + ': ' if site else ''}{obj}"


def trip_of(obj):
    return obj.day.trip if isinstance(obj, Item) else obj.trip


def plan_of(line):
    """The stay, leg or activity a budget line belongs to, or None."""
    return line.stay or line.leg or line.item


def estimate_for(line):
    """(AUD amount this line adds to the budget's estimate, whether that's the plan's estimate).
    A planned plan with an estimate counts that. So does a booked one whose cost was never recorded
    (a confirmation that didn't say what was paid, say): until it is, its estimate is the best
    guess, rather than nothing. Everything else counts what was paid."""
    plan = plan_of(line)
    if plan is not None and line.estimate is not None and (plan.status == BookingStatus.PLANNED or line.actual is None):
        return line.estimate_aud or Decimal(0), True  # a missing exchange rate is flagged on the page
    return line.actual_aud or Decimal(0), False


def cost_unrecorded(line):
    """Whether a booked plan's line still waits for what it cost (it counts its estimate meanwhile)."""
    plan = plan_of(line)
    return plan is not None and plan.status == BookingStatus.BOOKED and line.actual is None


def cost_shown(plan):
    """(amount, currency) the plan's Cost field shows: what it cost once booked, else its estimate
    (each falling back to the other); None if its budget line has neither."""
    line = budget_line(plan)
    return _shown(plan, line) if line is not None else None


def _shown(plan, line):
    paid = (line.actual, line.actual_currency) if line.actual is not None else None
    estimate = (line.estimate, line.estimate_currency) if line.estimate is not None else None
    return (paid or estimate) if plan.status == BookingStatus.BOOKED else (estimate or paid)


def save_cost(plan, change, status_changed=False):
    """After a plan's form is saved: its Cost onto its budget line, as an estimate while Planned or as
    what it cost once Booked. `change` is (amount or None, currency) when the field was changed, else
    False. Returns a message for the page, or ""."""
    line = budget_line(plan)
    if plan.status != BookingStatus.BOOKED:
        if change is False:
            return ""
        set_estimate(plan, *change)
        return f"Saved the estimated cost ({change[1]} {change[0]:,.2f}) to the budget." if change[0] is not None else ""
    if change is not False and change[0] is not None:
        record(plan, *change)
        return f"Saved {change[1]} {change[0]:,.2f} to the budget as what it cost."
    if change is False and status_changed and line and line.actual is None and line.estimate is not None:
        record(plan, line.estimate, line.estimate_currency)  # just booked: the estimate it showed is what it cost
        return f"Booked: {line.estimate_currency} {line.estimate:,.2f} is now on the budget as what it cost."
    if change is not False and line and (line.actual is not None or line.estimate is not None):
        # Cleared on a booked plan: nothing of its cost is left (its old estimate would otherwise show again).
        line.actual = line.estimate = None
        if not line.paid_by_id and not line.notes:
            line.delete()
        else:
            line.save()
    return ""


# ---------- activities: a cost per person, times how many people ----------
#
# An activity's Cost is per person, with People beside it (starting at the number of travellers in the
# trip's Settings). Its budget line holds the total, so the budget's sums are unchanged; the cost per
# person is that total shared out. A confirmation records the total it shows.

def default_people(trip, travellers=None):
    """How many people an activity is for when it doesn't say: the trip's travellers (at least one)."""
    return max(trip.travellers.count() if travellers is None else travellers, 1)


def people_for(item, travellers=None):
    return item.people or default_people(item.day.trip, travellers)


def per_person(total, people):
    return (Decimal(total) / max(people, 1)).quantize(Decimal("0.01"))


def total_from_price(plan, price, per_person=True):
    """A listing price for a plan, as the total its budget line holds: an activity's price is per person
    (and its People is kept, so the split stays as it was priced) unless it's for the whole group
    (`per_person=False`, a private car or package); a stay's or transport's is the price."""
    if not isinstance(plan, Item) or not per_person:
        return Decimal(price)
    people = people_for(plan)
    if plan.people != people:
        plan.people = people
        plan.save(update_fields=["people"])
    return Decimal(price) * people


def activity_cost(item, travellers=None):
    """For the Activities page: {"total", "per_person", "people", "currency", "estimate"} from the
    activity's budget line (already fetched with prefetch_related("budget_items")), or None."""
    lines = sorted(item.budget_items.all(), key=lambda line: line.pk)
    shown = _shown(item, lines[0]) if lines else None
    if shown is None:
        return None
    people = people_for(item, travellers)
    return {"total": shown[0], "currency": shown[1], "people": people, "per_person": per_person(shown[0], people),
            "estimate": item.status == BookingStatus.PLANNED and lines[0].estimate is not None}


def set_people(item, people, travellers=None):
    """Change how many people an activity is for, keeping its cost per person: the totals on its budget
    line (estimate and what it cost) are scaled to match."""
    old = people_for(item, travellers)
    line = budget_line(item)
    if line and people != old:
        for field in ("estimate", "actual"):
            amount = getattr(line, field)
            if amount is not None:
                setattr(line, field, per_person(amount, old) * people)
        line.save()
    item.people = people
    item.save(update_fields=["people"])


def set_estimate(obj, amount, currency="AUD", site=""):
    """Put a plan's estimated cost on its budget line (making the line if needed), or clear it.
    The plan's status is left as it is. Returns the line, or None if there's nothing left on it."""
    name, owner = owner_kwargs(obj)
    line = budget_line(obj)
    if amount is None:
        if line is None:
            return None
        line.estimate = None
        if line.actual is None and not line.paid_by_id and not line.notes:
            line.delete()  # only the estimate was ever on it
            return None
        line.save()
        return line
    line = line or BudgetItem(trip=trip_of(obj), category=_category(obj), description=_description(obj, site)[:200], **{name: owner})
    line.estimate = Decimal(amount)
    line.estimate_currency = currency if currency in KNOWN else "AUD"
    line.save()
    return line


def record(obj, amount, currency="AUD", site=""):
    """Put what the booking cost on the plan's budget line and mark the plan booked. Returns the line."""
    name, owner = owner_kwargs(obj)
    line = budget_line(obj) or BudgetItem(trip=trip_of(obj), category=_category(obj), description=_description(obj, site)[:200],
                                          **{name: owner})
    line.actual = Decimal(amount)
    line.actual_currency = currency if currency in KNOWN else "AUD"
    line.booked = line.purchased = True
    line.save()
    if obj.status != BookingStatus.BOOKED:
        obj.status = BookingStatus.BOOKED
        obj.save(update_fields=["status"])
    return line


def plan_link(plan):
    """The page a plan's details come from: a stay or leg's link, an activity's first link; or ""."""
    if isinstance(plan, Item):
        links = plan.link_list()
        return links[0]["url"] if links else ""
    return plan.url


def missing_details(plan):
    """For a planned stay, activity or transport whose details come from a link: which useful details
    are still missing (so the planner can warn about them). [] otherwise."""
    if plan is None or plan.status != BookingStatus.PLANNED or not (plan.added_from == "link" or plan_link(plan)):
        return []
    line = budget_line(plan)
    missing = [] if line and (line.estimate is not None or line.actual is not None) else ["cost"]
    if isinstance(plan, Stay):
        missing += [label for label, have in (
            ("address", plan.address or plan.location_id), ("phone or email", plan.phone or plan.email),
            ("check-in and check-out times", plan.check_in_time and plan.check_out_time)) if not have]
    elif isinstance(plan, Item):
        missing += [label for label, have in (
            ("address", plan.address or plan.place_id), ("phone or email", plan.phone or plan.email),
            ("opening hours", plan.opening_hours)) if not have]
    else:
        missing += [label for label, have in (("departure time", plan.depart_time), ("operator", plan.operator)) if not have]
    return missing
