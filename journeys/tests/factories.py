"""Small helpers to build test trips without touching the network."""

from datetime import date, time
from decimal import Decimal

from journeys.models import Airline, Day, Item, Leg, Place, Stay, Traveller, Trip
from journeys.routing import refresh_path


def place(name, kind, lat, lon, local="", code="", country="CN", tz="Asia/Shanghai", **extra):
    return Place.objects.create(name=name, name_local=local, kind=kind, lat=lat, lon=lon, code=code, country=country, tz=tz, **extra)


def sample_places():
    return {
        "bne": place("Brisbane International Airport", Place.Kind.AIRPORT, -27.38, 153.12, code="BNE", country="AU", tz="Australia/Brisbane", city="Brisbane"),
        "hkg": place("Hong Kong International Airport", Place.Kind.AIRPORT, 22.31, 113.91, code="HKG", country="HK", tz="Asia/Hong_Kong"),
        "xkl": place("Hong Kong West Kowloon", Place.Kind.STATION, 22.304, 114.165, local="香港西九龍", country="HK", tz="Asia/Hong_Kong"),
        "szn": place("Shenzhen North", Place.Kind.STATION, 22.612, 114.024, local="深圳北"),
        "hongkong": place("Hong Kong", Place.Kind.CITY, 22.2986, 114.1736, local="香港", country="HK", tz="Asia/Hong_Kong"),
        "shenzhen": place("Shenzhen", Place.Kind.CITY, 22.5446, 114.0545, local="深圳"),
    }


def sample_trip(visibility=Trip.Visibility.PUBLIC, **extra):
    places = sample_places()
    Airline.objects.get_or_create(code="CX", defaults={"name": "Cathay Pacific"})
    trip = Trip.objects.create(
        title="Test Trip", slug="test-trip", start_date=date(2027, 1, 1), end_date=date(2027, 1, 3),
        visibility=visibility, summary="A short test journey.", notes="SECRET-TRIP-NOTE", **extra,
    )
    jy = Traveller.objects.create(trip=trip, name="Jack", initials="JY")
    Traveller.objects.create(trip=trip, name="Aaron", initials="AB")
    days = {}
    for offset, (title, city) in enumerate([("Fly to Hong Kong", "hongkong"), ("Train to Shenzhen", "shenzhen"), ("Home", None)]):
        d = date(2027, 1, 1 + offset)
        days[d.day] = Day.objects.create(trip=trip, date=d, title=title, overnight=places[city] if city else None, notes="SECRET-DAY-NOTE")
    Item.objects.create(day=days[1], title="Harbour walk", start_time=time(19, 0), description="Evening stroll", private_notes="SECRET-ITEM-NOTE",
                        is_public=True)
    Item.objects.create(day=days[2], title="Hidden plan", is_public=False)
    flight = Leg.objects.create(
        trip=trip, mode=Leg.Mode.FLIGHT, number="CX156", origin=places["bne"], destination=places["hkg"],
        depart_date=date(2027, 1, 1), depart_time=time(0, 50), arrive_time=time(7, 20), booking_ref="SECRETREF1", status=Leg.Status.BOOKED,
        operator="Cathay Pacific", is_public=True,
    )
    train = Leg.objects.create(
        trip=trip, mode=Leg.Mode.TRAIN, number="G388", origin=places["xkl"], destination=places["szn"],
        depart_date=date(2027, 1, 2), depart_time=time(7, 20), arrive_time=time(7, 44), notes="SECRET-LEG-NOTE", is_public=True,
    )
    for leg in (flight, train):
        refresh_path(leg, allow_network=False)
    Stay.objects.create(
        trip=trip, label="Hong Kong 1", name="SECRET-HOTEL", address="SECRET-ADDRESS", city=places["hongkong"],
        check_in_date=date(2027, 1, 1), check_out_date=date(2027, 1, 2), booking_ref="SECRETBOOKING", status="booked",
    )
    trip.budget_items.create(description="Flights", estimate=Decimal("1000"), actual=Decimal("900"), paid_by=jy)
    return trip, places
