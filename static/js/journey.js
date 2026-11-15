/*
 * Journey planner page, in two tabs.
 *
 * Australian cities: choose a start (your location, a search or a tap on the map) and a
 * destination (search for a park, beach or address, or tap the map), then see up to three ways
 * there by train, metro, tram, ferry and bus, now (with live times) or on a date and at a time you
 * choose, plus a drive to compare. Tickboxes choose the ways to travel, the longest walk to a stop,
 * step-free only and avoiding tolls; they're remembered in this browser (never the places). The
 * city comes from the places chosen, so there's nothing to pick first. "Departures near the start"
 * lists what leaves nearby next.
 *
 * International routes: choose two stations and a day to see China Railway's trains between them,
 * with seats left; signed in, also direct flights between two airports.
 *
 * Signed in, a journey, train or flight can be added to one of the trips as private transport.
 * Anyone can add a journey or drive to their calendar (an .ics file made here in the browser) or
 * share it: the link carries the places after the #, which browsers never send to this site.
 *
 * Privacy: positions are kept only in this page's memory while it's open, never in the
 * browser's storage, and are sent to this site only in the body of a request.
 */
(function () {
  "use strict";

  var root = document.querySelector("[data-journey]");
  var dataNode = document.getElementById("journey-cities");
  if (!root || !window.L || !dataNode) return;
  var cities = JSON.parse(dataNode.textContent);
  var tripsNode = document.getElementById("journey-trips");
  var trips = tripsNode ? JSON.parse(tripsNode.textContent) : null;

  var csrfField = root.querySelector("input[name=csrfmiddlewaretoken]");  // signed-in staff only
  var csrf = csrfField ? csrfField.value : "";
  var MONTHS = ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"];
  var DAYS = ["Sun", "Mon", "Tue", "Wed", "Thu", "Fri", "Sat"];

  function css(name) { return getComputedStyle(document.documentElement).getPropertyValue(name).trim(); }
  function esc(text) {
    return String(text == null ? "" : text).replace(/[&<>"']/g, function (c) {
      return { "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c];
    });
  }
  function pad(n) { return (n < 10 ? "0" : "") + n; }
  function isoDay(date) { return date.getFullYear() + "-" + pad(date.getMonth() + 1) + "-" + pad(date.getDate()); }
  // A moment's date and time in a time zone: {day: "2026-11-02", time: "08:30"} (the browser's own as a fallback).
  function zoned(ms, tz) {
    var at = new Date(ms), parts = {};
    try {
      new Intl.DateTimeFormat("en-CA", { timeZone: tz, year: "numeric", month: "2-digit", day: "2-digit", hour: "2-digit", minute: "2-digit", hourCycle: "h23" })
        .formatToParts(at).forEach(function (part) { parts[part.type] = part.value; });
      return { day: parts.year + "-" + parts.month + "-" + parts.day, time: parts.hour + ":" + parts.minute };
    } catch (e) {
      return { day: isoDay(at), time: pad(at.getHours()) + ":" + pad(at.getMinutes()) };
    }
  }
  function dayLabel(iso) {  // "2026-11-02" -> "Mon 2 Nov"
    var parts = iso.split("-").map(Number), date = new Date(parts[0], parts[1] - 1, parts[2]);
    return DAYS[date.getDay()] + " " + date.getDate() + " " + MONTHS[date.getMonth()];
  }

  function post(url, body) {
    return fetch(url, {
      method: "POST",
      credentials: "same-origin",
      headers: csrf ? { "Content-Type": "application/json", "X-CSRFToken": csrf } : { "Content-Type": "application/json" },
      body: JSON.stringify(body)
    }).then(function (response) {
      return response.json().catch(function () { return {}; }).then(function (data) {
        if (!response.ok) throw new Error(data.error || "Something went wrong. Please try again.");
        return data;
      });
    });
  }

  function km(a, b) {
    var rad = Math.PI / 180, dlat = (b[0] - a[0]) * rad, dlon = (b[1] - a[1]) * rad;
    var h = Math.sin(dlat / 2) * Math.sin(dlat / 2) + Math.cos(a[0] * rad) * Math.cos(b[0] * rad) * Math.sin(dlon / 2) * Math.sin(dlon / 2);
    return 12742 * Math.asin(Math.sqrt(h));
  }
  function cityAt(lat, lon) {
    var found = null;
    cities.forEach(function (city) { if (!found && km([lat, lon], [city.lat, city.lon]) <= city.radius_km) found = city; });
    return found;
  }

  // A list of choices under a search box, filled as the visitor types.
  function suggest(input, list, minimum, fetchResults, render, choose) {
    var timer = null, asked = "";
    input.addEventListener("input", function () {
      clearTimeout(timer);
      var query = input.value.trim();
      if (query.length < minimum(query)) { list.hidden = true; return; }
      timer = setTimeout(function () {
        asked = query;
        fetchResults(query).then(function (results) {
          if (asked !== input.value.trim()) return;
          list.innerHTML = "";
          results.forEach(function (result) {
            var item = document.createElement("li");
            var button = document.createElement("button");
            button.type = "button";
            button.className = "place-results__item";
            button.setAttribute("role", "option");
            button.innerHTML = render(result);
            button.addEventListener("click", function () { list.hidden = true; choose(result); });
            item.appendChild(button);
            list.appendChild(item);
          });
          if (!list.children.length) list.innerHTML = '<li class="muted small place-results__none">Nothing found. Try other words.</li>';
          list.hidden = false;
        });
      }, 300);
    });
    input.addEventListener("keydown", function (event) {
      if (event.key === "Escape") list.hidden = true;
      if (event.key === "Enter") {
        event.preventDefault();
        var first = list.querySelector(".place-results__item");
        if (first && !list.hidden) first.click();
      }
    });
    document.addEventListener("click", function (event) {
      if (!list.contains(event.target) && event.target !== input) list.hidden = true;
    });
  }

  // ---------- map ----------

  var map = L.map("journey-map", { zoomControl: false, zoomSnap: 0.5 });
  L.control.zoom({ position: "topright" }).addTo(map);
  L.tileLayer("https://tile.openstreetmap.org/{z}/{x}/{y}.png", {
    maxZoom: 19,
    className: "basemap",
    attribution: '&copy; <a href="https://www.openstreetmap.org/copyright">OpenStreetMap</a> contributors'
  }).addTo(map);
  var layers = { city: L.layerGroup().addTo(map), world: L.layerGroup() };
  var worldLayers = { trains: L.layerGroup().addTo(layers.world), flights: L.layerGroup() };
  var WORLD_VIEWS = { trains: [[32, 110], 4], flights: [[5, 125], 2.5] };
  var routeLayer = L.layerGroup().addTo(layers.city);
  var markerLayer = L.layerGroup().addTo(layers.city);
  var labelLayer = L.layerGroup().addTo(layers.city);

  function pin(letter, kind) {
    return L.divIcon({ className: "", html: '<div class="journey-pin journey-pin--' + kind + '">' + letter + "</div>", iconSize: [26, 26], iconAnchor: [13, 13] });
  }

  // The cities, labelled until you zoom in: tap one to go there.
  cities.forEach(function (city) {
    L.marker([city.lat, city.lon], {
      icon: L.divIcon({ className: "", html: '<span class="journey-city-label">' + esc(city.name) + "</span>", iconSize: null }),
      title: "Go to " + city.name, keyboard: true
    }).on("click", function () { map.setView([city.lat, city.lon], 12); }).addTo(labelLayer);
  });
  function citiesView() {
    if (cities.length) map.fitBounds(L.latLngBounds(cities.map(function (c) { return [c.lat, c.lon]; })).pad(0.25));
    else map.setView([-30, 145], 4);
  }
  map.on("zoomend", function () {
    if (map.getZoom() >= 9) layers.city.removeLayer(labelLayer);
    else if (!layers.city.hasLayer(labelLayer)) labelLayer.addTo(layers.city);
  });

  // ---------- tabs ----------

  var tabs = Array.prototype.slice.call(root.querySelectorAll("[data-tab]"));
  var panels = Array.prototype.slice.call(root.querySelectorAll("[data-tab-panel]"));
  var activeTab = "city", worldMode = "trains", views = {};
  function viewKey(name) { return name === "world" ? "world-" + worldMode : name; }
  function hashFor() { return activeTab === "city" ? "" : worldMode === "flights" ? "#flights" : "#international"; }
  function showTab(name, focus) {
    if (name === activeTab) return;
    views[viewKey(activeTab)] = { centre: map.getCenter(), zoom: map.getZoom() };
    map.removeLayer(layers[activeTab]);
    map.addLayer(layers[name]);
    activeTab = name;
    tabs.forEach(function (tab) {
      var on = tab.dataset.tab === name;
      tab.setAttribute("aria-selected", String(on));
      tab.tabIndex = on ? 0 : -1;
      if (on && focus) tab.focus();
    });
    panels.forEach(function (panel) { panel.hidden = panel.dataset.tabPanel !== name; });
    restoreView();
  }
  function restoreView() {
    var saved = views[viewKey(activeTab)];
    if (saved) map.setView(saved.centre, saved.zoom);
    else if (activeTab === "world") map.setView(WORLD_VIEWS[worldMode][0], WORLD_VIEWS[worldMode][1]);
    else citiesView();
    try { history.replaceState(null, "", hashFor() || location.pathname + location.search); } catch (e) { /* not allowed here */ }
  }
  tabs.forEach(function (tab, index) {
    tab.addEventListener("click", function () { showTab(tab.dataset.tab); });
    tab.addEventListener("keydown", function (event) {
      if (event.key !== "ArrowRight" && event.key !== "ArrowLeft") return;
      var next = tabs[(index + (event.key === "ArrowRight" ? 1 : tabs.length - 1)) % tabs.length];
      showTab(next.dataset.tab, true);
    });
  });

  // International routes: trains in China, or (signed in) flights.
  var modeButtons = Array.prototype.slice.call(root.querySelectorAll("[data-world-mode]"));
  function showMode(mode) {
    if (mode === worldMode || !worldLayers[mode]) return;
    views[viewKey("world")] = { centre: map.getCenter(), zoom: map.getZoom() };
    layers.world.removeLayer(worldLayers[worldMode]);
    layers.world.addLayer(worldLayers[mode]);
    worldMode = mode;
    modeButtons.forEach(function (button) { button.setAttribute("aria-pressed", String(button.dataset.worldMode === mode)); });
    root.querySelectorAll("[data-world-panel]").forEach(function (panel) { panel.hidden = panel.dataset.worldPanel !== mode; });
    restoreView();
  }
  modeButtons.forEach(function (button) { button.addEventListener("click", function () { showMode(button.dataset.worldMode); }); });

  // ---------- adding to a trip (signed in) ----------

  var chosenTrip = trips && trips.length ? String(trips[0].id) : "";
  function tripOf(id) { return (trips || []).filter(function (t) { return String(t.id) === String(id); })[0]; }
  function tripPicker() {
    return '<select class="input" data-add-trip aria-label="Trip">' + trips.map(function (trip) {
      return '<option value="' + trip.id + '"' + (String(trip.id) === chosenTrip ? " selected" : "") + ">" + esc(trip.title) + " (" + esc(trip.dates) + ")</option>";
    }).join("") + "</select>";
  }
  // The trip picker and a line for what happened, in box: {select, status, row}, or null when
  // there's nothing to add to. day ("2026-11-02") is checked against the trip's dates.
  function tripBox(box, day, hint) {
    if (!trips || !root.dataset.addUrl) return null;
    if (!trips.length) { box.innerHTML = '<p class="muted small">Make a trip first to add journeys to it.</p>'; return null; }
    box.innerHTML = '<h2 class="journey__subhead">Add to a trip</h2><div class="journey-field__row">' + tripPicker() + "</div>" +
      '<p class="hint" data-add-status aria-live="polite"></p>';
    var picked = { select: box.querySelector("[data-add-trip]"), status: box.querySelector("[data-add-status]"), row: box.querySelector(".journey-field__row") };
    function check() {
      chosenTrip = picked.select.value;
      var trip = tripOf(picked.select.value);
      picked.status.textContent = trip && day && (day < trip.start || day > trip.end) ? "This is on " + dayLabel(day) + ", outside " + trip.title + "'s dates. " + hint : "";
    }
    picked.select.addEventListener("change", check);
    check();
    return picked;
  }
  function sendToTrip(picked, button, body, what) {
    button.disabled = true;
    picked.status.textContent = "Adding " + what + "…";
    post(root.dataset.addUrl, Object.assign({ trip: picked.select.value }, body))
      .then(function (data) { picked.status.innerHTML = esc(data.message) + ' <a href="' + esc(data.url) + '">See the trip\'s transport</a>'; })
      .catch(function (error) { picked.status.textContent = error.message; button.disabled = false; });
  }

  // ---------- Australian cities ----------

  var form = root.querySelector("[data-journey-form]");
  if (form) cityPlanner();
  else citiesView();

  function cityPlanner() {
    var statusLine = root.querySelector("[data-status]");
    var panel = root.querySelector("[data-results-panel]");
    var inputs = { from: root.querySelector("[data-place=from]"), to: root.querySelector("[data-place=to]") };
    var lists = { from: root.querySelector("[data-results=from]"), to: root.querySelector("[data-results=to]") };
    var whenDate = root.querySelector("[data-when-date]"), whenTime = root.querySelector("[data-when-time]");
    var whenNow = root.querySelector("[data-when-now]"), whenHint = root.querySelector("[data-when-hint]");
    var modeBoxes = Array.prototype.slice.call(root.querySelectorAll("[data-mode]"));
    var walkSelect = root.querySelector("[data-walk]"), stepFree = root.querySelector("[data-step-free]");
    var avoidTolls = root.querySelector("[data-avoid-tolls]"), tollsOption = root.querySelector("[data-tolls-option]");
    var state = { city: null, from: null, to: null, options: [], zone: "" };
    function say(text) { statusLine.textContent = text || ""; }

    citiesView();

    function drawMarkers() {
      markerLayer.clearLayers();
      if (state.from) L.marker([state.from.lat, state.from.lon], { icon: pin("A", "from"), title: "Start" }).addTo(markerLayer);
      if (state.to) L.marker([state.to.lat, state.to.lon], { icon: pin("B", "to"), title: "Destination" }).addTo(markerLayer);
    }

    // ----- choosing places -----

    function clearResults() { routeLayer.clearLayers(); panel.innerHTML = ""; state.options = []; }
    function clearPlace(which) { state[which] = null; inputs[which].value = ""; drawMarkers(); }

    function setPlace(which, place) {
      var city = cityAt(place.lat, place.lon);
      if (!city) {
        say("That's outside Brisbane, Melbourne and Sydney, so there are no routes to show.");
        return;
      }
      var other = which === "from" ? "to" : "from";
      var moved = state[other] && cityAt(state[other].lat, state[other].lon) !== city;
      if (moved) { clearPlace(other); clearResults(); }
      state.city = city.slug;
      if (leavingNow) fillNow();  // now, in this city's time
      state[which] = place;
      inputs[which].value = place.label;
      lists[which].hidden = true;
      drawMarkers();
      if (state.from && state.to) {
        plan();
        return;
      }
      if (map.getZoom() < 11) map.setView([place.lat, place.lon], 14);
      else map.panTo([place.lat, place.lon]);
      var next = which === "from" ? "Now choose where you're going." : "Now choose where you're starting from.";
      say(moved ? "That's in " + city.name + ", so the " + (other === "from" ? "start" : "destination") + " was cleared. " + next : next);
    }

    // Which city to search in: the other place's, else the map's when it's zoomed into one, else all three.
    function searchCity(which) {
      var other = state[which === "from" ? "to" : "from"];
      var city = other ? cityAt(other.lat, other.lon) : null;
      if (!city && map.getZoom() >= 9) city = cityAt(map.getCenter().lat, map.getCenter().lng);
      return city ? city.slug : "";
    }

    // Place search (Photon): only the words typed and a city (or all three) are sent.
    Object.keys(inputs).forEach(function (which) {
      inputs[which].addEventListener("input", function () { state[which] = null; drawMarkers(); });
      suggest(inputs[which], lists[which], function () { return 3; }, function (query) {
        return post(root.dataset.placesUrl, { q: query, city: searchCity(which) })
          .then(function (data) { return data.results || []; })
          .catch(function (error) { say(error.message); return []; });
      }, function (place) {
        return "<strong>" + esc(place.name) + "</strong>" + (place.detail ? '<span class="muted small">' + esc(place.detail) + "</span>" : "");
      }, function (place) {
        setPlace(which, { lat: place.lat, lon: place.lon, label: place.name });
      });
    });

    // Your location: asked for only when the button is pressed, and kept only in this page.
    root.querySelector("[data-locate]").addEventListener("click", function () {
      if (!navigator.geolocation) { say("This browser can't share your location. Search for a place or tap the map instead."); return; }
      say("Finding your location…");
      navigator.geolocation.getCurrentPosition(function (position) {
        var place = { lat: position.coords.latitude, lon: position.coords.longitude, label: "My location" };
        if (!cityAt(place.lat, place.lon)) {
          say("You're outside Brisbane, Melbourne and Sydney. Search for a starting place there, or tap the map.");
          return;
        }
        setPlace("from", place);
        if (!state.to) map.setView([place.lat, place.lon], 15);
      }, function (error) {
        say(error.code === 1 ? "Location sharing is off. Search for a place or tap the map instead." : "Couldn't find your location. Search for a place or tap the map instead.");
      }, { enableHighAccuracy: true, timeout: 12000, maximumAge: 30000 });
    });

    // Tap the map: start or end here.
    map.on("click", function (event) {
      if (activeTab !== "city") return;
      var lat = event.latlng.lat, lon = event.latlng.lng;
      if (!cityAt(lat, lon)) {
        say("Zoom into Brisbane, Melbourne or Sydney (tap a city's name) to start or end a journey there.");
        return;
      }
      var box = document.createElement("div");
      box.className = "journey-popup";
      [["from", "Start here"], ["to", "Go here"]].forEach(function (choice) {
        var button = document.createElement("button");
        button.type = "button";
        button.className = "btn" + (choice[0] === "to" ? " btn--primary" : "");
        button.textContent = choice[1];
        button.addEventListener("click", function () {
          map.closePopup();
          setPlace(choice[0], { lat: lat, lon: lon, label: "Point on the map" });
        });
        box.appendChild(button);
      });
      L.popup({ closeButton: false }).setLatLng(event.latlng).setContent(box).openOn(map);
    });

    root.querySelector("[data-swap]").addEventListener("click", function () {
      var from = state.from, to = state.to;
      state.from = to;
      state.to = from;
      inputs.from.value = to ? to.label : "";
      inputs.to.value = from ? from.label : "";
      drawMarkers();
      if (state.from && state.to) plan();
    });

    // ----- how to travel (remembered in this browser) and when -----

    var STORE = "journey-choices";
    function choices() {
      return {
        modes: modeBoxes.filter(function (box) { return box.checked; }).map(function (box) { return box.value; }),
        walk: parseInt(walkSelect.value, 10), step_free: stepFree.checked, avoid_tolls: avoidTolls.checked
      };
    }
    function showTolls() { tollsOption.hidden = !modeBoxes.some(function (box) { return box.value === "drive" && box.checked; }); }
    try {
      var kept = JSON.parse(localStorage.getItem(STORE) || "null");
      if (kept && Array.isArray(kept.modes)) {
        modeBoxes.forEach(function (box) { box.checked = kept.modes.indexOf(box.value) >= 0; });
        if (kept.walk) walkSelect.value = String(kept.walk);
        stepFree.checked = !!kept.step_free;
        avoidTolls.checked = !!kept.avoid_tolls;
      }
    } catch (e) { /* storage blocked: start with everything ticked */ }
    showTolls();

    var replan = null;
    function replanSoon(wait) {
      clearTimeout(replan);
      if (state.from && state.to) replan = setTimeout(plan, wait);
    }
    [walkSelect, stepFree, avoidTolls].concat(modeBoxes).forEach(function (control) {
      control.addEventListener("change", function () {
        showTolls();
        try { localStorage.setItem(STORE, JSON.stringify(choices())); } catch (e) { /* not kept: fine */ }
        replanSoon(400);
      });
    });

    // Leave now (live times) until a date or time is changed; Now goes back. Times are the city's own.
    var leavingNow = true;
    function cityZone() { return (cities.filter(function (c) { return c.slug === state.city; })[0] || cities[0]).tz; }
    function fillNow() {
      var at = zoned(Date.now(), cityZone());
      whenDate.value = at.day;
      whenTime.value = at.time;
      whenDate.min = at.day;
    }
    function describeWhen() {
      whenNow.setAttribute("aria-pressed", String(leavingNow));
      if (leavingNow) { whenHint.textContent = "Leaving now, with live times."; return; }
      var hour = parseInt(whenTime.value.slice(0, 2), 10);
      var time = (hour % 12 || 12) + ":" + whenTime.value.slice(3, 5) + (hour < 12 ? " am" : " pm");
      whenHint.textContent = whenDate.value && whenTime.value ? "Leaving " + dayLabel(whenDate.value) + " at " + time + ", " + (state.city ? "local time." : "in the city's own time.") : "Choose a date and time.";
    }
    function leaving() { return !leavingNow && whenDate.value && whenTime.value ? whenDate.value + "T" + whenTime.value : ""; }
    fillNow();
    describeWhen();
    setInterval(function () { if (leavingNow) fillNow(); }, 30000);
    [whenDate, whenTime].forEach(function (input) {
      input.addEventListener("change", function () {
        leavingNow = false;
        describeWhen();
        if (whenDate.value && whenTime.value) replanSoon(700);  // typing a date changes it a part at a time
      });
    });
    whenNow.addEventListener("click", function () {
      leavingNow = true;
      fillNow();
      describeWhen();
      replanSoon(0);
    });

    // ----- journeys -----

    function chip(leg) {
      var style = leg.colour ? ' style="--line: ' + esc(leg.colour) + (leg.text ? "; --line-text: " + esc(leg.text) : "") + ';"' : "";
      return '<span class="line-chip"' + style + ">" + esc(leg.line || leg.mode || "Ride") + "</span>";
    }
    function notesHtml(notes) {
      return (notes || []).map(function (text) { return '<p class="note small">' + esc(text) + "</p>"; }).join("");
    }
    function howFar(metres) { return metres >= 1000 ? (metres / 1000).toFixed(1) + " km" : Math.round(metres) + " m"; }
    function liveNote(live) {
      return live && live.text ? '<p class="journey__live small' + (live.live ? " is-live" : "") + '">' + (live.live ? '<span class="live-dot" aria-hidden="true"></span>' : "") + esc(live.text) + "</p>" : "";
    }

    function plan() {
      if (!state.from || !state.to) { say("Choose where you're starting from and where you're going."); return; }
      say("Finding the best ways there…");
      clearResults();
      post(root.dataset.planUrl, Object.assign({ from: { lat: state.from.lat, lon: state.from.lon }, to: { lat: state.to.lat, lon: state.to.lon }, when: leaving() }, choices()))
        .then(function (data) {
          state.options = data.options;
          state.zone = data.zone;
          say("");
          renderOptions(data);
          selectOption(0);
        })
        .catch(function (error) { say(error.message); });
    }
    form.addEventListener("submit", function (event) { event.preventDefault(); plan(); });

    function renderOptions(data) {
      var html = (leaving() ? '<h2 class="journey__subhead">Leaving ' + esc(data.day) + "</h2>" : "") +
        notesHtml(data.notes) + (data.options.some(function (o) { return o.kind === "transit"; }) ? liveNote(data.live) : "") + '<ol class="journey-options">';
      data.options.forEach(function (option, index) {
        var rides = option.legs.filter(function (leg) { return leg.kind === "ride"; });
        var lines, meta;
        if (option.kind === "drive") {
          lines = '<span class="line-chip line-chip--drive">Drive</span>';
          meta = howFar(option.metres) + " · " + (option.tolls.length ? "uses tolls: " + esc(option.tolls.join(", ")) : "no tolls") + " · without traffic";
        } else if (option.kind === "walk") {
          lines = '<span class="line-chip line-chip--walk">Walk</span>';
          meta = "Walk the whole way";
        } else {
          lines = (option.drive_minutes ? ['<span class="line-chip line-chip--drive">Drive</span>'] : []).concat(rides.map(chip))
            .join('<span class="journey-option__arrow" aria-hidden="true">›</span>');
          meta = (option.drive_minutes ? "Drive " + option.drive_minutes + " min, park · " : "") +
            (option.changes ? option.changes + (option.changes === 1 ? " change" : " changes") : "No changes") + " · " +
            option.walk_minutes + " min walking" + (option.live ? ' · <span class="live-text">Live</span>' : "");
        }
        html += '<li><button type="button" class="journey-option" data-option="' + index + '" aria-pressed="false">' +
          '<span class="journey-option__times"><strong>' + esc(option.leave) + " → " + esc(option.arrive) + "</strong>" +
          '<span class="muted">' + option.minutes + " min</span></span>" +
          '<span class="journey-option__lines">' + lines + "</span>" +
          '<span class="journey-option__meta muted small">' + meta + "</span>" +
          "</button></li>";
      });
      html += "</ol>" + '<div class="journey-steps" data-steps></div>' + '<div class="journey-actions" data-actions></div>' +
        '<div class="journey-add" data-add-box></div>' +
        '<p class="muted small">Times in ' + esc(data.zone) + ". Check the operator's app or signs before you travel" +
        (data.options.some(function (o) { return o.kind === "drive"; }) ? "; drive times are for clear roads and don't include parking" : "") + ".</p>";
      panel.innerHTML = html;
      panel.querySelectorAll("[data-option]").forEach(function (button) {
        button.addEventListener("click", function () { selectOption(parseInt(button.dataset.option, 10)); });
      });
    }

    function stepText(leg) {
      if (leg.kind === "drive") {
        return '<li class="journey-step journey-step--drive"><span class="journey-step__icon" aria-hidden="true"></span><div>' +
          '<p class="journey-step__head"><span class="line-chip line-chip--drive">Drive</span> ' + leg.minutes + " min · " + howFar(leg.metres) + "</p>" +
          '<ol class="drive-steps small">' + leg.steps.map(function (step) {
            return "<li><span>" + esc(step.text) + "</span><span>" + howFar(step.metres) + "</span></li>";
          }).join("") + "<li><span>" + (leg.park ? "Arrive at " + esc(leg.to) : "Arrive at your destination") + "</span><span></span></li></ol>" +
          (leg.tolls.length ? '<p class="muted small">Toll roads: ' + esc(leg.tolls.join(", ")) + ". Tick Avoid tolls to go around them.</p>" : "") +
          "</div></li>";
      }
      if (leg.kind === "park") {
        return '<li class="journey-step journey-step--walk"><span class="journey-step__icon" aria-hidden="true"></span><div><strong>Park</strong> and walk to the platform at ' + esc(leg.to) + ' <span class="muted">(allow ' + leg.minutes + " min)</span></div></li>";
      }
      if (leg.kind === "walk" && leg.change && leg.from === leg.to) {
        return '<li class="journey-step journey-step--walk"><span class="journey-step__icon" aria-hidden="true"></span><div><strong>Change platforms</strong> at ' + esc(leg.to) + ' <span class="muted">(' + leg.minutes + " min)</span></div></li>";
      }
      if (leg.kind === "walk") {
        var to = leg.to === "Destination" ? "your destination" : esc(leg.to);
        var distance = leg.metres >= 1000 ? (leg.metres / 1000).toFixed(1) + " km" : leg.metres + " m";
        return '<li class="journey-step journey-step--walk"><span class="journey-step__icon" aria-hidden="true"><svg viewBox="0 0 20 20" fill="none" stroke="currentColor" stroke-width="1.5" stroke-linecap="round" stroke-linejoin="round"><circle cx="10.5" cy="3.5" r="1.5"/><path d="M10 6.5l-1.5 5 2.5 2 1 4M8.5 11.5l-2 5.5M9.5 7.5l3 2 2-1"/></svg></span><div><strong>Walk ' + leg.minutes + " min</strong> <span class=\"muted\">(" + distance + ")</span> to " + to + "</div></li>";
      }
      return '<li class="journey-step"><span class="journey-step__icon" aria-hidden="true"></span><div>' +
        '<p class="journey-step__head">' + chip(leg) + (leg.headsign ? " towards " + esc(leg.headsign) : "") + (leg.live ? ' <span class="live-text small">Live</span>' : "") + "</p>" +
        "<p><strong>" + esc(leg.depart) + "</strong> from " + esc(leg.from) + (leg.platform ? ", platform " + esc(leg.platform) : "") + "</p>" +
        '<p class="muted small">' + leg.stops + (leg.stops === 1 ? " stop" : " stops") + (leg.operator ? " · " + esc(leg.operator) : "") + "</p>" +
        "<p><strong>" + esc(leg.arrive) + "</strong> get off at " + esc(leg.to) + "</p>" +
        "</div></li>";
    }

    function selectOption(index) {
      var option = state.options[index];
      if (!option) return;
      panel.querySelectorAll("[data-option]").forEach(function (button) {
        button.setAttribute("aria-pressed", String(button.dataset.option === String(index)));
      });
      panel.querySelector("[data-steps]").innerHTML = '<h2 class="visually-hidden">Steps</h2><ol class="journey-steps__list">' +
        option.legs.map(stepText).join("") + "</ol>";
      showActions(option);
      // A drive goes in as one car leg (to the destination, or to the station it parks at); rides one leg each.
      var rides = option.legs.filter(function (leg) { return leg.kind === "ride" || leg.kind === "drive"; }).map(function (leg) {
        return leg.kind !== "drive" ? leg : Object.assign({}, leg, {
          from_place: { name: state.from.label, lat: state.from.lat, lon: state.from.lon },
          to_place: leg.to_place || { name: state.to.label, lat: state.to.lat, lon: state.to.lon }
        });
      });
      var box = panel.querySelector("[data-add-box]");
      box.innerHTML = "";
      var picked = rides.length ? tripBox(box, rides[0].depart_at.slice(0, 10), "Change the date under Leave to a day of the trip.") : null;
      if (picked) {
        var add = document.createElement("button");
        add.type = "button";
        add.className = "btn btn--primary";
        add.textContent = option.kind === "drive" ? "Add this drive" : option.drive_minutes ? "Add the drive and " + (rides.length - 1 === 1 ? "ride" : (rides.length - 1) + " rides") :
          rides.length === 1 ? "Add this ride" : "Add these " + rides.length + " rides";
        add.addEventListener("click", function () {
          sendToTrip(picked, add, { legs: rides }, option.kind === "drive" ? "the drive" : rides.length === 1 ? "the ride" : "the rides");
        });
        picked.row.appendChild(add);
      }
      routeLayer.clearLayers();
      var points = [];
      option.legs.forEach(function (leg) {
        if (!leg.path || leg.path.length < 2) return;
        if (leg.kind === "walk") {
          L.polyline(leg.path, { color: css("--text") || "#333", weight: 4, opacity: 0.75, dashArray: "1 8", lineCap: "round" }).addTo(routeLayer);
        } else if (leg.kind === "drive") {
          L.polyline(leg.path, { color: css("--mode-road") || "#707070", weight: 5, opacity: 0.9, lineJoin: "round" }).addTo(routeLayer);
        } else {
          L.polyline(leg.path, { color: leg.colour || css("--mode-metro"), weight: 5, opacity: 0.9, lineJoin: "round" })
            .bindTooltip(esc((leg.line || "") + (leg.headsign ? " towards " + leg.headsign : "")), { sticky: true }).addTo(routeLayer);
          (leg.points || []).forEach(function (point) {
            L.circleMarker(point, { radius: 3, color: leg.colour || css("--mode-metro"), weight: 2, fillColor: "#fff", fillOpacity: 1 }).addTo(routeLayer);
          });
        }
        points = points.concat(leg.path);
      });
      if (points.length) map.fitBounds(L.latLngBounds(points), { padding: [30, 30], maxZoom: 16 });
    }

    // ----- add to calendar and share -----

    function legLine(leg) {
      if (leg.kind === "walk") return leg.change && leg.from === leg.to ? "Change platforms at " + leg.to + " (" + leg.minutes + " min)" :
        "Walk " + leg.minutes + " min to " + (leg.to === "Destination" ? state.to.label : leg.to);
      if (leg.kind === "park") return "Park and walk to the platform (allow " + leg.minutes + " min)";
      if (leg.kind === "drive") return "Drive " + leg.minutes + " min (" + howFar(leg.metres) + ") to " + (leg.park ? leg.to : state.to.label) +
        (leg.tolls.length ? ", toll roads: " + leg.tolls.join(", ") : "");
      return leg.depart + " " + (leg.line || leg.mode) + (leg.headsign ? " towards " + leg.headsign : "") + " from " + leg.from +
        (leg.platform ? ", platform " + leg.platform : "") + "; get off at " + leg.to + " at " + leg.arrive;
    }
    function optionTitle(option) {
      return (option.kind === "drive" ? "Drive" : "Public transport") + " to " + state.to.label;
    }
    function utcStamp(date) { return date.toISOString().replace(/[-:]/g, "").replace(/\.\d{3}/, ""); }
    function icsText(text) { return String(text).replace(/\\/g, "\\\\").replace(/;/g, "\\;").replace(/,/g, "\\,").replace(/\r?\n/g, "\\n"); }
    function fold(line) {  // calendar lines are at most 75 characters; longer ones continue after a space
      var out = [];
      while (line.length > 73) { out.push(line.slice(0, 73)); line = " " + line.slice(73); }
      out.push(line);
      return out.join("\r\n");
    }
    function calendarFile(option) {
      var steps = option.legs.map(legLine).concat(["Arrive " + option.arrive, "", "Planned with the journey planner at trips.aarondaws.com.au/journey/. Check the operator's app or signs before you travel."]);
      var lines = [
        "BEGIN:VCALENDAR", "VERSION:2.0", "PRODID:-//Aaron Daws Trips//Journey planner//EN", "CALSCALE:GREGORIAN", "METHOD:PUBLISH",
        "BEGIN:VEVENT", "UID:" + Date.now().toString(36) + Math.random().toString(36).slice(2) + "@trips.aarondaws.com.au",
        "DTSTAMP:" + utcStamp(new Date()), "DTSTART:" + utcStamp(new Date(option.leave_at)), "DTEND:" + utcStamp(new Date(option.arrive_at)),
        "SUMMARY:" + icsText(optionTitle(option)), "LOCATION:" + icsText(state.from.label), "DESCRIPTION:" + icsText(steps.join("\n")),
        "BEGIN:VALARM", "TRIGGER:-PT10M", "ACTION:DISPLAY", "DESCRIPTION:" + icsText("Time to leave for " + state.to.label), "END:VALARM",
        "END:VEVENT", "END:VCALENDAR"
      ];
      return lines.map(fold).join("\r\n") + "\r\n";
    }
    function addToCalendar(option) {
      var url = URL.createObjectURL(new Blob([calendarFile(option)], { type: "text/calendar;charset=utf-8" }));
      var link = document.createElement("a");
      link.href = url;
      link.download = "journey-" + option.leave_at.slice(0, 10) + ".ics";
      document.body.appendChild(link);
      link.click();
      link.remove();
      setTimeout(function () { URL.revokeObjectURL(url); }, 5000);
    }

    // The link that reopens this journey: places, time and choices after the # (base64 JSON).
    function encode(data) {
      return btoa(unescape(encodeURIComponent(JSON.stringify(data)))).replace(/\+/g, "-").replace(/\//g, "_").replace(/=+$/, "");
    }
    function decode(text) {
      return JSON.parse(decodeURIComponent(escape(atob(text.replace(/-/g, "+").replace(/_/g, "/")))));
    }
    function spot(place) { return { n: place.label, a: Math.round(place.lat * 1e5) / 1e5, o: Math.round(place.lon * 1e5) / 1e5 }; }
    function shareLink(from, to) {
      var choice = choices();
      return location.origin + location.pathname + "#go=" + encode({
        f: spot(from), t: spot(to), w: leaving(), m: choice.modes, k: choice.walk, s: choice.step_free, x: choice.avoid_tolls
      });
    }
    function shareText(option, from, to) {
      var rides = option.legs.filter(function (leg) { return leg.kind === "ride"; }).map(function (leg) { return leg.line || leg.mode; });
      return from.label + " → " + to.label + ", " + dayLabel(option.leave_at.slice(0, 10)) + ": leave " + option.leave +
        (option.kind === "drive" ? ", drive " + option.minutes + " min" : (option.drive_minutes ? ", drive and park, then " : ", ") + rides.join(" then ")) +
        ", arrive " + option.arrive + ".";
    }
    // Where "My location" is shared from, if you'd rather not send your position: the journey's first
    // stop, or for a drive the spot rounded to about a kilometre.
    function standIn(option, which) {
      var place = state[which];
      if (place.label !== "My location") return null;
      var rides = option.legs.filter(function (leg) { return leg.kind === "ride"; });
      var ride = which === "from" ? rides[0] : rides[rides.length - 1];
      if (ride && option.kind !== "drive") {
        var stop = which === "from" ? ride.from_place : ride.to_place;
        return { label: stop.name, lat: stop.lat, lon: stop.lon, words: (which === "from" ? "from " : "to ") + stop.name };
      }
      return { label: "Near my location", lat: Math.round(place.lat * 100) / 100, lon: Math.round(place.lon * 100) / 100,
               words: (which === "from" ? "from" : "to") + " about here (within 1 km)" };
    }
    function share(option, from, to, status) {
      var url = shareLink(from, to), text = shareText(option, from, to);
      if (navigator.share) {
        navigator.share({ title: optionTitle(option), text: text, url: url }).catch(function () { /* closed: fine */ });
        return;
      }
      var done = function () { status.textContent = "Link copied, with a summary. Paste it anywhere."; };
      if (navigator.clipboard && navigator.clipboard.writeText) {
        navigator.clipboard.writeText(text + "\n" + url).then(done, function () { showLink(status, url); });
      } else {
        showLink(status, url);
      }
    }
    function showLink(status, url) {
      status.innerHTML = 'Copy this link: <input class="input input--inline journey-share-link" readonly value="' + esc(url) + '">';
      status.querySelector("input").select();
    }
    function showActions(option) {
      var box = panel.querySelector("[data-actions]");
      if (!box) return;
      box.innerHTML = "";
      if (option.kind !== "transit" && option.kind !== "drive") return;
      box.innerHTML = '<div class="journey-actions__row"><button type="button" class="btn" data-calendar>Add to calendar</button>' +
        '<button type="button" class="btn" data-share>Share</button></div><p class="hint" data-share-status aria-live="polite"></p>';
      var status = box.querySelector("[data-share-status]");
      box.querySelector("[data-calendar]").addEventListener("click", function () { addToCalendar(option); });
      box.querySelector("[data-share]").addEventListener("click", function () {
        var subs = { from: standIn(option, "from"), to: standIn(option, "to") };
        if (!subs.from && !subs.to) { share(option, state.from, state.to, status); return; }
        // Ask each time whether your exact position goes in the link.
        var which = subs.from ? "from" : "to";
        status.innerHTML = "Your exact location is the " + (which === "from" ? "start" : "destination") + ". Share it, or " + esc(subs[which].words) + "? " +
          '<span class="journey-actions__row"><button type="button" class="btn btn--small" data-exact>Exact location</button>' +
          '<button type="button" class="btn btn--small btn--primary" data-near>' + esc(subs[which].words.charAt(0).toUpperCase() + subs[which].words.slice(1)) + "</button></span>";
        status.querySelector("[data-exact]").addEventListener("click", function () { status.textContent = ""; share(option, state.from, state.to, status); });
        status.querySelector("[data-near]").addEventListener("click", function () {
          status.textContent = "";
          share(option, subs.from || state.from, subs.to || state.to, status);
        });
      });
    }

    // Opened from a shared link: put its journey in, plan it, and take the places out of the address bar.
    function openShared() {
      if (location.hash.indexOf("#go=") !== 0) return;
      var data;
      try { data = decode(location.hash.slice(4)); } catch (e) { data = null; }
      try { history.replaceState(null, "", location.pathname + location.search); } catch (e) { /* not allowed here */ }
      function place(item) {
        var lat = item && Number(item.a), lon = item && Number(item.o);
        return item && isFinite(lat) && isFinite(lon) && cityAt(lat, lon) ? { lat: lat, lon: lon, label: String(item.n || "Shared place").slice(0, 100) } : null;
      }
      var from = data && place(data.f), to = data && place(data.t);
      if (!from || !to || cityAt(from.lat, from.lon) !== cityAt(to.lat, to.lon)) { say("That shared journey link isn't complete."); return; }
      if (Array.isArray(data.m)) modeBoxes.forEach(function (box) { box.checked = data.m.indexOf(box.value) >= 0; });
      if (data.k) walkSelect.value = String(data.k);
      stepFree.checked = !!data.s;
      avoidTolls.checked = !!data.x;
      showTolls();
      state.city = cityAt(from.lat, from.lon).slug;
      state.from = from;
      state.to = to;
      inputs.from.value = from.label;
      inputs.to.value = to.label;
      var later = typeof data.w === "string" && /^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}$/.test(data.w) && data.w.slice(0, 10) >= zoned(Date.now(), cityZone()).day;
      if (later) {
        leavingNow = false;
        whenDate.value = data.w.slice(0, 10);
        whenTime.value = data.w.slice(11, 16);
      } else {
        fillNow();
      }
      describeWhen();
      drawMarkers();
      plan();
    }
    openShared();

    // ----- departures near the start -----

    root.querySelector("[data-nearby]").addEventListener("click", function () {
      if (!state.from) { say("Choose a start first: tap My location, search for a place, or tap the map."); return; }
      say("Looking for departures nearby…");
      clearResults();
      post(root.dataset.nearbyUrl, Object.assign({ at: { lat: state.from.lat, lon: state.from.lon }, when: leaving() }, choices()))
        .then(function (data) {
          say(data.message || "");
          var html = notesHtml(data.notes) + liveNote(data.live) +
            '<h2 class="journey__subhead">Leaving near ' + esc(state.from.label) + "</h2>";
          if (!data.groups.length) {
            panel.innerHTML = html + '<p class="muted">Nothing leaves nearby in the next hour.</p>';
            return;
          }
          html += '<ul class="departures">';
          data.groups.forEach(function (group) {
            html += '<li class="departure">' +
              '<div class="departure__line">' + chip(group) + ' <span class="departure__to">' + esc(group.headsign || "") + "</span></div>" +
              '<div class="departure__stop muted small">' + esc(group.stop) + (group.platform ? ", platform " + esc(group.platform) : "") + " · " + group.walk_minutes + " min walk</div>" +
              '<div class="departure__times">' + group.times.map(function (t) {
                return '<span class="departure__time' + (t.live ? " is-live" : "") + '" title="' + esc(t.time) + '">' + (t.minutes === 0 ? "Now" : t.minutes + " min") + "</span>";
              }).join("") + "</div></li>";
          });
          panel.innerHTML = html + "</ul>" + '<p class="muted small">Times in ' + esc(data.zone) + ". Live times are marked with a dot.</p>";
        })
        .catch(function (error) { say(error.message); });
    });
  }

  // ---------- China trains ----------

  var chinaForm = root.querySelector("[data-china-form]");
  if (chinaForm) chinaTrains();

  function chinaTrains() {
    var statusLine = root.querySelector("[data-china-status]");
    var panel = root.querySelector("[data-china-results]");
    var dateInput = root.querySelector("[data-china-date]");
    var inputs = { from: root.querySelector("[data-station=from]"), to: root.querySelector("[data-station=to]") };
    var lists = { from: root.querySelector("[data-station-results=from]"), to: root.querySelector("[data-station-results=to]") };
    var state = { from: null, to: null, result: null, kind: "all", after: "00:00", exact: false, shown: 60 };
    function say(text) { statusLine.textContent = text || ""; }

    var today = zoned(Date.now(), "Asia/Shanghai").day;  // China's date, as 12306 counts it
    dateInput.min = today;
    dateInput.value = today;

    function label(station) { return station.name + (station.local ? " " + station.local : ""); }
    function drawStations() {
      worldLayers.trains.clearLayers();
      var points = [];
      [["from", "A"], ["to", "B"]].forEach(function (end) {
        var station = state[end[0]];
        if (station && station.lat != null) {
          L.marker([station.lat, station.lon], { icon: pin(end[1], end[0]), title: label(station) }).addTo(worldLayers.trains);
          points.push([station.lat, station.lon]);
        }
      });
      if (points.length === 2) {
        L.polyline(points, { color: css("--mode-hsr") || "#c50f1f", weight: 3, opacity: 0.8, dashArray: "6 6" }).addTo(worldLayers.trains);
        if (activeTab === "world" && worldMode === "trains") map.fitBounds(L.latLngBounds(points), { padding: [40, 40], maxZoom: 10 });
      } else if (points.length && activeTab === "world" && worldMode === "trains") {
        map.setView(points[0], 9);
      }
    }

    Object.keys(inputs).forEach(function (which) {
      inputs[which].addEventListener("input", function () { state[which] = null; });
      suggest(inputs[which], lists[which], function (query) { return /[一-鿿]/.test(query) ? 1 : 2; }, function (query) {
        return post(root.dataset.stationsUrl, { q: query })
          .then(function (data) { return data.results || []; })
          .catch(function (error) { say(error.message); return []; });
      }, function (station) {
        return "<strong>" + esc(station.name) + "</strong>" + '<span class="muted small">' + esc(station.local) + (station.city && station.city !== station.name ? " · " + esc(station.city) : "") + "</span>";
      }, function (station) {
        state[which] = station;
        inputs[which].value = label(station);
        drawStations();
        if (state.from && state.to) search();
        else say(which === "from" ? "Now choose the station you're going to." : "Now choose the station you're leaving from.");
      });
    });

    root.querySelector("[data-china-swap]").addEventListener("click", function () {
      var from = state.from;
      state.from = state.to;
      state.to = from;
      inputs.from.value = state.from ? label(state.from) : "";
      inputs.to.value = state.to ? label(state.to) : "";
      drawStations();
      if (state.from && state.to) search();
    });
    dateInput.addEventListener("change", function () { if (state.from && state.to && dateInput.value) search(); });
    chinaForm.addEventListener("submit", function (event) { event.preventDefault(); search(); });

    function search() {
      if (!state.from || !state.to) { say("Choose both stations from the lists."); return; }
      if (!dateInput.value) { say("Choose the day you're travelling."); return; }
      say("Asking China Railway for trains…");
      panel.innerHTML = "";
      post(root.dataset.trainsUrl, { from: state.from.code, to: state.to.code, date: dateInput.value })
        .then(function (data) {
          say("");
          state.result = data;
          state.shown = 60;
          render();
        })
        .catch(function (error) { say(error.message); });
    }

    function seatText(seat) {
      var cls = seat.left === "Sold out" ? " is-out" : seat.left === "Available" || /left$/.test(seat.left) ? " is-in" : "";
      return '<span class="seat' + cls + '">' + esc(seat.class) + ": " + esc(seat.left) + "</span>";
    }

    function matches(train) {
      if (state.kind === "fast" && !train.high_speed) return false;
      if (state.kind === "other" && train.high_speed) return false;
      if (state.exact && (train.from_code !== state.result.from.code || train.to_code !== state.result.to.code)) return false;
      return train.depart >= state.after;
    }

    function render() {
      var data = state.result;
      var others = data.trains.some(function (t) { return t.from_code !== data.from.code || t.to_code !== data.to.code; });
      var html = (data.note ? '<p class="note small">' + esc(data.note) + "</p>" : "") +
        '<h2 class="journey__subhead">' + esc(data.from.name) + " → " + esc(data.to.name) + " · " + esc(data.day_label) + "</h2>" +
        '<p class="muted small">' + data.trains.length + (data.trains.length === 1 ? " train" : " trains") +
        (data.projected ? " on " + esc(data.shown_label) : "") +
        ' · Book on <a href="' + esc(data.book) + '" target="_blank" rel="noopener">Trip.com</a> or <a href="https://www.12306.cn/en/index.html" target="_blank" rel="noopener">12306</a></p>';
      if (!data.trains.length) {
        panel.innerHTML = html + '<p class="muted">No trains between these stations that day. Try the cities\' other stations.</p>';
        return;
      }
      var hours = [];
      for (var h = 0; h < 24; h += 2) hours.push(pad(h) + ":00");
      html += '<div class="train-filters">' +
        '<div class="segmented" role="group" aria-label="Kind of train">' +
        [["all", "All"], ["fast", "High-speed"], ["other", "Other trains"]].map(function (kind) {
          return '<button type="button" class="segmented__item" data-kind="' + kind[0] + '" aria-pressed="' + (state.kind === kind[0]) + '">' + kind[1] + "</button>";
        }).join("") + "</div>" +
        '<label class="train-filters__after small">Leaving after <select class="input" data-after>' + hours.map(function (hour) {
          return '<option value="' + hour + '"' + (hour === state.after ? " selected" : "") + ">" + hour + "</option>";
        }).join("") + "</select></label>" +
        (others ? '<label class="train-filters__exact small"><input type="checkbox" data-exact' + (state.exact ? " checked" : "") + "> Only " + esc(data.from.name) + " to " + esc(data.to.name) + "</label>" : "") +
        "</div>";
      html += '<div class="journey-add" data-add-box></div>';
      var list = data.trains.filter(matches);
      html += '<ol class="trains">' + list.slice(0, state.shown).map(function (train) {
        var index = data.trains.indexOf(train);
        return '<li class="train">' +
          '<div class="train__head"><span class="train-chip' + (train.high_speed ? " train-chip--hsr" : "") + '">' + esc(train.number) + "</span>" +
          '<span class="muted small">' + esc(train.kind) + " · " + esc(train.start) + " → " + esc(train.end) + "</span></div>" +
          '<p class="train__times"><strong>' + esc(train.depart) + "</strong> → <strong>" + esc(train.arrive) + "</strong>" +
          (train.days ? '<sup title="Arrives ' + (train.days === 1 ? "the next day" : train.days + " days later") + '">+' + train.days + "</sup>" : "") +
          ' <span class="muted">' + esc(train.duration) + "</span></p>" +
          '<p class="small">' + esc(train.from) + " → " + esc(train.to) + "</p>" +
          (train.seats.length ? '<p class="train__seats small">' + train.seats.map(seatText).join("") + "</p>" : "") +
          (train.opens ? '<p class="muted small">Tickets on sale from ' + esc(train.opens) + " (China time)</p>" : "") +
          '<button type="button" class="btn btn--small" data-add-train="' + index + '" hidden>Add to trip</button>' +
          "</li>";
      }).join("") + "</ol>";
      if (!list.length) html += '<p class="muted">No trains match. Try another kind of train or an earlier time.</p>';
      if (list.length > state.shown) html += '<button type="button" class="btn" data-more>Show ' + Math.min(60, list.length - state.shown) + " more of " + list.length + "</button>";
      html += '<p class="muted small">Times are China time (CST). Seats left as of a few minutes ago, from China Railway (12306).</p>';
      panel.innerHTML = html;

      panel.querySelectorAll("[data-kind]").forEach(function (button) {
        button.addEventListener("click", function () { state.kind = button.dataset.kind; state.shown = 60; render(); });
      });
      panel.querySelector("[data-after]").addEventListener("change", function () { state.after = this.value; state.shown = 60; render(); });
      var exact = panel.querySelector("[data-exact]");
      if (exact) exact.addEventListener("change", function () { state.exact = exact.checked; state.shown = 60; render(); });
      var more = panel.querySelector("[data-more]");
      if (more) more.addEventListener("click", function () { state.shown += 60; render(); });

      // Signed in: one trip picker above the list, and an add button on each train.
      var picked = tripBox(panel.querySelector("[data-add-box]"), data.day, "Change the date above to a day of the trip.");
      if (picked) {
        panel.querySelectorAll("[data-add-train]").forEach(function (button) {
          var train = data.trains[parseInt(button.dataset.addTrain, 10)];
          button.hidden = false;
          button.addEventListener("click", function () {
            sendToTrip(picked, button, { train: {
              number: train.number, from_code: train.from_code, to_code: train.to_code, depart: train.depart, arrive: train.arrive,
              days: train.days, day: data.day, shown: data.shown
            } }, train.number);
          });
        });
      }
    }
  }

  // ---------- flights (signed in) ----------

  var flightForm = root.querySelector("[data-flight-form]");
  if (flightForm && root.dataset.flightsUrl) flightSearch();

  // The shortest way round the world between two points, as map points.
  function arc(a, b) {
    var rad = Math.PI / 180, lat1 = a[0] * rad, lon1 = a[1] * rad, lat2 = b[0] * rad, lon2 = b[1] * rad;
    var d = 2 * Math.asin(Math.sqrt(Math.pow(Math.sin((lat2 - lat1) / 2), 2) + Math.cos(lat1) * Math.cos(lat2) * Math.pow(Math.sin((lon2 - lon1) / 2), 2)));
    if (!d) return [a, b];
    var points = [];
    for (var i = 0; i <= 64; i++) {
      var f = i / 64, A = Math.sin((1 - f) * d) / Math.sin(d), B = Math.sin(f * d) / Math.sin(d);
      var x = A * Math.cos(lat1) * Math.cos(lon1) + B * Math.cos(lat2) * Math.cos(lon2);
      var y = A * Math.cos(lat1) * Math.sin(lon1) + B * Math.cos(lat2) * Math.sin(lon2);
      var z = A * Math.sin(lat1) + B * Math.sin(lat2);
      var lat = Math.atan2(z, Math.sqrt(x * x + y * y)) / rad, lon = Math.atan2(y, x) / rad;
      if (points.length) {  // keep going the same way across the 180° line
        var previous = points[points.length - 1][1];
        while (lon - previous > 180) lon -= 360;
        while (lon - previous < -180) lon += 360;
      }
      points.push([lat, lon]);
    }
    return points;
  }

  function flightSearch() {
    var statusLine = root.querySelector("[data-flight-status]");
    var panel = root.querySelector("[data-flight-results]");
    var dateInput = root.querySelector("[data-flight-date]");
    var inputs = { from: root.querySelector("[data-airport=from]"), to: root.querySelector("[data-airport=to]") };
    var lists = { from: root.querySelector("[data-airport-results=from]"), to: root.querySelector("[data-airport-results=to]") };
    var state = { from: null, to: null };
    function say(text) { statusLine.textContent = text || ""; }
    dateInput.value = isoDay(new Date());

    function label(airport) { return airport.code + " " + airport.name; }
    function drawAirports() {
      worldLayers.flights.clearLayers();
      var points = [];
      [["from", "A"], ["to", "B"]].forEach(function (end) {
        var airport = state[end[0]];
        if (airport) {
          L.marker([airport.lat, airport.lon], { icon: pin(end[1], end[0]), title: label(airport) }).addTo(worldLayers.flights);
          points.push([airport.lat, airport.lon]);
        }
      });
      var showing = activeTab === "world" && worldMode === "flights";
      if (points.length === 2) {
        var path = arc(points[0], points[1]);
        L.polyline(path, { color: css("--mode-flight") || "#0f6cbd", weight: 3, opacity: 0.85, dashArray: "8 6" }).addTo(worldLayers.flights);
        if (showing) map.fitBounds(L.latLngBounds(path), { padding: [40, 40], maxZoom: 8 });
      } else if (points.length && showing) {
        map.setView(points[0], 6);
      }
    }

    Object.keys(inputs).forEach(function (which) {
      inputs[which].addEventListener("input", function () { state[which] = null; });
      suggest(inputs[which], lists[which], function () { return 2; }, function (query) {
        return post(root.dataset.airportsUrl, { q: query })
          .then(function (data) { return data.results || []; })
          .catch(function (error) { say(error.message); return []; });
      }, function (airport) {
        return "<strong>" + esc(airport.code) + " · " + esc(airport.name) + "</strong>" +
          '<span class="muted small">' + esc(airport.city) + (airport.country ? ", " + esc(airport.country) : "") + (airport.local ? " · " + esc(airport.local) : "") + "</span>";
      }, function (airport) {
        state[which] = airport;
        inputs[which].value = label(airport);
        drawAirports();
        if (state.from && state.to) search();
        else say(which === "from" ? "Now choose the airport you're flying to." : "Now choose the airport you're flying from.");
      });
    });

    root.querySelector("[data-flight-swap]").addEventListener("click", function () {
      var from = state.from;
      state.from = state.to;
      state.to = from;
      inputs.from.value = state.from ? label(state.from) : "";
      inputs.to.value = state.to ? label(state.to) : "";
      drawAirports();
      if (state.from && state.to) search();
    });
    dateInput.addEventListener("change", function () { if (state.from && state.to && dateInput.value) search(); });
    flightForm.addEventListener("submit", function (event) { event.preventDefault(); search(); });

    function search() {
      if (!state.from || !state.to) { say("Choose both airports from the lists."); return; }
      if (!dateInput.value) { say("Choose the day you're flying."); return; }
      say("Looking up flights…");
      panel.innerHTML = "";
      post(root.dataset.flightsUrl, { from: state.from.code, to: state.to.code, date: dateInput.value })
        .then(function (data) { say(""); render(data); })
        .catch(function (error) { say(error.message); });
    }

    function render(data) {
      var count = data.flights.length;
      var html = '<h2 class="journey__subhead">' + esc(data.from.code) + " " + esc(data.from.city) + " → " + esc(data.to.code) + " " + esc(data.to.city) +
        " · " + esc(data.day_label) + "</h2>" +
        '<p class="muted small">' + count + (count === 1 ? " direct flight" : " direct flights") +
        ' · Book on <a href="' + esc(data.book) + '" target="_blank" rel="noopener">Trip.com</a>' +
        (data.allowance ? " · Flight data: " + data.allowance.left + " of " + data.allowance.of + " units left until " + esc(data.allowance.resets) : "") + "</p>";
      if (!count) {
        panel.innerHTML = html + '<p class="muted">No direct flights between these airports that day. Try a nearby airport, or a flight to a hub and on from there.</p>';
        return;
      }
      html += '<div class="journey-add" data-add-box></div><ol class="trains">' + data.flights.map(function (flight, index) {
        var day = flight.days > 0 ? "+" + flight.days : flight.days < 0 ? String(flight.days) : "";
        return '<li class="train">' +
          '<div class="train__head"><span class="train-chip train-chip--flight">' + esc(flight.number) + "</span>" +
          '<span class="muted small">' + esc(flight.airline) + (flight.aircraft ? " · " + esc(flight.aircraft) : "") + "</span></div>" +
          '<p class="train__times"><strong>' + esc(flight.depart) + "</strong> → <strong>" + esc(flight.arrive) + "</strong>" +
          (day ? '<sup title="Lands ' + esc(dayLabel(flight.arrive_day)) + ' local time">' + day + "</sup>" : "") +
          ' <span class="muted">' + esc(flight.duration) + "</span></p>" +
          (flight.terminal || flight.arrive_terminal ? '<p class="small">' + (flight.terminal ? "Terminal " + esc(flight.terminal) : "") +
            (flight.arrive_terminal ? (flight.terminal ? " → terminal " : "Arrives at terminal ") + esc(flight.arrive_terminal) : "") + "</p>" : "") +
          '<button type="button" class="btn btn--small" data-add-flight="' + index + '" hidden>Add to trip</button>' +
          "</li>";
      }).join("") + "</ol>" +
        '<p class="muted small">Times are local at each airport. Schedules can change; check with the airline before you book.</p>';
      panel.innerHTML = html;

      var picked = tripBox(panel.querySelector("[data-add-box]"), data.day, "Change the date above to a day of the trip.");
      if (picked) {
        panel.querySelectorAll("[data-add-flight]").forEach(function (button) {
          var flight = data.flights[parseInt(button.dataset.addFlight, 10)];
          button.hidden = false;
          button.addEventListener("click", function () {
            sendToTrip(picked, button, { flight: { number: flight.number, from: data.from.code, to: data.to.code, day: data.day } }, flight.number);
          });
        });
      }
    }
  }

  if (location.hash === "#international" || location.hash === "#china") showTab("world");
  if (location.hash === "#flights" && root.dataset.flightsUrl) { showTab("world"); showMode("flights"); }
})();
