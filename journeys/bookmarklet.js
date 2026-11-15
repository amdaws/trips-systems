// Send to Trips: a bookmarklet for the planner (see manage_views.capture). On a booking or sight page
// (Trip.com, Klook, a hotel's own site...), it collects what the page shows in your browser, where no
// site blocks it: the title, meta tags, schema.org data, map positions in the page's own data, and the
// words on the page. It opens the planner's capture page in a new tab with all of that in the address's
// #fragment (never sent to any server; compressed where the browser can), and the capture page sends it
// to the planner. Nothing is sent anywhere else. __CAPTURE_URL__ is filled in by the view.
(async () => {
  const target = "__CAPTURE_URL__";
  const tab = window.open("about:blank", "_blank");
  try {
    const cut = (text, most) => String(text || "").slice(0, most);
    const ld = [...document.querySelectorAll('script[type="application/ld+json"]')].map((s) => s.textContent).join("\n");
    const meta = {};
    document.querySelectorAll("meta[property],meta[name]").forEach((m) => {
      const key = m.getAttribute("property") || m.getAttribute("name");
      if (/^(og:|product:|place:|description$|geo\.|icbm$)/i.test(key) && m.content) meta[key] = cut(m.content, 2000);
    });
    const found = document.documentElement.innerHTML.match(/\\*"(lat|lng|lon|latitude|longitude|mapType)\\*"\s*:\s*\\*"?(-?\d{1,3}\.\d+|gg|gd|bd)/g) || [];
    const geo = found.slice(0, 40).map((s) => {
      const m = s.replace(/\\/g, "").match(/"(\w+)"\s*:\s*"?([^",}]+)/);
      return m ? JSON.stringify(m[1]) + ":" + (/^-?\d/.test(m[2]) ? m[2] : JSON.stringify(m[2])) : "";
    }).filter(Boolean).join(",");
    const data = { v: 1, url: location.href, title: document.title, meta, ld: cut(ld, 100000), geo, text: cut(document.body.innerText, 120000) };
    let bytes = new TextEncoder().encode(JSON.stringify(data));
    let mark = "j";
    if (window.CompressionStream) {
      const packed = new Blob([bytes]).stream().pipeThrough(new CompressionStream("deflate-raw"));
      bytes = new Uint8Array(await new Response(packed).arrayBuffer());
      mark = "z";
    }
    let binary = "";
    for (let i = 0; i < bytes.length; i += 8192) binary += String.fromCharCode.apply(null, bytes.subarray(i, i + 8192));
    const url = target + "#" + mark + btoa(binary).replace(/\+/g, "-").replace(/\//g, "_").replace(/=+$/, "");
    if (tab) tab.location.href = url; else location.href = url;
  } catch (error) {
    if (tab) tab.close();
    alert("Send to Trips couldn't read this page: " + error.message);
  }
})();
