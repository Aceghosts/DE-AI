/* DE-AI stream saver — downloads an HLS stream (.m3u8) entirely in the browser.
   Reads the playlist, fetches the pieces several at a time straight from the site,
   and repackages them into one MP4. Nothing is re-encoded, so quality is exactly
   what the site sends. Encrypted or DRM streams are refused, not worked around. */
(() => {
  "use strict";
  const PARALLEL = 6;
  const RETRIES = 3;

  const fetchOpts = { cache: "no-store", referrerPolicy: "no-referrer", credentials: "omit" };

  async function getText(url, signal) {
    let r;
    try { r = await fetch(url, { ...fetchOpts, signal }); }
    catch (e) {
      if (e.name === "AbortError") throw e;
      throw new StreamError("blocked", "This site doesn't let other pages read its stream, so it can't be saved from here.");
    }
    if (!r.ok) throw new StreamError("http", `The site refused the stream (HTTP ${r.status}).`);
    return { text: await r.text(), url: r.url || url };
  }

  class StreamError extends Error {
    constructor(code, msg) { super(msg); this.code = code; }
  }

  function attrs(line) {
    const out = {};
    const re = /([A-Z0-9-]+)=("[^"]*"|[^,]*)/g;
    let m;
    while ((m = re.exec(line))) out[m[1]] = m[2].replace(/^"|"$/g, "");
    return out;
  }

  /** Read a master or media playlist and list the qualities on offer, best first. */
  async function variants(url, signal) {
    const { text, url: base } = await getText(url, signal);
    if (!text.startsWith("#EXTM3U")) throw new StreamError("format", "This isn't a stream playlist.");
    const lines = text.split(/\r?\n/);
    const out = [];
    const audioGroups = {};
    for (const l of lines) {
      if (l.startsWith("#EXT-X-MEDIA:")) {
        const a = attrs(l);
        if (a.TYPE === "AUDIO" && a.URI) audioGroups[a["GROUP-ID"]] = true;
      }
    }
    for (let i = 0; i < lines.length; i++) {
      if (!lines[i].startsWith("#EXT-X-STREAM-INF:")) continue;
      const a = attrs(lines[i]);
      let j = i + 1;
      while (j < lines.length && (!lines[j] || lines[j].startsWith("#"))) j++;
      if (j >= lines.length) break;
      const res = (a.RESOLUTION || "").split("x");
      out.push({
        url: new URL(lines[j].trim(), base).href,
        width: +res[0] || null,
        height: +res[1] || null,
        bandwidth: +(a["AVERAGE-BANDWIDTH"] || a.BANDWIDTH) || 0,
        separateAudio: !!(a.AUDIO && audioGroups[a.AUDIO]),
        codecs: a.CODECS || "",
      });
    }
    if (!out.length) return [{ url: base, width: null, height: null, bandwidth: 0, separateAudio: false, single: true }];
    // One entry per height: keep the highest bitrate.
    const best = new Map();
    for (const v of out) {
      const k = v.height || v.bandwidth;
      if (!best.has(k) || best.get(k).bandwidth < v.bandwidth) best.set(k, v);
    }
    return [...best.values()].sort((a, b) => (b.height || 0) - (a.height || 0) || b.bandwidth - a.bandwidth);
  }

  /** Read a media playlist into a list of pieces. */
  async function pieces(url, signal) {
    const { text, url: base } = await getText(url, signal);
    const lines = text.split(/\r?\n/);
    if (lines.some((l) => l.startsWith("#EXT-X-STREAM-INF"))) {
      // We were given a master playlist; take its best quality.
      const v = await variants(url, signal);
      return pieces(v[0].url, signal);
    }
    const key = lines.find((l) => l.startsWith("#EXT-X-KEY:") && !/METHOD=NONE/.test(l));
    if (key) throw new StreamError("encrypted", "This stream is encrypted by the site, so DE-AI can't save it.");
    if (!lines.some((l) => l.startsWith("#EXT-X-ENDLIST"))) throw new StreamError("live", "This is a live stream, which can't be saved as a file.");
    const mapLine = lines.find((l) => l.startsWith("#EXT-X-MAP:"));
    const init = mapLine ? new URL(attrs(mapLine).URI, base).href : null;
    const segs = [];
    let duration = 0, pendingDur = 0, range = null, lastEnd = 0;
    for (const l of lines) {
      if (l.startsWith("#EXTINF:")) pendingDur = parseFloat(l.slice(8)) || 0;
      else if (l.startsWith("#EXT-X-BYTERANGE:")) {
        const [len, off] = l.slice(17).split("@");
        const start = off !== undefined ? +off : lastEnd;
        range = [start, start + +len - 1]; lastEnd = start + +len;
      } else if (l && !l.startsWith("#")) {
        segs.push({ url: new URL(l.trim(), base).href, range });
        duration += pendingDur; pendingDur = 0; range = null;
      }
    }
    if (!segs.length) throw new StreamError("format", "This stream has no pieces to download.");
    return { segs, init, duration, fmp4: !!init || /\.(m4s|mp4)(\?|$)/i.test(segs[0].url) };
  }

  async function fetchPiece(seg, signal) {
    let lastErr;
    for (let attempt = 0; attempt < RETRIES; attempt++) {
      try {
        const headers = seg.range ? { Range: `bytes=${seg.range[0]}-${seg.range[1]}` } : undefined;
        const r = await fetch(seg.url, { ...fetchOpts, headers, signal });
        if (!r.ok) throw new Error("HTTP " + r.status);
        return new Uint8Array(await r.arrayBuffer());
      } catch (e) {
        if (e.name === "AbortError") throw e;
        lastErr = e;
        await new Promise((res) => setTimeout(res, 600 * (attempt + 1)));
      }
    }
    throw new StreamError("piece", `A piece of the stream wouldn't download (${lastErr && lastErr.message}). Try again.`);
  }

  function loadMux() {
    if (window.muxjs) return Promise.resolve(window.muxjs);
    return new Promise((resolve, reject) => {
      const s = document.createElement("script");
      s.src = "/vendor/mux.min.js";
      s.onload = () => resolve(window.muxjs);
      s.onerror = () => reject(new StreamError("lib", "Couldn't load the MP4 packager."));
      document.head.appendChild(s);
    });
  }

  /**
   * Download a stream. onProgress({done, total, bytes, speed}) is called as pieces arrive.
   * Returns {blob, ext, bytes, seconds}.
   */
  async function save(variantUrl, { onProgress = () => {}, signal } = {}) {
    const plan = await pieces(variantUrl, signal);
    const total = plan.segs.length;
    const out = [];            // parts of the final file, in order
    let ext = "mp4";
    let mux = null, transmuxer = null, wroteInit = false;

    if (plan.fmp4) {
      if (plan.init) out.push(await fetchPiece({ url: plan.init }, signal));
    } else {
      mux = await loadMux();
      transmuxer = new mux.mp4.Transmuxer({ remux: true });
      transmuxer.on("data", (seg) => {
        if (!wroteInit) { out.push(new Uint8Array(seg.initSegment)); wroteInit = true; }
        out.push(new Uint8Array(seg.data));
      });
    }

    const ready = new Array(total);
    let next = 0, flushed = 0, done = 0, bytes = 0;
    const t0 = performance.now();
    const rawTs = [];          // kept only if repackaging fails, so we can still save a .ts

    const flushInOrder = () => {
      while (flushed < total && ready[flushed]) {
        const buf = ready[flushed];
        ready[flushed] = null;
        if (transmuxer) {
          rawTs.push(buf);
          transmuxer.push(buf);
          transmuxer.flush();
          if (wroteInit) rawTs.length = 0;  // packaging works; no need to keep the raw copy
        } else {
          out.push(buf);
        }
        flushed++;
      }
    };

    async function worker() {
      while (next < total) {
        const i = next++;
        const buf = await fetchPiece(plan.segs[i], signal);
        ready[i] = buf;
        done++; bytes += buf.length;
        const secs = (performance.now() - t0) / 1000;
        onProgress({ done, total, bytes, speed: bytes / Math.max(secs, 0.1), estimate: bytes / done * total });
        flushInOrder();
      }
    }
    await Promise.all(Array.from({ length: Math.min(PARALLEL, total) }, worker));
    flushInOrder();

    let parts = out;
    if (transmuxer && !wroteInit) { parts = rawTs; ext = "ts"; }  // fall back to the raw stream (plays in VLC)
    const blob = new Blob(parts, { type: ext === "mp4" ? "video/mp4" : "video/mp2t" });
    return { blob, ext, bytes: blob.size, seconds: plan.duration };
  }

  window.DEAIStream = { variants, save, StreamError };
})();
