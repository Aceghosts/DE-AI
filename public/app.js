/* DE-AI front end — plain JS, no build step. */
(() => {
  "use strict";
  const $ = (s, el = document) => el.querySelector(s);
  const $$ = (s, el = document) => [...el.querySelectorAll(s)];
  const esc = (s) => String(s ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
  const sleep = (ms) => new Promise((r) => setTimeout(r, ms));

  const state = {
    page: null, items: [], byId: new Map(), notes: [], filter: "all", sort: "page", hideTiny: true,
    selected: new Set(), dims: {}, health: null, zipping: false, lastDeep: false,
  };

  const GROUPS = [
    ["video", "Videos"], ["audio", "Audio"], ["image", "Images"], ["document", "Files"],
  ];
  const FILTERS = [["all", "All"], ...GROUPS];
  const SOURCE_LABEL = {
    "<img> tag": "image on page", "page preview (og)": "share preview", "CSS background": "background image",
    "found in page code": "found in page code", "link on page": "linked from page", "video poster": "video cover",
    "<video> tag": "video on page", "<audio> tag": "audio on page", "site icon": "site icon",
    "structured data": "page data", "loaded by page": "loaded by page", "thumbnail": "thumbnail",
    "link tag": "page header", "the link itself": "the link itself", "<embed> tag": "embedded", "<object> tag": "embedded",
  };

  const ICON = {
    eye: '<svg viewBox="0 0 24 24"><path d="M2 12s3.6-7 10-7 10 7 10 7-3.6 7-10 7S2 12 2 12z"/><circle cx="12" cy="12" r="3"/></svg>',
    down: '<svg viewBox="0 0 24 24"><path d="M12 4v11m-5-5 5 5 5-5M5 20h14"/></svg>',
    film: '<svg viewBox="0 0 24 24"><rect x="3" y="5" width="18" height="14" rx="2"/><path d="M10 9.5v5l4-2.5z"/></svg>',
    wave: '<svg viewBox="0 0 24 24"><path d="M3 12h1M7 8v8M11 5v14M15 9v6M19 7v10M22 12h-1"/></svg>',
    doc: '<svg viewBox="0 0 24 24"><path d="M14 3H7a2 2 0 0 0-2 2v14a2 2 0 0 0 2 2h10a2 2 0 0 0 2-2V8z"/><path d="M14 3v5h5"/></svg>',
  };
  const MARK = `<svg class="mark" viewBox="0 0 100 100" preserveAspectRatio="none" aria-hidden="true"><path pathLength="1" d="M4,6 C22,2.5 48,4 74,2.8 C86,2.4 95,3.6 97.5,5 C98.6,30 96.8,58 97.6,95.5 C70,97.8 38,96.4 6,97 C3,72 4.4,40 2.6,9 C2.4,6.5 5,5.5 9,5.4"/></svg>`;

  // ---------- helpers ----------
  const isLight = () => state.health?.mode === "light";
  function fileUrl(it, { inline = false, url = null } = {}) {
    if (isLight()) return url || it.url;  // light: straight from the original site
    const p = new URLSearchParams({ url: url || it.url });
    if (it.referer) p.set("referer", it.referer);
    if (inline) p.set("inline", "1");
    else if (it.name) p.set("name", it.name);
    return "/api/file?" + p.toString();
  }
  function bytes(n) {
    if (!n) return "";
    const u = ["B", "KB", "MB", "GB", "TB"]; let i = 0;
    while (n >= 1024 && i < u.length - 1) { n /= 1024; i++; }
    return (i === 0 ? n : n.toFixed(n < 10 ? 1 : 0)) + " " + u[i];
  }
  function duration(s) {
    if (!s && s !== 0) return "";
    s = Math.round(s); const h = Math.floor(s / 3600), m = Math.floor((s % 3600) / 60), sec = s % 60;
    return (h ? h + ":" + String(m).padStart(2, "0") : m) + ":" + String(sec).padStart(2, "0");
  }
  const site = () => (state.page?.site || "").replace(/^www\./, "");
  const extOf = (it) => (it.ext || (it.mime || "").split("/")[1] || it.kind).replace(/[^a-z0-9]/gi, "").slice(0, 5);
  const groupOf = (it) => (it.mode === "ytdlp" ? "player" : it.kind);
  const filterKind = (it) => (it.kind === "stream" ? "video" : it.kind);
  function isTiny(it) {
    if (it.kind !== "image") return false;
    if (it.source === "site icon") return true;
    const d = state.dims[it.id];
    if (d && d.w <= 100 && d.h <= 100) return true;
    if (!d && it.size && it.size < 400) return true;
    return false;
  }
  const shown = (it) => !(state.hideTiny && it.mode === "direct" && isTiny(it));
  const matches = (it) => state.filter === "all" || filterKind(it) === state.filter;
  const visible = (it) => shown(it) && matches(it);

  function triggerDownload(href) {
    const a = document.createElement("a");
    a.href = href; a.download = ""; a.rel = "noopener";
    document.body.appendChild(a); a.click(); a.remove();
  }
  function toast(msg, kind = "") {
    const t = document.createElement("div");
    t.className = "toast " + kind; t.textContent = msg;
    $("#toasts").appendChild(t);
    setTimeout(() => t.remove(), kind === "error" ? 7000 : 3200);
  }
  async function api(path, body) {
    const r = await fetch(path, { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body) });
    let data = null;
    try { data = await r.json(); } catch { /* not json */ }
    if (r.status === 401) { location.reload(); throw new Error("Enter the team password first."); }
    if (!r.ok) {
      const d = data && data.detail;
      throw new Error(typeof d === "string" ? d : "Something went wrong (HTTP " + r.status + ").");
    }
    return data;
  }
  const store = {
    get(k, d) { try { return JSON.parse(localStorage.getItem(k)) ?? d; } catch { return d; } },
    set(k, v) { try { localStorage.setItem(k, JSON.stringify(v)); } catch { /* storage unavailable */ } },
  };

  // ---------- landing ----------
  function renderRecent() {
    const list = store.get("deai.recent", null) ?? store.get("snag.recent", []);
    $("#recent").hidden = !list.length;
    $("#recent-list").innerHTML = list.map((u) => `<li><button type="button" data-url="${esc(u)}">${esc(u)}</button></li>`).join("");
  }
  function remember(url) {
    const list = (store.get("deai.recent", null) ?? store.get("snag.recent", [])).filter((u) => u !== url);
    list.unshift(url); store.set("deai.recent", list.slice(0, 6));
  }
  async function loadHealth() {
    try {
      const h = await (await fetch("/api/health")).json();
      state.health = h;
      const notes = [];
      if (h.mode === "light") {
        document.body.classList.add("light");
        $("#light-note").hidden = false;
        $("#step3").textContent = "One at a time, or save the links you marked and download them all with IDM.";
        $("#sources-label").textContent = "Also reads the video players on these sites (whether a video downloads depends on the site)";
        $("#deep").disabled = true; $("#deep-label").hidden = true;
        return;
      }
      if (!h.ffmpeg) notes.push("ffmpeg isn't installed, so HD video and MP3 are off. Install it with: winget install Gyan.FFmpeg");
      if (!h.deep_scan) {
        notes.push(h.deep_scan_note || "Browser scanning isn't available.");
        $("#deep").disabled = true; $("#deep-label").classList.add("disabled");
      }
      if (notes.length) { const n = $("#health-note"); n.textContent = notes.join(" "); n.hidden = false; }
    } catch { /* the scan will report it */ }
  }

  // ---------- scan ----------
  let scanToken = 0;
  async function scan(rawUrl, { deep = $("#deep").checked } = {}) {
    const url = rawUrl.trim();
    if (!url) { $("#url").focus(); toast("Paste a link first."); return; }
    const token = ++scanToken;
    state.lastDeep = deep;
    document.body.classList.add("has-results");
    $("#landing").hidden = true; $("#results").hidden = true;
    state.selected.clear(); updateTray();
    const status = $("#status");
    status.hidden = false; status.className = "status wrap";
    status.innerHTML = `<p class="line"><span class="spinner"></span><span class="txt">Opening the page…</span></p><div class="skeleton">${"<div></div>".repeat(10)}</div>`;
    const steps = deep
      ? [[2500, "Loading it in a browser and scrolling through…"], [9000, "Collecting everything the page loaded…"], [20000, "Still working — heavy pages can take half a minute."]]
      : [[1600, "Looking for images, video, audio and files…"], [4500, "Checking for video players and streams…"], [11000, "Still working — video sites can take a few seconds."]];
    const timers = steps.map(([ms, text]) => setTimeout(() => { if (token === scanToken) { const l = $(".txt", status); if (l) l.textContent = text; } }, ms));
    $("#scan-btn").disabled = true;
    window.scrollTo({ top: 0 });
    try {
      const data = await api("/api/scan", { url, deep });
      if (token !== scanToken) return;
      data.items.forEach((it, i) => { it._i = i; });
      Object.assign(state, { page: data.page, items: data.items, notes: data.notes || [], dims: {}, filter: "all" });
      state.byId = new Map(data.items.map((i) => [i.id, i]));
      remember(url);
      const q = new URLSearchParams(location.search); q.set("url", url);
      history.replaceState(null, "", "?" + q.toString());
      status.hidden = true;
      renderResults();
    } catch (e) {
      if (token !== scanToken) return;
      status.className = "status wrap error";
      const msg = e.message === "Failed to fetch" ? "Can't reach the DE-AI server. Is its window still open?" : e.message;
      status.innerHTML = `<p class="line">${esc(msg)}</p>`;
    } finally {
      timers.forEach(clearTimeout);
      if (token === scanToken) $("#scan-btn").disabled = false;
    }
  }

  // ---------- results ----------
  function renderResults() {
    const p = state.page;
    $("#page-title").textContent = p.title || site() || "This page";
    const icon = state.items.find((i) => i.source === "site icon");
    $("#site-icon").innerHTML = icon
      ? `<img alt="" referrerpolicy="no-referrer" src="${esc(fileUrl(icon, { inline: true }))}" onerror="this.parentNode.textContent='${esc(site().charAt(0))}'">`
      : esc(site().charAt(0));
    $("#notes").innerHTML = state.notes.map((n) => `<li>${esc(n)}</li>`).join("");

    const reels = state.items.filter((i) => i.mode === "ytdlp");
    let html = "";
    if (reels.length) {
      html += `<section class="group" data-group="player">
        <div class="group-head"><h2>From the page's player</h2><span class="count"></span></div>
        <p class="group-sub">${isLight() ? "Videos and audio found in the page's player." : "Pick a quality, then download. Video and audio are fetched and joined into one file for you."}</p>
        <div class="reels">${reels.map(reelHTML).join("")}</div></section>`;
    }
    for (const [kind, label] of GROUPS) {
      const list = state.items.filter((i) => i.mode === "direct" && i.kind === kind);
      if (!list.length) continue;
      html += `<section class="group" data-group="${kind}">
        <div class="group-head"><h2>${label}</h2><span class="count"></span><button type="button" class="link-btn sel-group">Mark all</button></div>
        <div class="grid">${list.map(frameHTML).join("")}</div></section>`;
    }
    $("#sections").innerHTML = html;
    wirePreviews();
    if (isLight()) loadStreams();
    for (const box of $$("#sections .stream-thumb")) {
      if (previewObserver) previewObserver.observe(box); else { previewQueue.push(box); }
    }
    pumpPreviews();
    $("#results").hidden = false;
    applyView();
  }

  function applyView() {
    // Frames + reels
    for (const el of $$("#sections [data-id]")) {
      const it = state.byId.get(el.dataset.id);
      el.hidden = !visible(it);
      if (!shown(it) && state.selected.has(it.id)) toggle(it.id, false);
    }
    // Sort each grid
    const cmp = {
      page: (a, b) => a._i - b._i,
      large: (a, b) => (b.size || 0) - (a.size || 0) || a._i - b._i,
      small: (a, b) => (a.size || Infinity) - (b.size || Infinity) || a._i - b._i,
      name: (a, b) => (a.name || a.title || "").localeCompare(b.name || b.title || "", undefined, { numeric: true }),
    }[state.sort];
    for (const grid of $$("#sections .grid")) {
      const frames = $$(".frame", grid);
      frames.sort((x, y) => cmp(state.byId.get(x.dataset.id), state.byId.get(y.dataset.id)));
      frames.forEach((f) => grid.appendChild(f));
    }
    // Groups
    let total = 0;
    for (const g of $$("#sections .group")) {
      const n = $$("[data-id]", g).filter((el) => !el.hidden).length;
      g.hidden = n === 0;
      total += n;
      $(".count", g).textContent = n;
    }
    updateGroupButtons();
    renderFilters();
    // Meta line
    const many = total === 1 ? "1 item" : `${total} items`;
    $("#page-meta").innerHTML = `${esc(site())} · ${many} · <a href="${esc(state.page.final_url)}" target="_blank" rel="noopener noreferrer">Open page</a>`;
    renderEmpty(total);
    updateTray();
  }

  function renderFilters() {
    const c = { all: 0, video: 0, audio: 0, image: 0, document: 0 };
    for (const it of state.items) {
      if (!shown(it)) continue;
      c.all++; const k = filterKind(it); if (k in c) c[k]++;
    }
    if (state.filter !== "all" && !c[state.filter]) state.filter = "all";
    $("#filters").innerHTML = FILTERS.filter(([k]) => k === "all" || c[k] > 0)
      .map(([k, label]) => `<button type="button" role="tab" data-filter="${k}" aria-selected="${state.filter === k}">${label}<span class="n">${c[k]}</span></button>`).join("");
    $(".toolbar").hidden = c.all === 0 && !state.items.some(isTiny);
  }

  function renderEmpty(total) {
    const box = $("#empty");
    if (total > 0) { box.hidden = true; return; }
    const hiddenTiny = state.items.filter((i) => !shown(i)).length;
    const canDeep = !state.lastDeep && state.health?.deep_scan !== false;
    const actions = [];
    if (canDeep) actions.push(`<button type="button" class="btn primary" data-act="deep">Scan again in a browser</button>`);
    if (hiddenTiny) actions.push(`<button type="button" class="btn ghost" data-act="tiny">Show ${hiddenTiny} small image${hiddenTiny > 1 ? "s" : ""}</button>`);
    const unreachable = !state.items.length && state.notes.some((n) => /couldn't open|answered with HTTP/i.test(n));
    box.innerHTML = unreachable
      ? `<h2>Couldn't read this page</h2><p>Check that the link opens in your browser. Some sites block automated visitors; scanning in a browser can get past that.</p><div class="tray-actions">${actions.join("")}</div>`
      : `<h2>Nothing downloadable found on this page</h2>
      <p>${canDeep ? "Many sites build their pages with JavaScript, so the media only appears once the page runs in a browser." : "The site may load its media from somewhere else, block automated visitors, or protect it."}</p>
      <div class="tray-actions">${actions.join("")}</div>`;
    box.hidden = false;
  }

  // ---------- markup ----------
  function previewHTML(it) {
    const ext = extOf(it);
    if (it.kind === "image") return `<img loading="lazy" decoding="async" referrerpolicy="no-referrer" alt="" data-preview="img" src="${esc(fileUrl(it, { inline: true }))}">`;
    if (it.kind === "video") {
      if (it.thumb) return `<img loading="lazy" referrerpolicy="no-referrer" alt="" src="${esc(fileUrl(it, { inline: true, url: it.thumb }))}"><span class="play"></span>`;
      return `<video muted playsinline preload="metadata" referrerpolicy="no-referrer" data-preview="video" src="${esc(fileUrl(it, { inline: true }))}#t=0.5"></video><span class="play"></span>`;
    }
    if (it.kind === "audio") return `<div class="glyph">${ICON.wave}<b>${esc(ext)}</b></div>`;
    return `<div class="glyph">${ICON.doc}<b>${esc(ext)}</b></div>`;
  }
  function dimsText(it) {
    const d = state.dims[it.id];
    if (d && d.dur) return duration(d.dur);
    if (d && d.w) return `${d.w} × ${d.h}`;
    if (it.width && it.height) return `${it.width} × ${it.height}`;
    return "";
  }
  function metaText(it) {
    return [bytes(it.size), SOURCE_LABEL[it.source] || it.source].filter(Boolean).join(" · ");
  }
  function frameHTML(it) {
    const label = it.name || it.title || it.kind;
    return `<article class="frame" data-id="${it.id}">
      <button type="button" class="frame-hit" aria-pressed="false" aria-label="Mark ${esc(label)}" title="${esc(it.title || label)}">
        <div class="preview">${previewHTML(it)}</div>
        <span class="badge">${esc(extOf(it).toUpperCase())}</span>
        <span class="dims">${esc(dimsText(it))}</span>
        <span class="tick" aria-hidden="true"></span>
        ${MARK}
      </button>
      <div class="tools">
        <button type="button" class="t-view" aria-label="View ${esc(label)}" title="View">${ICON.eye}</button>
        <button type="button" class="t-dl" aria-label="Download ${esc(label)}" title="Download">${ICON.down}</button>
      </div>
      <div class="caption"><p class="name">${esc(label)}</p><p class="meta">${esc(metaText(it))}</p></div>
    </article>`;
  }
  function reelHTML(it) {
    const meta = [it.uploader, it.duration ? duration(it.duration) : null, it.height ? `up to ${it.height}p` : null,
      it.size ? "about " + bytes(it.size) : null, it.source && it.source !== "media player on page" ? (SOURCE_LABEL[it.source] || it.source) : null]
      .filter(Boolean).map((m) => `<span>${esc(m)}</span>`).join("");
    const opts = (it.options || [{ id: "best", label: "Best quality" }]);
    const pills = opts.map((o, i) => `<label><input type="radio" name="q-${it.id}" value="${esc(o.id)}"${i === 0 ? " checked" : ""}><span>${esc(o.label)}</span></label>`).join("");
    const thumb = it.thumb ? `<img loading="lazy" referrerpolicy="no-referrer" alt="" src="${esc(fileUrl(it, { inline: true, url: it.thumb }))}" onerror="this.remove()">` : "";
    const light = isLight();
    const d = it.direct;
    const stream = isStream(it);
    const lightActions = stream
      ? `<div class="stream-box" data-stream="${esc(it.url)}">
           <fieldset class="quality"><legend>Quality</legend><span class="muted-note">Reading the stream…</span></fieldset>
           <div class="reel-actions">
             <button type="button" class="btn primary stream-dl" disabled>${ICON.down.replace("<svg", '<svg width="18" height="18"')}Download</button>
             <div class="job" hidden><div class="meter"><i></i></div><p></p></div>
           </div>
         </div>`
      : d
      ? `<div class="reel-actions"><a class="btn primary" href="${esc(d.url)}" target="_blank" rel="noreferrer noopener" download>${ICON.down.replace("<svg", '<svg width="18" height="18"')}Download${d.height ? " " + d.height + "p" : ""}</a>
         <p class="muted-note">Straight from the site, and its link expires after a while, so download it now. If the video opens in a new tab instead, IDM will offer to grab it, or use the ⋮ menu on the video and choose Download.</p></div>`
      : `<div class="reel-actions"><a class="btn ghost" href="${esc(it.url)}" target="_blank" rel="noreferrer noopener">Open on the site</a>
         <p class="muted-note">This site splits video and audio or locks its links, so it needs the full version of DE-AI (or IDM's own download button on the video).</p></div>`;
    const thumbBox = stream && !it.thumb
      ? `<div class="reel-thumb stream-thumb" data-stream="${esc(it.url)}">
           <span class="ph">${ICON.film}</span>
           <video muted playsinline preload="none" disablepictureinpicture></video>
           <button type="button" class="play-btn" aria-label="Play a preview" hidden></button>
           <span class="dur" hidden></span>
         </div>`
      : `<div class="reel-thumb"><span class="ph">${it.kind === "audio" ? ICON.wave : ICON.film}</span>${thumb}${it.duration ? `<span class="dur">${duration(it.duration)}</span>` : ""}</div>`;
    return `<article class="reel" data-id="${it.id}">
      ${thumbBox}
      <div class="reel-body">
        <h3>${esc(it.title || streamName(it.url) || it.url)}</h3>
        <p class="meta">${meta}</p>
        ${light ? lightActions : `<fieldset class="quality"><legend>Quality</legend>${pills}</fieldset>
        <div class="reel-actions">
          <button type="button" class="btn primary reel-dl">${ICON.down.replace("<svg", '<svg width="18" height="18"')}Download</button>
          <div class="job" hidden><div class="meter"><i></i></div><p></p></div>
        </div>`}
      </div>
    </article>`;
  }

  // ---------- streams (light version: saved in the browser) ----------
  const isStream = (it) => it.kind === "stream" || /\.m3u8(\?|$)/i.test(it.url);
  function streamName(url) {
    try {
      const parts = decodeURIComponent(new URL(url).pathname).split("/").filter(Boolean);
      const clean = (x) => (x || "").replace(/\.(m3u8|mpd)$/i, "").replace(/[_-]+/g, " ").replace(/^,|,$/g, "").trim();
      let name = clean(parts.pop());
      if (/^(master|index|playlist|main|manifest|video|stream|prog_index|chunklist.*)$/i.test(name)) {
        const folder = clean(parts.pop());
        name = folder && !/^[\d,.]+$/.test(folder) ? folder : (state.page?.title || name);
      }
      return name;
    } catch { return ""; }
  }
  const streamJobs = new Map();

  // Previews: as a stream card scrolls into view, load a few seconds of its lowest
  // quality and show a frame from about 20% in. Clicking plays it in the card.
  let hlsLib = null;
  function loadHlsLib() {
    if (window.Hls) return Promise.resolve(window.Hls);
    if (!hlsLib) hlsLib = new Promise((res, rej) => {
      const sc = document.createElement("script");
      sc.src = "/vendor/hls.light.min.js"; sc.onload = () => res(window.Hls); sc.onerror = rej;
      document.head.appendChild(sc);
    });
    return hlsLib;
  }
  const previewQueue = []; let previewsRunning = 0;
  const previewObserver = "IntersectionObserver" in window ? new IntersectionObserver((entries) => {
    for (const e of entries) if (e.isIntersecting) { previewObserver.unobserve(e.target); previewQueue.push(e.target); }
    pumpPreviews();
  }, { rootMargin: "300px" }) : null;
  function pumpPreviews() {
    while (previewsRunning < 3 && previewQueue.length) {
      previewsRunning++;
      loadPreview(previewQueue.shift()).finally(() => { previewsRunning--; pumpPreviews(); });
    }
  }
  async function loadPreview(box) {
    const video = $("video", box);
    const native = !!video.canPlayType("application/vnd.apple.mpegurl");
    let hls = null;
    try {
      // Preview from the smallest version of the stream to save data.
      let url = box.dataset.stream;
      try {
        const list = await DEAIStream.variants(url);
        if (list.length) url = list[list.length - 1].url;
      } catch { /* fall back to the stream as given */ }
      const Hls = native ? null : await loadHlsLib();
      if (!native && !(Hls && Hls.isSupported())) return;
      await new Promise((resolve, reject) => {
        const timer = setTimeout(() => reject(new Error("slow")), 25000);
        video.addEventListener("loadedmetadata", () => {
          const d = video.duration;
          if (isFinite(d) && d > 0) { const dur = $(".dur", box); dur.textContent = duration(d); dur.hidden = false; }
          video.currentTime = isFinite(d) && d > 4 ? Math.min(d * 0.2, 60) : 0;
        }, { once: true });
        video.addEventListener("seeked", () => { clearTimeout(timer); resolve(); }, { once: true });
        video.addEventListener("error", () => { clearTimeout(timer); reject(new Error("media")); }, { once: true });
        if (native) { video.preload = "metadata"; video.src = url; }
        else {
          hls = new Hls({ maxBufferLength: 4, maxMaxBufferLength: 8, capLevelToPlayerSize: false, testBandwidth: false,
            xhrSetup: (xhr) => { xhr.withCredentials = false; } });
          hls.on(Hls.Events.ERROR, (_, data) => { if (data.fatal) { clearTimeout(timer); reject(new Error(data.details)); } });
          hls.loadSource(url); hls.attachMedia(video);
        }
      });
      if (hls) hls.stopLoad();  // keep the frame, stop downloading
      box.classList.add("has-preview");
      box._hls = hls;
      $(".play-btn", box).hidden = false;
    } catch {
      if (hls) hls.destroy();
      box.classList.add("no-preview");
    }
  }
  function playPreview(box) {
    // Switch from the small preview to the full stream (best quality the connection allows).
    const video = $("video", box), at = video.currentTime, full = box.dataset.stream;
    $(".play-btn", box).hidden = true;
    video.muted = false; video.controls = true;
    const go = () => { try { video.currentTime = at; } catch { /* ignore */ } video.play().catch(() => {}); };
    if (box._hls) {
      const hls = box._hls;
      hls.config.maxBufferLength = 30; hls.config.maxMaxBufferLength = 60;
      hls.loadSource(full);
      hls.once(window.Hls.Events.MANIFEST_PARSED, () => { hls.startLoad(at); go(); });
    } else {
      video.src = full;
      video.addEventListener("loadedmetadata", go, { once: true });
    }
  }
  async function loadStreams() {
    if (!window.DEAIStream) return;
    for (const box of $$("#sections .stream-box")) {
      const card = box.closest(".reel"); const it = state.byId.get(card.dataset.id);
      const fs = $(".quality", box), btn = $(".stream-dl", box);
      try {
        let list = await DEAIStream.variants(box.dataset.stream);
        const withAudio = list.filter((v) => !v.separateAudio);
        if (!withAudio.length) throw new DEAIStream.StreamError("audio", "This stream keeps its sound in a separate track, which the light version can't join. The full version of DE-AI can.");
        list = withAudio;
        it._variants = list;
        fs.innerHTML = `<legend>Quality</legend>` + list.map((v, i) => {
          const label = v.height ? `${v.height}p` : "Original";
          const rate = v.bandwidth ? ` · ${(v.bandwidth / 1e6).toFixed(1)} Mbit/s` : "";
          return `<label><input type="radio" name="sq-${it.id}" value="${i}"${i === 0 ? " checked" : ""}><span>${esc(label)}${i === 0 ? " (best)" : ""}<small class="rate">${esc(rate)}</small></span></label>`;
        }).join("");
        btn.disabled = false;
      } catch (e) {
        fs.innerHTML = `<legend>Quality</legend><span class="muted-note">${esc(e.message)}</span>`;
        btn.outerHTML = `<button type="button" class="btn ghost stream-copy" data-url="${esc(box.dataset.stream)}">Copy stream link</button>`;
      }
    }
  }
  async function downloadStream(card) {
    const it = state.byId.get(card.dataset.id);
    const box = $(".stream-box", card), btn = $(".stream-dl", box), job = $(".job", box);
    const bar = $(".meter i", job), msg = $("p", job);
    const running = streamJobs.get(it.id);
    if (running) { running.abort(); return; }
    const idx = +($(`input[name="sq-${it.id}"]:checked`, box)?.value || 0);
    const v = it._variants[idx];
    const ctrl = new AbortController(); streamJobs.set(it.id, ctrl);
    $$(".quality input", box).forEach((i) => (i.disabled = true));
    btn.textContent = "Cancel"; btn.classList.replace("primary", "ghost");
    job.hidden = false; job.className = "job"; bar.style.width = "1%"; msg.textContent = "Reading the stream…";
    try {
      const res = await DEAIStream.save(v.url, {
        signal: ctrl.signal,
        onProgress: (p) => {
          bar.style.width = Math.max(1, Math.round((p.done / p.total) * 100)) + "%";
          msg.textContent = `Piece ${p.done} of ${p.total} · ${bytes(p.bytes)} of about ${bytes(p.estimate)} · ${bytes(p.speed)}/s`;
        },
      });
      const base = (it.title || streamName(it.url) || "video").replace(/[\\/:*?"<>|]+/g, " ").trim().slice(0, 120);
      const name = `${base}${v.height ? " " + v.height + "p" : ""}.${res.ext}`;
      const href = URL.createObjectURL(res.blob);
      const a = document.createElement("a"); a.href = href; a.download = name;
      document.body.appendChild(a); a.click(); a.remove();
      setTimeout(() => URL.revokeObjectURL(href), 60000);
      job.classList.add("done"); bar.style.width = "100%";
      msg.textContent = `Saved ${name} (${bytes(res.bytes)}).` + (res.ext === "ts" ? " It's in .ts format; VLC plays it." : "");
    } catch (e) {
      job.classList.add("error");
      msg.textContent = e.name === "AbortError" ? "Cancelled." : e.message;
    } finally {
      streamJobs.delete(it.id);
      btn.innerHTML = `${ICON.down.replace("<svg", '<svg width="18" height="18"')}Download`; btn.classList.replace("ghost", "primary");
      $$(".quality input", box).forEach((i) => (i.disabled = false));
    }
  }

  function wirePreviews() {
    for (const img of $$('#sections img[data-preview="img"]')) {
      const done = () => {
        const id = img.closest("[data-id]").dataset.id;
        if (img.naturalWidth) { state.dims[id] = { w: img.naturalWidth, h: img.naturalHeight }; refreshFrame(id); scheduleView(); }
      };
      if (img.complete && img.naturalWidth) done(); else img.addEventListener("load", done, { once: true });
      img.addEventListener("error", () => { img.replaceWith(Object.assign(document.createElement("div"), { className: "glyph", innerHTML: "<small>No preview</small>" })); }, { once: true });
    }
    for (const v of $$('#sections video[data-preview="video"]')) {
      v.addEventListener("loadedmetadata", () => {
        const id = v.closest("[data-id]").dataset.id;
        state.dims[id] = { w: v.videoWidth, h: v.videoHeight, dur: v.duration }; refreshFrame(id);
      }, { once: true });
    }
  }
  function refreshFrame(id) {
    const el = $(`.frame[data-id="${id}"]`); const it = state.byId.get(id);
    if (el && it) $(".dims", el).textContent = dimsText(it);
  }
  let viewTimer = null;
  function scheduleView() { clearTimeout(viewTimer); viewTimer = setTimeout(applyView, 300); }

  // ---------- selection ----------
  function toggle(id, force) {
    const it = state.byId.get(id);
    if (!it || it.mode !== "direct") return;
    const on = force ?? !state.selected.has(id);
    if (on) state.selected.add(id); else state.selected.delete(id);
    const el = $(`.frame[data-id="${id}"]`);
    if (el) { el.classList.toggle("picked", on); $(".frame-hit", el).setAttribute("aria-pressed", String(on)); }
    if (viewing === id) $("#viewer-mark").textContent = on ? "Unmark" : "Mark";
    updateTray(); updateGroupButtons();
  }
  function groupFrames(g) { return $$(".frame", g).filter((f) => !f.hidden).map((f) => f.dataset.id); }
  function updateGroupButtons() {
    for (const g of $$("#sections .group")) {
      const b = $(".sel-group", g); if (!b) continue;
      const ids = groupFrames(g);
      b.textContent = ids.length && ids.every((id) => state.selected.has(id)) ? "Unmark all" : "Mark all";
    }
  }
  function updateTray() {
    const n = state.selected.size;
    if (!n && !state.zipping) { $("#tray").hidden = true; return; }
    $("#tray").hidden = false;
    let size = 0, unknown = 0;
    for (const id of state.selected) { const s = state.byId.get(id)?.size; if (s) size += s; else unknown++; }
    $("#tray-count").textContent = n ? `${n} marked${size ? " · " + bytes(size) + (unknown ? "+" : "") : ""}` : "";
    $("#zip-btn").textContent = isLight() ? (n === 1 ? "Download 1 file" : `Save ${n} links for IDM`) : (n === 1 ? "Download 1 file" : `Download ${n} as ZIP`);
    $("#copy-btn").hidden = !isLight() || n < 2;
    $("#zip-btn").disabled = state.zipping || !n;
  }

  // ---------- downloads ----------
  function downloadDirect(it) {
    if (isLight()) {
      const a = document.createElement("a");
      a.href = it.url; a.download = it.name || ""; a.target = "_blank"; a.rel = "noreferrer noopener";
      document.body.appendChild(a); a.click(); a.remove();
      toast(`Downloading ${it.name || "file"} from the original site`);
      return;
    }
    triggerDownload(fileUrl(it));
    toast(`Downloading ${it.name || "file"}`);
  }
  function selectedLinks() {
    return [...state.selected].map((id) => state.byId.get(id)).filter(Boolean).map((i) => i.url);
  }
  function saveLinkList() {
    const links = selectedLinks();
    const blob = new Blob([links.join("\r\n") + "\r\n"], { type: "text/plain" });
    const a = document.createElement("a");
    a.href = URL.createObjectURL(blob); a.download = `${site() || "media"} links.txt`;
    document.body.appendChild(a); a.click(); a.remove();
    setTimeout(() => URL.revokeObjectURL(a.href), 4000);
    toast(`Saved ${links.length} links. In IDM: Tasks → Import → From text file.`);
  }
  async function copyLinks() {
    const links = selectedLinks();
    try { await navigator.clipboard.writeText(links.join("\n")); toast(`Copied ${links.length} links.`); }
    catch { toast("Your browser blocked copying — use “Save links for IDM” instead.", "error"); }
  }
  async function runJob(payload, onUpdate) {
    let job = await api("/api/jobs", payload);
    onUpdate(job);
    while (job.status === "queued" || job.status === "running") {
      await sleep(600);
      const r = await fetch("/api/jobs/" + job.id);
      job = await r.json();
      if (!r.ok) throw new Error(job.detail || "Lost track of the download.");
      onUpdate(job);
    }
    if (job.status === "done") triggerDownload("/api/jobs/" + job.id + "/file");
    return job;
  }
  async function downloadReel(card) {
    const it = state.byId.get(card.dataset.id);
    const btn = $(".reel-dl", card), box = $(".job", card), bar = $(".meter i", box), msg = $("p", box);
    const option = $(`input[name="q-${it.id}"]:checked`, card)?.value || "best";
    btn.disabled = true; $$(".quality input", card).forEach((i) => (i.disabled = true));
    box.hidden = false; box.className = "job"; bar.style.width = "2%"; msg.textContent = "Starting…";
    try {
      const job = await runJob({ type: "media", url: it.url, option, title: it.title }, (j) => {
        bar.style.width = Math.max(2, Math.round((j.progress || 0) * 100)) + "%";
        msg.textContent = j.status === "error" ? j.error : j.message;
      });
      if (job.status === "done") {
        box.classList.add("done"); bar.style.width = "100%";
        msg.textContent = `Saved ${job.filename}${job.size ? " (" + bytes(job.size) + ")" : ""}. Check your downloads.`;
      } else { box.classList.add("error"); msg.textContent = job.error || "Download failed."; }
    } catch (e) { box.classList.add("error"); msg.textContent = e.message; }
    finally { btn.disabled = false; $$(".quality input", card).forEach((i) => (i.disabled = false)); }
  }
  async function downloadSelected() {
    const items = [...state.selected].map((id) => state.byId.get(id)).filter(Boolean);
    if (!items.length) return;
    if (items.length === 1) { downloadDirect(items[0]); return; }
    if (isLight()) { saveLinkList(); return; }
    state.zipping = true; updateTray();
    const box = $("#tray-job"), bar = $(".meter i", box), msg = $("#tray-msg");
    box.hidden = false; bar.style.width = "2%"; msg.textContent = "Starting…";
    try {
      const job = await runJob({ type: "zip", name: `${site() || "media"} media`, items: items.map((i) => ({ url: i.url, referer: i.referer, name: i.name })) }, (j) => {
        bar.style.width = Math.max(2, Math.round((j.progress || 0) * 100)) + "%";
        msg.textContent = j.status === "error" ? j.error : j.message;
      });
      if (job.status === "done") toast(`ZIP ready: ${job.filename} (${bytes(job.size)})${job.message !== "Ready" ? " — " + job.message.replace(/^Ready \(|\)$/g, "") : ""}`);
      else toast(job.error || "The ZIP couldn't be made.", "error");
    } catch (e) { toast(e.message, "error"); }
    finally { state.zipping = false; box.hidden = true; updateTray(); }
  }

  // ---------- viewer ----------
  let viewing = null;
  function viewable() {
    // Same order as on screen
    return $$("#sections .frame").filter((f) => !f.hidden && !f.closest(".group").hidden).map((f) => state.byId.get(f.dataset.id));
  }
  function openViewer(id) {
    const it = state.byId.get(id); if (!it) return;
    viewing = id;
    const stage = $("#viewer-stage"); const src = fileUrl(it, { inline: true });
    if (it.kind === "image") stage.innerHTML = `<img alt="${esc(it.title || "")}" referrerpolicy="no-referrer" src="${esc(src)}">`;
    else if (it.kind === "video") stage.innerHTML = `<video controls autoplay playsinline referrerpolicy="no-referrer" src="${esc(src)}"></video>`;
    else if (it.kind === "audio") stage.innerHTML = `<audio controls autoplay referrerpolicy="no-referrer" src="${esc(src)}"></audio>`;
    else stage.innerHTML = `<div class="glyph">There's no preview for this kind of file. Download it to open it.</div>`;
    $("#viewer-name").textContent = it.name || it.title || it.url;
    $("#viewer-meta").textContent = [dimsText(it), metaText(it), it.title && it.title !== it.name ? it.title : ""].filter(Boolean).join(" · ");
    $("#viewer-open").href = it.url;
    $("#viewer-mark").textContent = state.selected.has(id) ? "Unmark" : "Mark";
    if (!$("#viewer").open) $("#viewer").showModal();
  }
  function stepViewer(dir) {
    const list = viewable(); const i = list.findIndex((x) => x.id === viewing);
    if (i < 0 || !list.length) return;
    openViewer(list[(i + dir + list.length) % list.length].id);
  }
  function closeViewer() { $("#viewer-stage").innerHTML = ""; if ($("#viewer").open) $("#viewer").close(); viewing = null; }

  // ---------- events ----------
  $("#scan-form").addEventListener("submit", (e) => { e.preventDefault(); scan($("#url").value); });
  $("#paste-btn").addEventListener("click", async () => {
    try {
      const t = (await navigator.clipboard.readText()).trim();
      if (t) { $("#url").value = t; scan(t); } else toast("Your clipboard is empty.");
    } catch { $("#url").focus(); toast("Your browser blocked clipboard access — press Ctrl+V instead."); }
  });
  $("#recent-list").addEventListener("click", (e) => {
    const b = e.target.closest("button[data-url]"); if (!b) return;
    $("#url").value = b.dataset.url; scan(b.dataset.url);
  });
  $("#filters").addEventListener("click", (e) => {
    const b = e.target.closest("button[data-filter]"); if (!b) return;
    state.filter = b.dataset.filter; applyView();
  });
  $("#sort").addEventListener("change", (e) => { state.sort = e.target.value; applyView(); });
  $("#hide-tiny").addEventListener("change", (e) => { state.hideTiny = e.target.checked; applyView(); });
  $("#sections").addEventListener("click", (e) => {
    const t = e.target;
    const reelBtn = t.closest(".reel-dl"); if (reelBtn) { downloadReel(reelBtn.closest(".reel")); return; }
    const sBtn = t.closest(".stream-dl"); if (sBtn) { downloadStream(sBtn.closest(".reel")); return; }
    const pBtn = t.closest(".play-btn"); if (pBtn) { playPreview(pBtn.closest(".stream-thumb")); return; }
    const cBtn = t.closest(".stream-copy");
    if (cBtn) { navigator.clipboard.writeText(cBtn.dataset.url).then(() => toast("Stream link copied. IDM and VLC can open it."), () => toast("Couldn't copy the link.", "error")); return; }
    const sel = t.closest(".sel-group");
    if (sel) {
      const ids = groupFrames(sel.closest(".group"));
      const allOn = ids.every((id) => state.selected.has(id));
      ids.forEach((id) => toggle(id, !allOn)); return;
    }
    const frame = t.closest(".frame"); if (!frame) return;
    const id = frame.dataset.id;
    if (t.closest(".t-dl")) downloadDirect(state.byId.get(id));
    else if (t.closest(".t-view")) openViewer(id);
    else if (t.closest(".frame-hit")) toggle(id);
  });
  $("#sections").addEventListener("dblclick", (e) => {
    const frame = e.target.closest(".frame-hit"); if (!frame) return;
    const id = frame.closest(".frame").dataset.id; toggle(id); openViewer(id);
  });
  $("#empty").addEventListener("click", (e) => {
    const b = e.target.closest("[data-act]"); if (!b) return;
    if (b.dataset.act === "deep") { $("#deep").checked = true; scan($("#url").value, { deep: true }); }
    if (b.dataset.act === "tiny") { $("#hide-tiny").checked = false; state.hideTiny = false; applyView(); }
  });
  $("#clear-sel").addEventListener("click", () => [...state.selected].forEach((id) => toggle(id, false)));
  $("#zip-btn").addEventListener("click", downloadSelected);
  $("#copy-btn").addEventListener("click", copyLinks);
  $("#viewer-close").addEventListener("click", closeViewer);
  $("#viewer-prev").addEventListener("click", () => stepViewer(-1));
  $("#viewer-next").addEventListener("click", () => stepViewer(1));
  $("#viewer-mark").addEventListener("click", () => viewing && toggle(viewing));
  $("#viewer-dl").addEventListener("click", () => viewing && downloadDirect(state.byId.get(viewing)));
  $("#viewer").addEventListener("click", (e) => { if (e.target === e.currentTarget) closeViewer(); });
  $("#viewer").addEventListener("cancel", (e) => { e.preventDefault(); closeViewer(); });
  document.addEventListener("keydown", (e) => {
    if ($("#viewer").open) {
      if (e.key === "ArrowRight") stepViewer(1);
      if (e.key === "ArrowLeft") stepViewer(-1);
      if (e.key.toLowerCase() === "m" && viewing) toggle(viewing);
      return;
    }
    if (e.key === "/" && document.activeElement !== $("#url")) { e.preventDefault(); $("#url").focus(); $("#url").select(); }
  });
  document.addEventListener("dragover", (e) => e.preventDefault());
  document.addEventListener("drop", (e) => {
    const t = e.dataTransfer.getData("text/uri-list") || e.dataTransfer.getData("text/plain");
    if (t) { e.preventDefault(); $("#url").value = t.trim(); scan(t.trim()); }
  });

  // ---------- start ----------
  renderRecent();
  loadHealth().then(() => {
    const initial = new URLSearchParams(location.search).get("url");
    if (initial) { $("#url").value = initial; scan(initial); }
  });
  $("#url").focus();
})();
