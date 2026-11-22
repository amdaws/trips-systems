/* Send to Trips (journeys/bookmarklet.js, manage_views.capture): the page the bookmarklet brought, in this
   address's #fragment, is unpacked here; pick the trip, kind and day, the planner reads it, and the new
   plan's form opens and fills itself in (manage.js picks the result up from sessionStorage). */
(function () {
  "use strict";

  var trips = JSON.parse(document.getElementById("capture-trips").textContent);
  var urls = JSON.parse(document.getElementById("capture-urls").textContent);
  var box = document.querySelector("[data-capture]");
  var status = box.querySelector("[data-capture-status]");
  var tripSelect = box.querySelector("[data-capture-trip]");
  var kindSelect = box.querySelector("[data-capture-kind]");
  var daySelect = box.querySelector("[data-capture-day]");
  var dayField = box.querySelector("[data-capture-day-field]");
  var note = document.querySelector("[data-capture-note]");

  // The bookmarklet itself: dragged to the bookmarks bar, not clicked here (this site's rules block it).
  var link = document.querySelector("[data-bookmarklet]");
  link.addEventListener("click", function (event) {
    event.preventDefault();
    note.textContent = "Drag the button to your bookmarks bar, then click it there on a booking or sight page.";
  });
  var copy = document.querySelector("[data-bookmarklet-copy]");
  copy.addEventListener("click", function () {
    navigator.clipboard.writeText(link.href)
      .then(function () { note.textContent = "Copied. Edit a bookmark and paste this over its address."; })
      .catch(function () { note.textContent = "Couldn't copy here: press and hold the Send to Trips button and copy the link."; });
  });

  function csrf() {
    var match = document.cookie.match(/(?:^|;\s*)csrftoken=([^;]+)/);
    return match ? decodeURIComponent(match[1]) : (document.querySelector("input[name=csrfmiddlewaretoken]") || {}).value;
  }

  function unpack(packed) {
    var mark = packed.charAt(0), text = packed.slice(1).replace(/-/g, "+").replace(/_/g, "/");
    text += "===".slice(0, (4 - text.length % 4) % 4);
    var binary = atob(text), bytes = new Uint8Array(binary.length);
    for (var i = 0; i < binary.length; i++) bytes[i] = binary.charCodeAt(i);
    var unpacked = mark === "z"
      ? new Response(new Blob([bytes]).stream().pipeThrough(new DecompressionStream("deflate-raw"))).arrayBuffer()
      : Promise.resolve(bytes.buffer);
    return unpacked.then(function (buffer) { return JSON.parse(new TextDecoder().decode(buffer)); });
  }

  function host(url) {
    try { return new URL(url).hostname.replace(/^www\./, ""); } catch (error) { return ""; }
  }

  // What it probably is, from the address: a hotel page is a stay, a transfer or bus is transport.
  function guessKind(url) {
    if (/\/hotels?\/|hotel-detail|\/homes?\/|airbnb\.|agoda\.com|booking\.com\/hotel/i.test(url)) return "stay";
    if (/airport-transfer|\/transfers?\b|car-rental|\/bus\b|\/ferry|12go\.|\/car\b|chauffeur/i.test(url)) return "leg";
    return "activity";
  }

  // The trip on now, else the next to start, else the newest.
  function guessTrip() {
    var today = new Date().toISOString().slice(0, 10);
    var now = trips.filter(function (t) { return t.start <= today && today <= t.end; })[0];
    var next = trips.filter(function (t) { return t.start > today; }).sort(function (a, b) { return a.start < b.start ? -1 : 1; })[0];
    return (now || next || trips[0] || {}).id;
  }

  function fillDays() {
    var trip = trips.filter(function (t) { return String(t.id) === tripSelect.value; })[0];
    daySelect.innerHTML = "";
    daySelect.appendChild(new Option("Choose on the form", ""));
    (trip ? trip.days : []).forEach(function (day) { daySelect.appendChild(new Option(day.label, day.id)); });
    dayField.hidden = kindSelect.value !== "activity";
  }

  function read(capture) {
    var trip = tripSelect.value, kind = kindSelect.value;
    if (!trip) { status.textContent = "Add a trip in the planner first."; return; }
    var body = { capture: capture };
    var day = null;
    if (kind === "activity" && daySelect.value) {
      body.day = daySelect.value;
      day = trips.filter(function (t) { return String(t.id) === trip; })[0].days.filter(function (d) { return String(d.id) === daySelect.value; })[0];
    }
    status.textContent = "Reading the page…";
    fetch(urls[kind].replace("/0/", "/" + trip + "/"), {
      method: "POST", credentials: "same-origin",
      headers: { "Content-Type": "application/json", "X-CSRFToken": csrf() },
      body: JSON.stringify(body)
    })
      .then(function (response) { return response.json(); })
      .then(function (result) {
        if (result.error) { status.textContent = result.error; return; }
        sessionStorage.setItem("trips-capture", JSON.stringify({ kind: kind, url: capture.url, result: result, at: Date.now() }));
        location.href = urls[kind + "_form"].replace("/0/", "/" + trip + "/") + (day ? "?date=" + day.date : "");
      })
      .catch(function () { status.textContent = "Couldn't read it just now. Try again in a moment."; });
  }

  var packed = location.hash.slice(1);
  if (!packed) return;
  history.replaceState(null, "", location.pathname);  // the page stays here, not in the address or the history
  box.hidden = false;
  unpack(packed).then(function (capture) {
    box.querySelector("[data-capture-title]").textContent = capture.title || capture.url || "This page";
    box.querySelector("[data-capture-site]").textContent = host(capture.url);
    trips.forEach(function (trip) { tripSelect.appendChild(new Option(trip.title, trip.id)); });
    tripSelect.value = String(guessTrip() || "");
    kindSelect.value = guessKind(capture.url || "");
    fillDays();
    tripSelect.addEventListener("change", fillDays);
    kindSelect.addEventListener("change", fillDays);
    box.querySelector("[data-capture-read]").addEventListener("click", function () { read(capture); });
  }).catch(function () {
    status.textContent = "That page didn't arrive whole. Click Send to Trips on it again.";
  });
})();
