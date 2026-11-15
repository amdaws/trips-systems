/*
 * Trip page: the travel-day list beside the map. Opening a day highlights it on
 * the map; picking a journey opens its details panel (a bottom sheet on phones)
 * and highlights it; the filter chips and the map's "Whole trip / This day"
 * switch narrow what's shown. Also the Share button on every trip page.
 * The map itself is trip-map.js, driven through window.tripMap.
 */
(function () {
  "use strict";

  function tripMap() { return window.tripMap; }

  function pressed(buttons, chosen) {
    buttons.forEach(function (button) { button.setAttribute("aria-pressed", String(button === chosen)); });
  }

  var page = document.querySelector("[data-trip-page]");
  if (page) {
    var panel = document.getElementById("journey-panel");
    var rows = Array.prototype.slice.call(page.querySelectorAll(".dayrow"));
    var filters = Array.prototype.slice.call(page.querySelectorAll("[data-filter]"));
    var scopes = Array.prototype.slice.call(page.querySelectorAll("[data-scope]"));
    var count = page.querySelector("[data-day-count]");
    var current = { day: null, journey: null, opener: null };

    var setOpen = function (row, open) {
      row.classList.toggle("is-open", open);
      row.querySelector("[data-toggle-day]").setAttribute("aria-expanded", String(open));
      row.querySelector(".dayrow__journeys").hidden = !open;
    };

    var openDay = function (day) {
      rows.forEach(function (row) { setOpen(row, row.dataset.day === String(day)); });
      current.day = String(day);
      if (tripMap()) tripMap().highlightDay(day, true);
    };

    var selectJourney = function (id, day, fromMap) {
      id = String(id);
      var row = page.querySelector('.dayrow[data-day="' + day + '"]');
      if (row && !row.classList.contains("is-open")) {
        rows.forEach(function (other) { setOpen(other, other === row); });
        current.day = String(day);
      }
      page.querySelectorAll("[data-journey]").forEach(function (card) {
        card.setAttribute("aria-pressed", String(card.dataset.journey === id));
      });
      panel.querySelectorAll("[data-journey-detail]").forEach(function (detail) {
        detail.hidden = detail.dataset.journeyDetail !== id;
      });
      panel.hidden = false;
      page.classList.add("has-journey");
      current.journey = id;
      if (tripMap()) { tripMap().invalidate(); tripMap().selectLeg(id); }
      var card = page.querySelector('[data-journey="' + id + '"]');
      if (fromMap && card) card.scrollIntoView({ block: "nearest", behavior: "smooth" });
      current.opener = card;
      var close = panel.querySelector('[data-journey-detail="' + id + '"] [data-close-journey]');
      if (close && window.matchMedia("(max-width: 56rem)").matches) close.focus({ preventScroll: true });
    };

    var closeJourney = function () {
      if (current.journey === null) return;
      panel.hidden = true;
      page.classList.remove("has-journey");
      page.querySelectorAll('[data-journey][aria-pressed="true"]').forEach(function (card) { card.setAttribute("aria-pressed", "false"); });
      current.journey = null;
      if (tripMap()) { tripMap().invalidate(); tripMap().clearLeg(); }
      if (current.opener) current.opener.focus({ preventScroll: true });
    };

    var applyFilter = function (group, button) {
      pressed(filters, button);
      var shown = 0;
      rows.forEach(function (row) {
        var any = false;
        row.querySelectorAll(".dayrow__journeys > li").forEach(function (item) {
          var show = group === "all" || item.dataset.group === group;
          item.hidden = !show;
          if (show) any = true;
        });
        row.hidden = !any;
        if (any) shown += 1;
      });
      page.querySelectorAll("[data-band]").forEach(function (band) { band.hidden = group !== "all"; });
      if (count) count.textContent = shown + " travel day" + (shown === 1 ? "" : "s");
      if (tripMap()) tripMap().setFilter(group);
    };

    var applyScope = function (scope) {
      pressed(scopes, scopes.filter(function (button) { return button.dataset.scope === scope; })[0]);
      if (tripMap()) tripMap().setScope(scope, current.day);
    };

    // Start with the first travel day open (the list is all open without JavaScript).
    rows.forEach(function (row, index) { setOpen(row, index === 0); });
    if (rows.length) current.day = rows[0].dataset.day;

    page.addEventListener("click", function (event) {
      var toggle = event.target.closest("[data-toggle-day]");
      if (toggle) {
        var row = toggle.closest(".dayrow");
        if (row.classList.contains("is-open")) {
          setOpen(row, false);
        } else {
          openDay(row.dataset.day);
        }
        return;
      }
      var card = event.target.closest("[data-journey]");
      if (card) {
        if (current.journey === card.dataset.journey) closeJourney();
        else selectJourney(card.dataset.journey, card.dataset.day, false);
        return;
      }
      if (event.target.closest("[data-close-journey]")) { closeJourney(); return; }
      var filter = event.target.closest("[data-filter]");
      if (filter) { applyFilter(filter.dataset.filter, filter); return; }
      var scope = event.target.closest("[data-scope]");
      if (scope) { applyScope(scope.dataset.scope); return; }
      var showDay = event.target.closest("[data-show-day]");
      if (showDay) { current.day = showDay.dataset.showDay; applyScope("day"); }
    });

    document.addEventListener("tripmap:leg", function (event) {
      selectJourney(event.detail.id, event.detail.day, true);
    });

    document.addEventListener("keydown", function (event) {
      if (event.key === "Escape" && current.journey !== null && !event.defaultPrevented) closeJourney();
    });

    // A link to one journey, as from Recently updated: #journey-<id>.
    var journeyFromHash = function () {
      var match = /^#journey-(\d+)$/.exec(window.location.hash);
      var card = match && page.querySelector('[data-journey="' + match[1] + '"]');
      if (!card) return;
      selectJourney(match[1], card.dataset.day, false);
      card.scrollIntoView({ block: "center" });
    };
    window.addEventListener("hashchange", journeyFromHash);
    journeyFromHash();
  }

  // Recently updated: following a link closes the list (it may point to this same page).
  document.querySelectorAll("[data-changes] a").forEach(function (link) {
    link.addEventListener("click", function () { link.closest("details").open = false; });
  });

  // Countdown above the itinerary: ticks every second until the time comes.
  var countdown = document.querySelector("[data-countdown]");
  if (countdown) {
    var target = Date.parse(countdown.dataset.countdown);
    var units = {};
    countdown.querySelectorAll("[data-unit]").forEach(function (el) { units[el.dataset.unit] = el; });
    var pad = function (n) { return (n < 10 ? "0" : "") + n; };
    var tick = function () {
      var left = Math.max(0, Math.floor((target - Date.now()) / 1000));
      units.days.textContent = Math.floor(left / 86400);
      units.days.nextElementSibling.textContent = Math.floor(left / 86400) === 1 ? "day" : "days";
      units.hours.textContent = pad(Math.floor(left % 86400 / 3600));
      units.minutes.textContent = pad(Math.floor(left % 3600 / 60));
      units.seconds.textContent = pad(left % 60);
      if (!left) {
        clearInterval(timer);
        countdown.querySelector("[data-countdown-label]").textContent = "It's time. Have a great trip!";
      }
    };
    var timer = setInterval(tick, 1000);
    tick();
  }

  // Share: the phone's share sheet where there is one, else copy the link.
  document.querySelectorAll("[data-share]").forEach(function (button) {
    button.addEventListener("click", function () {
      var data = { title: button.dataset.shareTitle || document.title, url: window.location.href };
      if (navigator.share) {
        navigator.share(data).catch(function () { /* closed without sharing */ });
        return;
      }
      if (!navigator.clipboard) return;
      navigator.clipboard.writeText(data.url).then(function () {
        var label = button.querySelector("[data-share-label]");
        if (!label) return;
        var before = label.textContent;
        label.textContent = "Link copied";
        setTimeout(function () { label.textContent = before; }, 2000);
      });
    });
  });
})();
