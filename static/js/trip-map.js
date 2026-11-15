/*
 * Trip map: legs drawn by transport type, numbered overnight stops, and
 * switchable layers (Leaflet, grey OpenStreetMap base map, OpenRailwayMap overlay, and
 * Brisbane, Melbourne and Sydney's train, tram and ferry lines in their official colours).
 * Clicking a day in the plan highlights its legs; clicking a leg highlights its day.
 * "Play route" (trains) and "Show route" (metro journeys) buttons in the plan draw
 * that journey with arrows for its direction and send a dot along it.
 * While a flight is in the air, the plane is shown where adsb.lol last heard it, with the
 * path flown so far (or an estimated position when it's out of receiver range).
 *
 * window.tripMap lets the trip page drive the map: select a journey, open a day,
 * filter by transport type, or fade everything but one day. A click on a line
 * asks the page to select that journey ("tripmap:leg" event).
 */
(function () {
  "use strict";

  var el = document.getElementById("trip-map");
  if (!el || !window.L) return;

  function colour(name) { return getComputedStyle(document.documentElement).getPropertyValue(name).trim(); }
  function isDark() {
    return window.siteTheme ? window.siteTheme.isDark() : window.matchMedia("(prefers-color-scheme: dark)").matches;
  }

  // Line styles by transport type, in the page's current (light or dark) colours.
  function buildStyles() {
    var road = { color: colour("--mode-road"), weight: 3 };
    var water = { color: colour("--mode-boat"), weight: 3.5, dashArray: "1 7", lineCap: "round" };
    return {
      flight: { color: colour("--mode-flight"), weight: 2.5, dashArray: "7 7" },
      train: { color: colour("--mode-train"), weight: 4 },
      hsr: { color: colour("--mode-hsr"), weight: 4 },
      boat: water,
      ferry: water,
      bus: road,
      shuttle: road,
      car: road,
      other: road,
      metro: { color: colour("--mode-metro"), weight: 3, dashArray: "8 5" },
      walk: { color: colour("--mode-walk"), weight: 2.5, dashArray: "1 6", lineCap: "round" }
    };
  }
  var STYLES = buildStyles();
  var recolour = [];  // restyle what's drawn when the colour theme changes
  var GROUPS = {
    flight: "Flights", hsr: "High-speed rail", train: "Trains", boat: "Boats and ferries", ferry: "Boats and ferries",
    bus: "Road", shuttle: "Road", car: "Road", other: "Road", metro: "Metro and walking", walk: "Metro and walking"
  };

  function esc(text) {
    return String(text == null ? "" : text).replace(/[&<>"']/g, function (c) {
      return { "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c];
    });
  }

  function placeName(name, local) {
    return "<strong>" + esc(name) + "</strong>" + (local ? ' <span class="local">' + esc(local) + "</span>" : "");
  }

  function hub(name, local, pinyin) {
    return esc(name) + (local && local !== name ? " " + esc(local) : "") + (pinyin ? " <i>(" + esc(pinyin) + ")</i>" : "");
  }

  function legPopup(leg) {
    var cities = leg.from_city === leg.to_city ? esc(leg.from_city) : esc(leg.from_city) + " → " + esc(leg.to_city);
    var html = '<span class="mode mode--' + esc(leg.high_speed ? "hsr" : leg.mode) + '">' + esc(leg.mode_label) +
      (leg.number ? " " + esc(leg.number) : "") + "</span><br><strong>" + cities + "</strong><br>" +
      '<small class="muted">' + hub(leg.from, leg.from_local, leg.from_pinyin) + " → " + hub(leg.to, leg.to_local, leg.to_pinyin) + "</small>" +
      "<br>" + esc(leg.date) + (leg.times ? " · " + esc(leg.times) : "");
    var extra = [leg.operator, leg.duration].filter(Boolean).map(esc).join(" · ");
    if (extra) html += '<br><small class="muted">' + extra + "</small>";
    if (leg.note) html += "<br>" + esc(leg.note);
    if (leg.approx) html += '<br><small class="muted">Route shown approximately</small>';
    return html;
  }

  function newTab(url, text) {
    return '<a href="' + esc(url) + '" target="_blank" rel="noopener noreferrer">' + esc(text) + "</a>";
  }

  function routeSummary(route) {
    return [route.duration, route.fare, route.transfers].filter(Boolean).map(esc).join(" · ");
  }

  function ridePopup(stay, way, ride) {
    return "<strong>" + esc(ride.line) + "</strong> " + esc(ride.from) + " → " + esc(ride.to) +
      (ride.count ? ' <small class="muted">(' + esc(ride.count) + ")</small>" : "") +
      "<br>" + esc(way.kind === "arrival" ? "To " + stay.label : "From " + stay.label) + ": " + routeSummary(way.route) +
      "<br>" + newTab(way.url, "Metro map");
  }

  // A stay: the hotel (for the planner, the secret link and the owner), its nearest metro station and MetroMan links.
  function hotelPopup(stay) {
    var html = (stay.city ? placeName(stay.city, stay.city_local) + "<br>" : "") +
      "<strong>" + esc(stay.name || stay.label) + "</strong>" + (stay.public === false ? ' <small class="muted">(private)</small>' : "");
    if (stay.address) html += "<br><small>" + esc(stay.address) + "</small>";
    html += "<br>" + esc(stay.dates) + (stay.nights ? " · " + stay.nights + (stay.nights === 1 ? " night" : " nights") : "");
    if (stay.weather) html += '<br><small class="muted">' + esc(stay.weather) + "</small>";
    if (stay.metro) {
      html += "<br>Metro: " + placeName(stay.metro.name, stay.metro.local) +
        (stay.metro.exit ? " · Exit " + esc(stay.metro.exit) : "") +
        (stay.metro.distance ? ' <small class="muted">(' + esc(stay.metro.distance) + " away)</small>" : "");
    }
    var links = [];
    if (stay.arrival) links.push(newTab(stay.arrival.url, stay.arrival.same ? "Metro map: " + stay.arrival.station : "Metro map from " + stay.arrival.station + " (arrival)") +
      (stay.arrival.route ? ' <small class="muted">' + routeSummary(stay.arrival.route) + "</small>" : ""));
    if (stay.departure && !stay.departure.same) links.push(newTab(stay.departure.url, "Metro map to " + stay.departure.station + " (departure)") +
      (stay.departure.route ? ' <small class="muted">' + routeSummary(stay.departure.route) + "</small>" : ""));
    if (!links.length && stay.map_url) links.push(newTab(stay.map_url, stay.map_label || stay.network + " metro map"));
    if (links.length) html += "<br>" + links.join("<br>");
    return html;
  }

  var map = L.map(el, { scrollWheelZoom: false, worldCopyJump: true, zoomSnap: 0.5, zoomControl: false });
  map.on("focus click", function () { map.scrollWheelZoom.enable(); });
  L.control.zoom({ position: "topright" }).addTo(map);

  // OpenStreetMap's standard tiles, turned light grey by CSS (.basemap) so the coloured routes stand out.
  L.tileLayer("https://tile.openstreetmap.org/{z}/{x}/{y}.png", {
    maxZoom: 19,
    className: "basemap",
    attribution: '&copy; <a href="https://www.openstreetmap.org/copyright">OpenStreetMap</a> contributors'
  }).addTo(map);

  var railways = L.tileLayer("https://{s}.tiles.openrailwaymap.org/standard/{z}/{x}/{y}.png", {
    subdomains: "abc",
    maxZoom: 19,
    className: "railmap",
    opacity: isDark() ? 0.55 : 0.7,
    attribution: 'Railways: <a href="https://www.openrailwaymap.org/">OpenRailwayMap</a> (CC-BY-SA)'
  });

  var animLayer = L.layerGroup().addTo(map);
  var legsById = {};
  var linesById = {};
  var layersByDay = {};
  var allPoints = [];
  var view = { leg: null, day: null, filter: "all", scope: "trip" };  // what the page has picked

  // Every line's weight and strength from the view: the picked journey stands out, the open day
  // a little; journeys filtered out (or on other days, with "This day") fade right back.
  function restyle() {
    Object.keys(linesById).forEach(function (id) {
      var line = linesById[id], leg = legsById[id];
      var chosen = view.leg !== null && String(leg.id) === String(view.leg);
      var onDay = view.day !== null && String(leg.day) === String(view.day);
      var faded = (view.filter !== "all" && leg.group !== view.filter) || (view.scope === "day" && view.day !== null && !onDay);
      line.setStyle({
        weight: line.options.baseWeight + (chosen ? 3 : onDay && view.leg === null ? 2 : 0),
        opacity: faded ? 0.12 : chosen ? 1 : leg.approx ? 0.5 : 0.9
      });
      if (chosen || (onDay && view.leg === null)) line.bringToFront();
    });
  }

  function fitTo(points, maxZoom) {
    if (points.length) map.flyToBounds(L.latLngBounds(points), { padding: [40, 40], maxZoom: maxZoom || 11, duration: 0.6 });
  }

  function dayPoints(day) {
    var points = [];
    (layersByDay[day] || []).forEach(function (layer) {
      if (layer.getLatLngs) points = points.concat(layer.getLatLngs());
      else if (layer.getLatLng) points.push(layer.getLatLng());
    });
    return points;
  }

  function highlightDay(day, fromPlan) {
    view.day = String(day);
    view.leg = null;
    restyle();
    fitTo(dayPoints(day));

    document.querySelectorAll(".day.is-active").forEach(function (node) { node.classList.remove("is-active"); });
    var card = document.querySelector('.day[data-day="' + day + '"]');
    if (card) {
      card.classList.add("is-active");
      if (!fromPlan) card.scrollIntoView({ behavior: "smooth", block: "nearest" });
    }
  }

  function focusPoints(stops, everything) {
    // Ignore points far from the median stop (e.g. the flight home) for the first view.
    if (stops.length < 3) return everything.length ? everything : stops;
    var lats = stops.map(function (p) { return p[0]; }).sort(function (a, b) { return a - b; });
    var lons = stops.map(function (p) { return p[1]; }).sort(function (a, b) { return a - b; });
    var centre = L.latLng(lats[Math.floor(lats.length / 2)], lons[Math.floor(lons.length / 2)]);
    var near = stops.filter(function (p) { return centre.distanceTo(p) < 2500000; });
    return near.length >= 2 ? near : everything;
  }

  function draw(data) {
    var groups = {};
    data.legs.forEach(function (leg) {
      var kind = leg.high_speed ? "hsr" : leg.mode;
      var style = Object.assign({ opacity: leg.approx ? 0.5 : 0.9 }, STYLES[kind] || STYLES.other);
      if (leg.approx && !style.dashArray) style.dashArray = "4 6";
      style.baseWeight = style.weight;
      legsById[leg.id] = leg;
      var line = L.polyline(leg.path, style).bindPopup(legPopup(leg));
      linesById[leg.id] = line;
      recolour.push(function () { line.setStyle({ color: (STYLES[kind] || STYLES.other).color }); });
      line.on("click", function () {
        if (document.querySelector("[data-trip-page]")) {
          document.dispatchEvent(new CustomEvent("tripmap:leg", { detail: { id: leg.id, day: leg.day } }));
        } else {
          highlightDay(leg.day, false);
        }
      });
      var name = GROUPS[kind] || "Road";
      (groups[name] = groups[name] || L.layerGroup()).addLayer(line);
      (layersByDay[leg.day] = layersByDay[leg.day] || []).push(line);
      allPoints = allPoints.concat(leg.path);
    });

    var stopLayer = L.layerGroup();
    data.stops.forEach(function (stop) {
      var icon = L.divIcon({
        className: "",
        html: '<div class="stop-marker" style="width:22px;height:22px">' + stop.index + "</div>",
        iconSize: [22, 22],
        iconAnchor: [11, 11]
      });
      var marker = L.marker([stop.lat, stop.lon], { icon: icon, title: stop.name, riseOnHover: true }).bindPopup(
        placeName(stop.name, stop.local) + "<br>" + esc(stop.dates) + " · " + stop.nights + (stop.nights === 1 ? " night" : " nights")
      );
      stopLayer.addLayer(marker);
      stop.days.forEach(function (day) { (layersByDay[day] = layersByDay[day] || []).push(marker); });
      allPoints.push([stop.lat, stop.lon]);
    });

    var hotelLayer = null;
    if (data.stays && data.stays.length) {
      hotelLayer = L.layerGroup();
      data.stays.forEach(function (stay) {
        var marker = null, placed = stay.lat != null;  // visitors get the station and routes, never the hotel
        if (placed) {
          // Signed in, the hotel is where the city is labelled: its stop number and name.
          var icon = stay.index ? L.divIcon({
            className: "", iconSize: null, iconAnchor: [11, 11],
            html: '<div class="stay-label"><span class="stop-marker">' + stay.index + "</span>" + esc(stay.city) + "</div>"
          }) : L.divIcon({ className: "", html: '<div class="hotel-marker">H</div>', iconSize: [20, 20], iconAnchor: [10, 10] });
          marker = L.marker([stay.lat, stay.lon], { icon: icon, title: stay.label, riseOnHover: true }).bindPopup(hotelPopup(stay));
          hotelLayer.addLayer(marker);
        }
        if (stay.metro && stay.metro.lat != null) {
          var walk = placed ? L.polyline([[stay.lat, stay.lon], [stay.metro.lat, stay.metro.lon]], STYLES.walk) : null;
          var station = L.circleMarker([stay.metro.lat, stay.metro.lon], {
            radius: 5, color: colour("--mode-metro"), weight: 2, fillColor: colour("--bg") || "#fff", fillOpacity: 1
          }).bindPopup(hotelPopup(stay));
          if (walk) hotelLayer.addLayer(walk);
          hotelLayer.addLayer(station);
          marker = marker || station;
          recolour.push(function () {
            if (walk) walk.setStyle({ color: STYLES.walk.color });
            station.setStyle({ color: colour("--mode-metro"), fillColor: colour("--bg") || "#fff" });
          });
        }
        [stay.arrival, stay.departure].forEach(function (way, i) {
          if (!way || !way.route) return;
          way.kind = i === 0 ? "arrival" : "departure";
          way.route.rides.forEach(function (ride) {
            if (ride.path.length < 2) return;
            var line = L.polyline(ride.path, ride.walk ? STYLES.walk : { color: ride.colour || colour("--mode-metro"), weight: 4, opacity: 0.85, lineJoin: "round" })
              .bindPopup(ridePopup(stay, way, ride));
            hotelLayer.addLayer(line);
            if (ride.walk) return;
            (ride.dots || ride.path).forEach(function (point) {  // a dot at each station
              hotelLayer.addLayer(L.circleMarker(point, { radius: 2.5, color: ride.colour || colour("--mode-metro"), weight: 1.5, fillColor: "#fff", fillOpacity: 1 }));
            });
            if (!ride.colour) recolour.push(function () { line.setStyle({ color: colour("--mode-metro") }); });
          });
        });
        if (marker) stay.days.forEach(function (day) { (layersByDay[day] = layersByDay[day] || []).push(marker); });
      });
    }

    // City train, metro, tram and ferry lines, under everything else (not part of the route's extent).
    var cityLines = null;
    if (data.lines && data.lines.length) {
      cityLines = L.layerGroup();
      data.lines.forEach(function (line) {
        cityLines.addLayer(L.polyline(line.path, {
          color: line.colour || colour("--mode-metro"), weight: line.mode === "train" ? 3 : 2, opacity: 0.6,
          dashArray: line.mode === "ferry" ? "3 6" : null, lineJoin: "round"
        }).bindTooltip(esc(line.city + " · " + line.name), { sticky: true }));
      });
      cityLines.addTo(map);
    }

    Object.keys(groups).forEach(function (name) { groups[name].addTo(map); });
    stopLayer.addTo(map);
    if (hotelLayer) hotelLayer.addTo(map);
    var overlays = Object.assign({}, groups, { "Overnight stops": stopLayer });
    if (hotelLayer) overlays["Stays and metro routes"] = hotelLayer;
    if (cityLines) overlays["City trains, trams and ferries"] = cityLines;
    overlays["Railway network"] = railways;
    L.control.layers(null, overlays, { collapsed: true, position: "topright" }).addTo(map);
    L.control.scale({ imperial: false, position: "bottomright" }).addTo(map);

    var WholeRoute = L.Control.extend({
      options: { position: "topright" },
      onAdd: function () {
        var button = L.DomUtil.create("a", "leaflet-bar-part");
        var bar = L.DomUtil.create("div", "leaflet-bar");
        bar.appendChild(button);
        button.href = "#";
        button.title = "Show the whole route";
        button.setAttribute("role", "button");
        button.innerHTML = "&#8689;";
        button.style.fontSize = "16px";
        L.DomEvent.on(button, "click", function (event) {
          L.DomEvent.preventDefault(event);
          map.flyToBounds(L.latLngBounds(allPoints), { padding: [24, 24], duration: 0.6 });
        });
        return bar;
      }
    });
    map.addControl(new WholeRoute());

    var start = focusPoints(data.stops.map(function (s) { return [s.lat, s.lon]; })
      .concat((data.stays || []).filter(function (s) { return s.index && s.lat != null; }).map(function (s) { return [s.lat, s.lon]; })), allPoints);
    if (start.length) map.fitBounds(L.latLngBounds(start), { padding: [24, 24] });
    else map.setView([20, 110], 3);
    drawn = true;
    restyle();
    if (view.leg !== null) api.selectLeg(view.leg);
  }

  var drawn = false;
  var api = window.tripMap = {
    highlightDay: highlightDay,
    selectLeg: function (id) {
      view.leg = String(id);
      var leg = legsById[id];
      if (!leg) return;  // drawn once the map data arrives
      view.day = String(leg.day);
      restyle();
      fitTo(leg.path);
    },
    clearLeg: function () { view.leg = null; restyle(); },
    setFilter: function (group) { view.filter = group; restyle(); },
    setScope: function (scope, day) {
      view.scope = scope;
      if (day !== undefined && day !== null) view.day = String(day);
      restyle();
      if (scope === "day" && view.day !== null && drawn) fitTo(view.leg !== null && legsById[view.leg] ? legsById[view.leg].path : dayPoints(view.day));
      if (scope === "trip" && drawn) fitTo(allPoints, 6);
    },
    invalidate: function () { setTimeout(function () { map.invalidateSize(); }, 30); }
  };

  // ---- Route animation: a dot travels the journey, with arrows showing its direction ----

  var reduceMotion = window.matchMedia("(prefers-reduced-motion: reduce)").matches;
  var frame = null, playing = 0;

  function measure(path) {
    var lengths = [0];
    for (var i = 1; i < path.length; i++) lengths.push(lengths[i - 1] + map.distance(path[i - 1], path[i]));
    return lengths;
  }

  // The position `along` metres into a path, and the direction it heads there (degrees on screen).
  function pointAt(path, lengths, along) {
    var i = 1;
    while (i < lengths.length - 1 && lengths[i] < along) i++;
    var f = Math.max(0, Math.min(1, (along - lengths[i - 1]) / ((lengths[i] - lengths[i - 1]) || 1)));
    var a = L.latLng(path[i - 1]), b = L.latLng(path[i]);
    var pa = map.project(a, 10), pb = map.project(b, 10);
    return {
      latlng: L.latLng(a.lat + (b.lat - a.lat) * f, a.lng + (b.lng - a.lng) * f),
      angle: Math.atan2(pb.y - pa.y, pb.x - pa.x) * 180 / Math.PI
    };
  }

  function arrow(spot, tint) {
    return L.marker(spot.latlng, {
      interactive: false, keyboard: false,
      icon: L.divIcon({
        className: "", iconSize: [14, 14], iconAnchor: [7, 7],
        html: '<span class="route-arrow" style="transform: rotate(' + (spot.angle + 45).toFixed(1) + 'deg); border-color: ' + esc(tint) + '"></span>'
      })
    });
  }

  // pieces: [{path: [[lat, lon], …], colour, walk}], in travel order.
  function playRoute(pieces) {
    animLayer.clearLayers();
    if (frame) cancelAnimationFrame(frame);
    var token = ++playing, parts = [], everything = [], total = 0;
    pieces.forEach(function (piece) {
      if (!piece.path || piece.path.length < 2) return;
      var tint = piece.walk ? colour("--mode-walk") : (piece.colour || colour("--mode-metro"));
      animLayer.addLayer(L.polyline(piece.path, piece.walk
        ? { color: tint, weight: 3, dashArray: "1 6", lineCap: "round", interactive: false }
        : { color: tint, weight: 7, opacity: 0.9, lineJoin: "round", interactive: false }));
      var lengths = measure(piece.path), length = lengths[lengths.length - 1];
      if (!piece.walk) {
        var count = Math.max(1, Math.min(12, Math.round(length / 4000)));  // an arrow every 4 km or so
        for (var k = 0; k < count; k++) animLayer.addLayer(arrow(pointAt(piece.path, lengths, length * (k + 0.5) / count), tint));
      }
      parts.push({ path: piece.path, lengths: lengths, start: total, tint: tint });
      total += length;
      everything = everything.concat(piece.path);
    });
    if (!parts.length) return;
    var rect = el.getBoundingClientRect();
    if (rect.bottom < 0 || rect.top > window.innerHeight) el.scrollIntoView({ behavior: "smooth", block: "center" });
    map.flyToBounds(L.latLngBounds(everything), { padding: [40, 40], maxZoom: 15, duration: 0.6 });
    if (reduceMotion) return;

    var dot = L.circleMarker(parts[0].path[0], { radius: 7, color: "#fff", weight: 3, fillColor: parts[0].tint, fillOpacity: 1, interactive: false });
    var duration = Math.max(3000, Math.min(8000, 2000 + total / 150)), started = null;
    function step(now) {
      if (token !== playing) return;
      if (started === null) started = now;
      var t = Math.min(1, (now - started) / duration), along = total * t, part = parts[0];
      parts.forEach(function (candidate) { if (along >= candidate.start) part = candidate; });
      dot.setLatLng(pointAt(part.path, part.lengths, along - part.start).latlng);
      dot.setStyle({ fillColor: part.tint });
      frame = t < 1 ? requestAnimationFrame(step) : null;
    }
    setTimeout(function () {
      if (token !== playing) return;
      animLayer.addLayer(dot);
      frame = requestAnimationFrame(step);
    }, 700);
  }

  document.addEventListener("click", function (event) {
    var button = event.target.closest("[data-animate-route], [data-animate-leg]");
    if (!button) return;
    if (button.hasAttribute("data-animate-leg")) {
      var leg = legsById[button.getAttribute("data-animate-leg")];
      if (leg) playRoute([{ path: leg.path, colour: (STYLES[leg.high_speed ? "hsr" : leg.mode] || STYLES.other).color }]);
      return;
    }
    try {
      playRoute(JSON.parse(button.getAttribute("data-animate-route")));
    } catch (e) { /* a malformed route: nothing to show */ }
  });

  // ---- Live flights: where a flight is while it's in the air (adsb.lol, through live.json) ----
  // The page asks again as often as the answer says (every 30 seconds while a flight is up), and
  // not at all while it's in a background tab.

  var PLANE = '<svg viewBox="0 0 24 24" aria-hidden="true"><path fill="currentColor" d="M12 1.5c.8 0 1.4.7 1.4 1.6V9l8.1 4.6V16l-8.1-2.4v5.2l2.4 1.8V22L12 21l-3.8 1v-1.4l2.4-1.8v-5.2L2.5 16v-2.4L10.6 9V3.1c0-.9.6-1.6 1.4-1.6z"/></svg>';
  var liveLayer = L.layerGroup().addTo(map);
  var live = { flights: [], at: 0, timer: null, paused: false, control: null, ticker: null };

  function ago(secs) {
    if (secs < 90) return "just now";
    var mins = Math.round(secs / 60);
    if (mins < 60) return mins + " min ago";
    return Math.floor(mins / 60) + " h" + (mins % 60 ? " " + (mins % 60) + " min" : "") + " ago";
  }

  function liveHeadline(flight) {
    return { flying: "In the air", out_of_range: "Out of receiver range", landed: "Landed" }[flight.status] || "Not in the air yet";
  }

  function liveFacts(flight) {
    var p = flight.position;
    if (!p) return "The plane shows on the map once it takes off.";
    var secs = p.seen_secs + (Date.now() - live.at) / 1000, facts = [];
    if (flight.status === "flying") {
      if (p.altitude_ft) facts.push((Math.round(p.altitude_ft / 100) * 100).toLocaleString("en-AU") + " ft");
      if (p.kmh) facts.push(p.kmh.toLocaleString("en-AU") + " km/h");
    }
    facts.push((flight.status === "flying" ? "seen " : "last seen ") + ago(secs));
    if (flight.status === "out_of_range") facts.push("the plane's position is estimated");
    return facts.join(" · ");
  }

  function liveRoute(flight) {
    var leg = legsById[flight.id];
    return leg ? (leg.from_city === leg.to_city ? leg.from_city : leg.from_city + " → " + leg.to_city) : "Flight";
  }

  function headingBetween(a, b) {
    var pa = map.project(L.latLng(a), 10), pb = map.project(L.latLng(b), 10);
    return Math.atan2(pb.x - pa.x, pa.y - pb.y) * 180 / Math.PI;
  }

  function planeMarker(latlng, heading, estimated, flight) {
    var leg = legsById[flight.id];
    return L.marker(latlng, {
      keyboard: false, zIndexOffset: 1000, title: estimated ? "Estimated position" : "Live position",
      icon: L.divIcon({
        className: "", iconSize: [32, 32], iconAnchor: [16, 16],
        html: '<div class="live-plane' + (estimated ? " live-plane--estimate" : "") + '" style="transform: rotate(' +
          (Number(heading) || 0).toFixed(0) + 'deg)">' + PLANE + "</div>"
      })
    }).bindPopup(function () {
      return (leg ? legPopup(leg) + "<br>" : "") + "<strong>" + esc(liveHeadline(flight)) + "</strong><br><small class=\"muted\">" +
        esc(liveFacts(flight)) + '</small><br><small class="muted">Live position: <a href="https://adsb.lol/" target="_blank" rel="noopener noreferrer">adsb.lol</a></small>';
    });
  }

  function drawLive() {
    liveLayer.clearLayers();
    var tint = colour("--mode-flight");
    live.flights.forEach(function (flight) {
      if (flight.trail.length > 1) liveLayer.addLayer(L.polyline(flight.trail, { color: tint, weight: 4, opacity: 0.95, interactive: false }));
      var p = flight.position;
      if (!p || flight.status === "landed") return;
      if (flight.status === "out_of_range" && flight.estimate) {
        liveLayer.addLayer(L.polyline([[p.lat, p.lon], flight.estimate], { color: tint, weight: 2, dashArray: "2 6", interactive: false }));
        liveLayer.addLayer(L.circleMarker([p.lat, p.lon], { radius: 4, color: tint, weight: 2, fillColor: colour("--bg") || "#fff", fillOpacity: 1 })
          .bindTooltip("Last seen here"));
        liveLayer.addLayer(planeMarker(flight.estimate, headingBetween([p.lat, p.lon], flight.estimate), true, flight));
      } else {
        liveLayer.addLayer(planeMarker([p.lat, p.lon], p.heading, false, flight));
      }
    });
  }

  function planeAt(flight) {
    var p = flight.position;
    if (!p) return null;
    return flight.status === "out_of_range" && flight.estimate ? flight.estimate : [p.lat, p.lon];
  }

  function showPlane(id) {
    var flight = live.flights.filter(function (f) { return String(f.id) === String(id); })[0];
    var spot = flight && planeAt(flight);
    if (!spot) return;
    var rect = el.getBoundingClientRect();
    if (rect.bottom < 0 || rect.top > window.innerHeight) el.scrollIntoView({ behavior: "smooth", block: "center" });
    map.flyTo(spot, Math.max(map.getZoom(), 6), { duration: 0.8 });
  }

  var LiveControl = L.Control.extend({
    options: { position: "topleft" },
    onAdd: function () {
      var box = L.DomUtil.create("div", "live-control");
      L.DomEvent.disableClickPropagation(box);
      box.setAttribute("aria-live", "polite");
      return box;
    }
  });

  // The status lines: on the map, on each flight in the plan, and in its details panel.
  function showLiveStatus() {
    var shown = {};
    live.flights.forEach(function (flight) {
      shown[flight.id] = true;
      var card = document.querySelector('[data-live-card="' + flight.id + '"]');
      if (card) {
        card.hidden = false;
        card.dataset.status = flight.status;
        card.textContent = liveHeadline(flight) + (flight.position ? " · " + (flight.status === "flying" ? "seen " : "last seen ") +
          ago(flight.position.seen_secs + (Date.now() - live.at) / 1000) : "");
      }
      var detail = document.querySelector('[data-live-detail="' + flight.id + '"]');
      if (detail) {
        detail.hidden = false;
        detail.dataset.status = flight.status;
        detail.querySelector("[data-live-text]").textContent = liveHeadline(flight);
        detail.querySelector("[data-live-facts]").textContent = liveFacts(flight);
        detail.querySelector("[data-live-show]").hidden = !planeAt(flight) || flight.status === "landed";
      }
    });
    document.querySelectorAll("[data-live-card], [data-live-detail]").forEach(function (node) {
      if (!shown[node.dataset.liveCard || node.dataset.liveDetail]) node.hidden = true;
    });

    var up = live.flights.filter(function (f) { return f.status !== "landed" || f.trail.length > 1; });
    if (!up.length) {
      if (live.control) { map.removeControl(live.control); live.control = null; }
      return;
    }
    if (!live.control) live.control = new LiveControl().addTo(map);
    var box = live.control.getContainer();
    box.innerHTML = up.map(function (flight) {
      return '<button type="button" data-status="' + esc(flight.status) + '" data-live-go="' + esc(flight.id) + '">' +
        '<span class="live-flight__dot" aria-hidden="true"></span><span>' + esc(liveRoute(flight)) +
        "<small>" + esc(liveHeadline(flight)) + "</small></span></button>";
    }).join("");
    box.querySelectorAll("[data-live-go]").forEach(function (button) {
      button.addEventListener("click", function () {
        var id = button.getAttribute("data-live-go"), leg = legsById[id];
        if (leg && document.querySelector("[data-trip-page]")) {
          document.dispatchEvent(new CustomEvent("tripmap:leg", { detail: { id: leg.id, day: leg.day } }));
        }
        setTimeout(function () { showPlane(id); }, 700);
      });
    });
  }

  function pollLive(seconds) {
    clearTimeout(live.timer);
    if (seconds !== null && seconds !== undefined) live.timer = setTimeout(fetchLive, seconds * 1000);
  }

  function fetchLive() {
    if (document.hidden) { live.paused = true; return; }
    fetch(el.dataset.live, { credentials: "same-origin", cache: "no-store" })
      .then(function (response) { return response.json(); })
      .then(function (data) {
        if (data.flights) {
          live.flights = data.flights;
          live.at = Date.now();
          drawLive();
          showLiveStatus();
          if (live.flights.length && !live.ticker) live.ticker = setInterval(showLiveStatus, 15000);
        }
        pollLive(data.poll);
      })
      .catch(function () { pollLive(60); });
  }

  document.addEventListener("visibilitychange", function () {
    if (!document.hidden && live.paused) { live.paused = false; fetchLive(); }
  });
  document.addEventListener("click", function (event) {
    var button = event.target.closest("[data-live-show]");
    if (button) showPlane(button.getAttribute("data-live-show"));
  });
  recolour.push(drawLive);

  document.addEventListener("themechange", function () {
    STYLES = buildStyles();
    recolour.forEach(function (restyle) { restyle(); });
    railways.setOpacity(isDark() ? 0.55 : 0.7);
  });

  fetch(el.dataset.src, { credentials: "same-origin" })
    .then(function (response) { return response.json(); })
    .then(function (data) {
      draw(data);
      if (el.dataset.live) fetchLive();
    })
    .catch(function () { el.textContent = "The map couldn't be loaded. Please refresh the page."; });

  document.querySelectorAll("[data-show-day]").forEach(function (button) {
    button.addEventListener("click", function () {
      highlightDay(button.getAttribute("data-show-day"), true);
      var rect = el.getBoundingClientRect();
      if (rect.bottom < 0 || rect.top > window.innerHeight) el.scrollIntoView({ behavior: "smooth", block: "center" });
    });
  });
})();
