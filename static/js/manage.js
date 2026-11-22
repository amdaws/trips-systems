/*
 * Planner forms:
 * - place boxes: autocomplete from the local airport/station/city tables, auto-pick on
 *   leaving the box when the typed text clearly matches, "Search the map" (OpenStreetMap)
 * - transport form: From/To limited by mode; Auto-fill from free text; typing a flight or
 *   train number fills what's known; arrival date set automatically for overnight arrivals;
 *   two Brisbane, Melbourne or Sydney stations show the timetabled journey and fill its time
 */
(function () {
  "use strict";

  var PLACES_URL = "/manage/api/places/";
  var SEARCH_URL = "/manage/api/places/search/";
  var CREATE_URL = "/manage/api/places/create/";
  var NUMBER_RE = /^\s*([A-Z]{2}|[A-Z]\d|\d[A-Z]|[GDCZTKYSL])\s?\d{1,5}\s*$/i;

  function csrf() {
    var match = document.cookie.match(/(?:^|;\s*)csrftoken=([^;]+)/);
    if (match) return decodeURIComponent(match[1]);
    var input = document.querySelector("input[name=csrfmiddlewaretoken]");
    return input ? input.value : "";
  }

  function esc(text) {
    return String(text == null ? "" : text).replace(/[&<>"']/g, function (c) {
      return { "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c];
    });
  }

  // 24-hour times (forms.Time24Field): "720", "7:20", "7.20" and "0720" become "07:20" on leaving the box,
  // before the form's own change handlers see it (capture phase).
  function time24(text) {
    var m = String(text).trim().match(/^([01]?\d|2[0-3])[:.]?([0-5]\d)(?::[0-5]\d)?$/);
    return m ? m[1].padStart(2, "0") + ":" + m[2] : null;
  }
  document.addEventListener("change", function (event) {
    var input = event.target;
    if (!input.matches || !input.matches("input[data-time-24]")) return;
    var tidy = time24(input.value);
    if (tidy) input.value = tidy;
  }, true);

  function debounce(fn, ms) {
    var timer;
    return function () {
      clearTimeout(timer);
      timer = setTimeout(fn, ms);
    };
  }

  function postJSON(url, body) {
    return fetch(url, {
      method: "POST",
      credentials: "same-origin",
      headers: { "Content-Type": "application/json", "X-CSRFToken": csrf() },
      body: JSON.stringify(body)
    }).then(function (response) {
      if (!response.ok) throw new Error("HTTP " + response.status);
      return response.json();
    });
  }

  function getJSON(url, params) {
    return fetch(url + "?" + new URLSearchParams(params), { credentials: "same-origin" }).then(function (response) {
      if (!response.ok) throw new Error("HTTP " + response.status);
      return response.json();
    });
  }

  function setPicker(root, place) {
    root.querySelector("[data-place-id]").value = place.id;
    root.querySelector("[data-place-text]").value = place.label;
    root.querySelector("[data-place-status]").textContent = "✓ " + place.kind_label;
    root.dispatchEvent(new CustomEvent("place:picked", { bubbles: true }));
  }

  function addDays(iso, days) {
    var p = iso.split("-").map(Number);
    var d = new Date(p[0], p[1] - 1, p[2] + days);
    return d.getFullYear() + "-" + String(d.getMonth() + 1).padStart(2, "0") + "-" + String(d.getDate()).padStart(2, "0");
  }

  function prettyDate(iso) {
    var p = iso.split("-").map(Number);
    return new Date(p[0], p[1] - 1, p[2]).toLocaleDateString(undefined, { weekday: "short", day: "numeric", month: "short" });
  }

  // ---------- Place boxes ----------

  function setupPicker(root, index) {
    var hidden = root.querySelector("[data-place-id]");
    var text = root.querySelector("[data-place-text]");
    var list = root.querySelector(".ac-list");
    var status = root.querySelector("[data-place-status]");
    var mapButton = root.querySelector("[data-map-search]");
    var results = [];
    var active = -1;
    list.id = "place-list-" + index;
    text.setAttribute("aria-controls", list.id);

    function close() {
      list.hidden = true;
      text.setAttribute("aria-expanded", "false");
      text.removeAttribute("aria-activedescendant");
    }

    function render(items, fromMap) {
      results = items;
      active = -1;
      list.innerHTML = "";
      if (!items.length) {
        list.innerHTML = '<li aria-disabled="true" class="muted">' +
          (fromMap ? "Nothing found on the map." : "No matches here. Try 'Search the map'.") + "</li>";
      }
      items.forEach(function (place, i) {
        var li = document.createElement("li");
        li.setAttribute("role", "option");
        li.id = list.id + "-" + i;
        li.innerHTML = '<span class="ac-kind">' + esc(place.kind_label) + "</span>" + esc(place.label);
        li.addEventListener("mousedown", function (event) {
          event.preventDefault();
          choose(i);
        });
        list.appendChild(li);
      });
      list.hidden = false;
      text.setAttribute("aria-expanded", "true");
    }

    function highlight(i) {
      var options = list.querySelectorAll("li[role=option]");
      if (!options.length) return;
      active = (i + options.length) % options.length;
      options.forEach(function (option, n) { option.setAttribute("aria-selected", n === active ? "true" : "false"); });
      text.setAttribute("aria-activedescendant", options[active].id);
      options[active].scrollIntoView({ block: "nearest" });
    }

    function choose(i) {
      var place = results[i];
      if (!place) return;
      close();
      if (place.source_id) {
        status.textContent = "Saving…";
        postJSON(CREATE_URL, place)
          .then(function (saved) { setPicker(root, saved); })
          .catch(function () { status.textContent = "Couldn't save that place."; });
      } else {
        setPicker(root, place);
      }
    }

    function lookup(q) {
      return getJSON(PLACES_URL, { q: q, kinds: root.dataset.kinds || "" }).then(function (data) { return data.results; });
    }

    var search = debounce(function () {
      var q = text.value.trim();
      if (q.length < 2) { close(); return; }
      lookup(q).then(function (items) { if (text.value.trim() === q) render(items, false); });
    }, 180);

    text.addEventListener("input", function () {
      hidden.value = "";
      status.textContent = "";
      search();
    });
    text.addEventListener("keydown", function (event) {
      if (list.hidden) return;
      if (event.key === "ArrowDown") { event.preventDefault(); highlight(active + 1); }
      else if (event.key === "ArrowUp") { event.preventDefault(); highlight(active - 1); }
      else if (event.key === "Enter" && active >= 0) { event.preventDefault(); choose(active); }
      else if (event.key === "Escape") { close(); }
    });

    // Leaving the box without picking: choose the match if it's clear (exact code or name, or the only result).
    text.addEventListener("blur", function () {
      setTimeout(close, 150);
      var q = text.value.trim();
      if (hidden.value || q.length < 2) return;
      lookup(q).then(function (items) {
        if (hidden.value || text.value.trim() !== q || !items.length) return;
        var lower = q.toLowerCase();
        var exact = items.filter(function (p) {
          return (p.code && p.code.toLowerCase() === lower) || p.name.toLowerCase() === lower || p.local === q;
        });
        if (exact.length) setPicker(root, exact[0]);
        else if (items.length === 1) setPicker(root, items[0]);
        else status.textContent = "Pick one from the list";
      });
    });

    mapButton.addEventListener("click", function () {
      var q = text.value.trim();
      if (q.length < 2) { status.textContent = "Type what to search for first."; text.focus(); return; }
      status.textContent = "Searching the map…";
      getJSON(SEARCH_URL, { q: q })
        .then(function (data) {
          status.textContent = data.error || "";
          render(data.results.map(function (r) { r.kind_label = "Map"; return r; }), true);
          text.focus();
        })
        .catch(function () { status.textContent = "Map search failed. Try again in a moment."; });
    });
  }

  document.querySelectorAll(".place-picker").forEach(setupPicker);

  // ---------- Transport form ----------

  var legForm = document.querySelector("form[data-mode-kinds]");
  if (!legForm) return;

  var field = function (name) { return legForm.querySelector("[name=" + name + "]"); };
  var pickerFor = function (name) { return field(name).closest(".place-picker"); };
  var kindsByMode = JSON.parse(legForm.dataset.modeKinds);

  function applyKinds() {
    var kinds = (kindsByMode[field("mode").value] || []).join(",");
    ["origin", "destination"].forEach(function (name) { pickerFor(name).dataset.kinds = kinds; });
  }
  field("mode").addEventListener("change", applyKinds);
  applyKinds();

  // Overnight arrivals: if the arrival time is earlier than the departure, it's the next day.
  var autoArrival = null;
  function checkArrival() {
    var dep = field("depart_time").value, arr = field("arrive_time").value, date = field("depart_date").value;
    var arrival = field("arrive_date");
    if (dep && arr && date && arr < dep) {
      if (!arrival.value || arrival.value === autoArrival) {
        autoArrival = arrival.value = addDays(date, 1);
      }
    } else if (autoArrival && arrival.value === autoArrival) {
      arrival.value = autoArrival = "";
    }
  }
  ["depart_time", "arrive_time", "depart_date"].forEach(function (name) { field(name).addEventListener("change", checkArrival); });

  function describe(data) {
    var bits = [];
    if (data.mode) bits.push((data.number ? data.number + " " : "") + data.mode);
    if (data.operator) bits.push(data.operator);
    if (data.origin && data.destination) bits.push(data.origin.name + " → " + data.destination.name);
    if (data.depart_date) bits.push(prettyDate(data.depart_date));
    if (data.depart_time) bits.push(data.depart_time + (data.arrive_time ? "–" + data.arrive_time + (data.next_day ? " (+1 day)" : "") : ""));
    if (data.seat_class) bits.push(data.seat_class + " class");
    return bits.join(" · ");
  }

  // Put parsed values into the form. overwrite=false only fills fields that are still empty.
  var lastNumber = "";
  function fillLeg(data, overwrite) {
    var changed = 0;
    lastNumber = (data.number || "").toUpperCase();
    var set = function (name, value) {
      var f = field(name);
      if (!f || !value || (!overwrite && f.value)) return;
      f.value = value;
      f.dispatchEvent(new Event("change", { bubbles: true }));
      changed++;
    };
    set("mode", data.mode);
    set("number", data.number);
    set("operator", data.operator);
    set("seat_class", data.seat_class);
    set("depart_date", data.depart_date);
    set("depart_time", data.depart_time);
    set("arrive_time", data.arrive_time);
    set("arrive_date", data.arrive_date);
    ["origin", "destination"].forEach(function (name) {
      var picker = pickerFor(name);
      if (data[name] && (overwrite || !picker.querySelector("[data-place-id]").value)) {
        setPicker(picker, data[name]);
        changed++;
      }
    });
    checkArrival();
    return changed;
  }

  function parse(text) {
    return postJSON(legForm.dataset.parseUrl, { text: text, trip: legForm.dataset.trip, date: field("depart_date").value });
  }

  // Auto-fill box: any format, overwrites the form.
  var quick = document.getElementById("quick-entry");
  var quickStatus = document.querySelector("[data-quick-status]");
  function runQuick() {
    if (!quick.value.trim()) return;
    quickStatus.textContent = "Reading…";
    parse(quick.value)
      .then(function (data) {
        fillLeg(data, true);
        var summary = describe(data);
        quickStatus.textContent = (summary ? "Filled in: " + summary + ". " : "") +
          (data.messages.length ? data.messages.join(" ") : "Check the details, then save.");
      })
      .catch(function () { quickStatus.textContent = "Auto-fill isn't responding. Please fill the form in below."; });
  }
  if (quick) {
    document.querySelector("[data-quick-fill]").addEventListener("click", runQuick);
    quick.addEventListener("keydown", function (event) {
      if (event.key === "Enter") { event.preventDefault(); runQuick(); }
    });
    quick.addEventListener("paste", function () { setTimeout(runQuick, 0); });
  }

  // Two stations in Brisbane, Melbourne or Sydney: show the timetabled journey (lines, time, changes)
  // and fill the operator and arrival time if they're still empty.
  var journeyHint = document.createElement("p");
  journeyHint.className = "hint";
  journeyHint.setAttribute("aria-live", "polite");
  pickerFor("destination").insertAdjacentElement("afterend", journeyHint);
  var TIMETABLE_MODES = ["train", "metro", "ferry", "boat"];
  function checkJourney() {
    var from = pickerFor("origin").querySelector("[data-place-id]").value;
    var to = pickerFor("destination").querySelector("[data-place-id]").value;
    if (!from || !to || TIMETABLE_MODES.indexOf(field("mode").value) < 0) { journeyHint.textContent = ""; return; }
    getJSON(legForm.dataset.journeyUrl, { from: from, to: to })
      .then(function (data) {
        if (!data.found) { journeyHint.textContent = ""; return; }
        journeyHint.textContent = "Timetable: " + data.summary;
        if (!field("operator").value && data.operator) field("operator").value = data.operator;
        var dep = field("depart_time").value;
        if (dep && !field("arrive_time").value) {
          var parts = dep.split(":"), total = parseInt(parts[0], 10) * 60 + parseInt(parts[1], 10) + data.minutes;
          field("arrive_time").value = String(Math.floor(total / 60) % 24).padStart(2, "0") + ":" + String(total % 60).padStart(2, "0");
          checkArrival();
        }
      })
      .catch(function () { journeyHint.textContent = ""; });
  }
  legForm.addEventListener("place:picked", checkJourney);
  ["mode", "depart_time"].forEach(function (name) { field(name).addEventListener("change", checkJourney); });
  checkJourney();

  // Typing a flight or train number fills whatever is still empty (airline, and the route if it's known).
  var numberField = field("number");
  var numberHint = document.createElement("span");
  numberHint.className = "hint";
  numberHint.setAttribute("aria-live", "polite");
  numberField.insertAdjacentElement("afterend", numberHint);
  numberField.addEventListener("change", function () {
    var value = numberField.value.trim();
    if (!NUMBER_RE.test(value)) { numberHint.textContent = ""; return; }
    if (value.replace(/\s/g, "").toUpperCase() === lastNumber) return;  // just filled by auto-fill
    var modeWord = field("mode").value ? field("mode").value + " " : "";
    parse(modeWord + value)
      .then(function (data) {
        var count = fillLeg(data, false);
        numberHint.textContent = count ? "Filled in " + describe(data) : "";
      })
      .catch(function () { numberHint.textContent = ""; });
  });
})();

/* Stay, activity and transport forms: fill the details from a page link (or pasted page text). */
(function () {
  "use strict";

  var box = document.querySelector("[data-page-import]");
  if (!box) return;
  var kind = box.getAttribute("data-page-import");
  var form = box.parentElement.querySelector("form.form");
  var status = box.querySelector("[data-page-status]");

  function csrf() {
    var match = document.cookie.match(/(?:^|;\s*)csrftoken=([^;]+)/);
    return match ? decodeURIComponent(match[1]) : (form.querySelector("input[name=csrfmiddlewaretoken]") || {}).value;
  }

  function setPlace(name, place) {
    var input = form.querySelector("[name=" + name + "]");
    if (!input || !place) return;
    var picker = input.closest(".place-picker");
    picker.querySelector("[data-place-id]").value = place.id;
    picker.querySelector("[data-place-text]").value = place.label;
    picker.querySelector("[data-place-status]").textContent = "✓ " + place.kind_label;
  }

  var LOCAL_LABELS = { name_local: "local name", address_local: "local address" };

  function setter(filled, labels) {
    return function (name, value, onlyIfEmpty) {
      var field = form.querySelector("[name=" + name + "]");
      if (!field || !value || (onlyIfEmpty && field.value)) return;
      field.value = value;
      filled.push(labels[name] || name);
    };
  }

  // A price found on the booking page becomes the cost: an estimate while the plan is Planned (it goes
  // on the Budget page's estimate once saved), what it cost once it's Booked.
  function setCost(set, f) {
    if (!f.amount) return;
    var amount = f.amount, people = form.querySelector("[name=people]"), count = people ? parseInt(people.value, 10) : 0;
    if (f.price_is_total && count > 1) amount = (parseFloat(f.amount) / count).toFixed(2);  // a group or car price, shared out
    set("cost", amount);  // an activity's is per person: its Total updates to match
    set("cost_currency", f.currency);
    var cost = form.querySelector("[name=cost]");
    if (cost) cost.dispatchEvent(new Event("input", { bubbles: true }));
  }
  var COST_LABELS = { cost: "cost", cost_currency: "currency", opening_hours: "opening hours" };

  // A new plan filled in from a booking link is Planned (the link gives rough details, not a confirmed
  // booking), and remembers the link, so its edit page says how it was added.
  function markFromLink(source) {
    if (box.getAttribute("data-new") !== "1") return;
    var status = form.querySelector("[name=status]");
    if (status) status.value = "planned";
    var from = form.querySelector("[name=filled_from]");
    if (from) from.value = source;
  }
  function missingNote(result) {
    return result.missing && result.missing.length
      ? " Not on the page: " + result.missing.join(", ") + ". Add them when you can; the plan shows them as missing until then."
      : "";
  }

  function fillStay(result) {
    var f = result.fields, filled = [];
    var set = setter(filled, Object.assign({ url: "link", check_in_time: "check-in time", check_out_time: "check-out time",
                               check_in_date: "check-in date", check_out_date: "check-out date" }, LOCAL_LABELS, COST_LABELS));
    set("name", f.name);
    set("address", f.address);
    set("name_local", f.name_local);
    set("address_local", f.address_local);
    set("phone", f.phone);
    set("email", f.email);
    set("url", f.url);
    set("check_in_time", f.check_in_time);
    set("check_out_time", f.check_out_time);
    set("check_in_date", f.check_in_date);
    set("check_out_date", f.check_out_date);
    set("label", f.city, true);  // visitors see the label, so never the property's name
    setCost(set, f);
    if (f.rating) set("notes", "Rated " + f.rating + (f.site ? " on " + f.site : ""), true);
    if (result.city) { setPlace("city", result.city); filled.push("city"); }
    if (result.location) { setPlace("location", result.location); filled.push("map location"); }
    return filled.length
      ? "Filled in: " + filled.join(", ") + ". Times are the property's standard times; change them if your booking differs."
      : "Nothing new to fill in.";
  }

  function fillActivity(result) {
    var f = result.fields, filled = [];
    var set = setter(filled, Object.assign({}, LOCAL_LABELS, COST_LABELS));
    set("title", f.name);
    set("address", f.address);
    set("name_local", f.name_local);
    set("address_local", f.address_local);
    set("description", f.description, true);
    set("opening_hours", f.opening_hours);
    set("phone", f.phone);
    set("email", f.email);
    var links = form.querySelector("[name=links]");
    if (links && f.url && links.value.indexOf(f.url) === -1) {
      links.value = (links.value ? links.value.replace(/\s+$/, "") + "\n" : "") + (f.site || "Link") + " | " + f.url;
      filled.push("link");
    }
    if (result.place) { setPlace("place", result.place); filled.push("map location"); }
    setCost(set, f);
    if (!filled.length) return "Nothing new to fill in.";
    return "Filled in: " + filled.join(", ") + "." + (result.place ? " Directions from that night's hotel are worked out once you save." :
      " No map position found: pick the place with 'Search the map' to get directions.");
  }

  function fillLeg(result) {
    var f = result.fields, filled = [];
    var set = setter(filled, Object.assign({ seat_class: "class", url: "link", number: "service number" }, COST_LABELS));
    var mode = form.querySelector("[name=mode]");
    if (mode && f.mode) {
      mode.value = f.mode;
      mode.dispatchEvent(new Event("change", { bubbles: true }));
      filled.push("type (" + mode.options[mode.selectedIndex].text + ")");
    }
    set("operator", f.operator);
    set("number", f.number);
    set("seat_class", f.seat_class);
    set("url", f.url);
    setCost(set, f);
    var notes = form.querySelector("[name=notes]");
    if (notes && f.notes && notes.value.indexOf(f.notes) === -1) {
      notes.value = (notes.value ? notes.value.replace(/\s+$/, "") + "\n" : "") + f.notes;
      filled.push("notes");
    }
    var missing = [];
    if (result.origin) { setPlace("origin", result.origin); filled.push("from"); } else if (f.origin) missing.push("from (" + f.origin + ")");
    if (result.destination) { setPlace("destination", result.destination); filled.push("to"); } else if (f.destination) missing.push("to (" + f.destination + ")");
    if (!filled.length) return "Nothing new to fill in.";
    return "Filled in: " + filled.join(", ") + ". Check the from and to places, then set the date and times." +
      (missing.length ? " Couldn't find " + missing.join(" or ") + " on the map: search for it in the field." : "");
  }

  box.querySelectorAll("[data-page-fill]").forEach(function (button) {
    button.addEventListener("click", function () {
      var fromLink = button.getAttribute("data-page-fill") === "link";
      var body = fromLink ? { url: box.querySelector("input[type=url]").value.trim() } : { text: box.querySelector("textarea").value };
      var day = form.querySelector("[name=day]");
      if (day) body.day = day.value;
      // An activity's title as typed: stands in when the site (Trip.com) won't show the page, or for pasted text.
      var title = kind === "activity" ? form.querySelector("[name=title]") : null;
      if (title && title.value.trim()) body.title = title.value.trim();
      if (!(body.url || body.text || "").trim()) { status.textContent = fromLink ? "Paste a link first." : "Paste the page text first."; return; }
      status.textContent = fromLink ? "Reading the page…" : "Reading the text…";
      fetch(box.dataset.url, {
        method: "POST", credentials: "same-origin",
        headers: { "Content-Type": "application/json", "X-CSRFToken": csrf() },
        body: JSON.stringify(body)
      })
        .then(function (response) { return response.json(); })
        .then(function (result) {
          if (result.error) {
            status.textContent = result.error;
            if (fromLink) box.querySelector("details").open = true;
          } else {
            markFromLink(body.url || "pasted page text");
            status.textContent = fill(result);
            if (result.note && fromLink) box.querySelector("details").open = true;  // the page text gives the rest
          }
        })
        .catch(function () { status.textContent = "Couldn't read that. Try pasting the page text instead."; });
    });
  });

  function fill(result) {
    return (kind === "stay" ? fillStay(result) : kind === "leg" ? fillLeg(result) : fillActivity(result)) + missingNote(result) +
      (result.note ? " " + result.note : "") + (result.fields && result.fields.read_by === "claude" ? " (Read by Claude.)" : "");
  }

  // A page sent from the browser with Send to Trips (static/js/capture.js) opens a new plan's form: fill it in.
  try {
    var sent = JSON.parse(sessionStorage.getItem("trips-capture") || "null");
    if (sent && sent.kind === kind && box.getAttribute("data-new") === "1" && Date.now() - sent.at < 10 * 60 * 1000) {
      sessionStorage.removeItem("trips-capture");
      var linkBox = box.querySelector("input[type=url]");
      if (linkBox && sent.url) linkBox.value = sent.url;
      markFromLink(sent.url || "a page sent from the browser");
      status.textContent = "From your browser: " + fill(sent.result);
    }
  } catch (error) { /* storage blocked, or nothing sent: the form is as usual */ }
})();

/* An activity's form: Cost per person × People, shown as its Total as either is typed. */
(function () {
  "use strict";

  var each = document.querySelector("[data-cost-each]");
  var people = document.querySelector("[data-cost-people]");
  var total = document.querySelector("[data-cost-total]");
  if (!each || !people || !total) return;
  var currency = each.form.querySelector("[name=cost_currency]");

  function update() {
    var amount = parseFloat(each.value), count = parseInt(people.value, 10) || parseInt(people.defaultValue, 10) || 1;
    total.value = isNaN(amount) ? "" : (currency ? currency.value + " " : "") +
      (amount * count).toLocaleString("en-AU", { minimumFractionDigits: 2, maximumFractionDigits: 2 });
  }
  [each, people, currency].forEach(function (input) {
    if (input) { input.addEventListener("input", update); input.addEventListener("change", update); }
  });
  update();
})();

/* Activities, stays and budget lines: tick several to change them together (public or private, move to
   another day, delete, or who paid). */
(function () {
  "use strict";

  var bar = document.querySelector("[data-bulk-bar]");
  if (!bar) return;
  var boxes = Array.prototype.slice.call(document.querySelectorAll("[data-bulk-item]"));
  var groups = Array.prototype.slice.call(document.querySelectorAll("[data-bulk-group]"));
  var all = bar.querySelector("[data-bulk-all]");
  var count = bar.querySelector("[data-bulk-count]");
  var note = bar.querySelector("[data-bulk-note]");
  var actions = Array.prototype.slice.call(bar.querySelectorAll("[data-bulk-action]"));
  var last = null;

  function ticked(list) {
    return list.filter(function (box) { return box.checked; }).length;
  }

  function mark(box, list) {
    var n = ticked(list);
    box.checked = n > 0 && n === list.length;
    box.indeterminate = n > 0 && n < list.length;
  }

  function onDay(group) {
    return boxes.filter(function (box) { return box.dataset.day === group.dataset.bulkGroup; });
  }

  function refresh() {
    var n = ticked(boxes);
    count.textContent = n ? n + " selected" : "Select all";
    mark(all, boxes);
    groups.forEach(function (group) { mark(group, onDay(group)); });
    actions.forEach(function (control) { control.disabled = n === 0; });
    bar.classList.toggle("is-active", n > 0);
    boxes.forEach(function (box) {  // an activity, or a table row (stays)
      var row = box.closest(".activity, [data-bulk-row]");
      if (row) row.classList.toggle("is-selected", box.checked);
    });
    if (!n) note.textContent = "";
  }

  boxes.forEach(function (box, index) {
    box.addEventListener("click", function (event) {
      if (event.shiftKey && last !== null) {  // tick (or untick) the whole run since the last one
        var from = Math.min(last, index), to = Math.max(last, index);
        for (var i = from; i <= to; i++) boxes[i].checked = box.checked;
      }
      last = index;
      refresh();
    });
  });

  all.addEventListener("change", function () {
    boxes.forEach(function (box) { box.checked = all.checked; });
    refresh();
  });

  groups.forEach(function (group) {
    group.addEventListener("change", function () {
      onDay(group).forEach(function (box) { box.checked = group.checked; });
      refresh();
    });
  });

  bar.querySelector("[data-bulk-clear]").addEventListener("click", function () {
    boxes.forEach(function (box) { box.checked = false; });
    last = null;
    refresh();
  });

  bar.addEventListener("submit", function (event) {
    var button = event.submitter;
    if (button && button.value === "paid_by" && !bar.querySelector("[name=paid_by]").value) {
      event.preventDefault();
      note.textContent = "Pick who paid first.";
      bar.querySelector("[name=paid_by]").focus();
      return;
    }
    if (button && button.value === "move" && !bar.querySelector("[name=day]").value) {
      event.preventDefault();
      note.textContent = "Pick the day to move them to first.";
      bar.querySelector("[name=day]").focus();
    }
  });

  refresh();
})();
