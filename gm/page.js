// Ghost Manga 5 page agent. Injected at document start (Page.addScriptToEvaluateOnNewDocument)
// and into already-open pages. Python polls __gm.scan() and pushes results with __gm.apply().
// Every call that targets an element carries the document token and the source/version the
// job was made for, so late results never land on a different image or a newer page.
(() => {
  if (window.__gm || window.top !== window) return;
  const MIN_SIDE = __GM_MIN_SIDE__;
  const LAZY = ["data-src", "data-original", "data-lazy-src", "data-lazy", "data-url", "data-cfsrc",
                "data-echo", "data-original-src", "data-img", "data-image"];
  const LAZY_SET = ["data-srcset", "data-lazy-srcset"];
  const gm = window.__gm = {
    token: Math.random().toString(36).slice(2) + Date.now().toString(36),
    n: 0,                 // id counter (never reused within this document)
    els: new Map(),       // id -> element
    keys: new Map(),      // id -> {key, srcAttr} at scan time
    pendingKeys: new Set(),
    done: new Map(),      // original url -> translated blob url
    canv: new Map(),      // id -> {tr, orig, version}
    showOrig: false,
    csp: [],
    errors: [],
    scans: 0,
    selfDraw: false,
  };

  const note = (arr, s) => { if (arr.length < 30) arr.push(String(s).slice(0, 160)); };
  addEventListener("securitypolicyviolation", e => note(gm.csp, e.effectiveDirective + " " + e.blockedURI), true);

  // --- canvas readers: remember when the site draws so new pages get re-translated
  try {
    const P = CanvasRenderingContext2D.prototype;
    for (const name of ["drawImage", "putImageData"]) {
      const orig = P[name];
      P[name] = function (...a) {
        if (!gm.selfDraw && this.canvas) this.canvas.__gmDrawn = performance.now();
        return orig.apply(this, a);
      };
    }
  } catch (e) { note(gm.errors, "canvas hook: " + e); }

  const abs = u => { try { return new URL(u, location.href).href; } catch (e) { return u || ""; } };
  const keyOf = el => el.currentSrc || el.src || "";
  const winOf = el => (el.ownerDocument && el.ownerDocument.defaultView) || window;
  const visible = el => {
    const cs = winOf(el).getComputedStyle(el);
    return cs.display !== "none" && cs.visibility !== "hidden";
  };
  function frameOffset(el) {           // offset of same-origin iframes up to the top page
    let x = 0, y = 0, w = winOf(el);
    while (w && w !== window && w.frameElement) {
      const r = w.frameElement.getBoundingClientRect();
      x += r.left; y += r.top; w = w.parent;
    }
    return { x, y };
  }
  function viewRect(el) {
    const r = el.getBoundingClientRect(), o = frameOffset(el);
    return { left: r.left + o.x, top: r.top + o.y, width: r.width, height: r.height, bottom: r.bottom + o.y };
  }

  const blockedFrames = [];
  function collect(root, sel, out, depth, deep) {   // deep: also walk open shadow roots (costly)
    root.querySelectorAll(sel).forEach(e => out.push(e));
    if (deep) root.querySelectorAll("*").forEach(e => e.shadowRoot && collect(e.shadowRoot, sel, out, depth, deep));
    if (depth < 2) root.querySelectorAll("iframe,frame").forEach(f => {
      let d = null;
      try { d = f.contentDocument; } catch (e) {}
      if (d && d.documentElement) collect(d, sel, out, depth + 1, deep);
      else {
        const r = f.getBoundingClientRect();
        if (r.width > 300 && r.height > 300 && f.src) blockedFrames.push(f.src);
      }
    });
    return out;
  }

  function looksPlaceholder(img) {
    const cur = img.getAttribute("src") || "";
    return !cur || cur.startsWith("data:") || /blank|placeholder|loading|lazy|spacer|grey|gray|pixel|transparent/i.test(cur) ||
           (img.complete && img.naturalWidth > 0 && Math.max(img.naturalWidth, img.naturalHeight) < MIN_SIDE);
  }

  function forceLazy(img) {
    if (img.__gmForced) return;
    img.__gmForced = true;
    try { img.loading = "eager"; } catch (e) {}
    for (const a of LAZY) {
      const v = (img.getAttribute(a) || "").trim();
      if (!v || v.startsWith("data:")) continue;
      if (abs(v) !== abs(img.getAttribute("src") || "") && looksPlaceholder(img)) img.src = v;
      return;
    }
    for (const a of LAZY_SET) {
      const v = (img.getAttribute(a) || "").trim();
      if (v && looksPlaceholder(img)) {
        const parts = v.split(",").map(s => s.trim().split(/\s+/)[0]).filter(Boolean);
        if (parts.length) img.src = parts[parts.length - 1];
        return;
      }
    }
  }

  // Remove everything that could make the browser show another source than ours.
  function strip(img, origKey, url) {
    const pic = img.parentElement && img.parentElement.tagName === "PICTURE" ? img.parentElement : null;
    if (pic) pic.querySelectorAll("source").forEach(s => {
      for (const a of ["srcset", ...LAZY_SET]) if (s.hasAttribute(a)) { s.setAttribute("data-gm-" + a, s.getAttribute(a)); s.removeAttribute(a); }
    });
    for (const a of ["srcset", "sizes", ...LAZY_SET]) if (img.hasAttribute(a)) { img.setAttribute("data-gm-" + a, img.getAttribute(a)); img.removeAttribute(a); }
    for (const a of LAZY) {             // only lazy attrs that point at the image we translated
      const v = img.getAttribute(a);
      if (v && abs(v) === origKey) { if (!img.hasAttribute("data-gm-" + a)) img.setAttribute("data-gm-" + a, v); img.setAttribute(a, url); }
    }
  }
  function show(img) {
    const want = gm.showOrig ? img.dataset.gmOrig : img.dataset.gmUrl;
    if (want && img.src !== want) img.src = want;
  }
  function markDone(img, origKey, url) {
    img.dataset.gmState = "done";
    img.dataset.gmOrig = origKey;
    img.dataset.gmUrl = url;
    strip(img, origKey, url);
    show(img);
  }

  function forgetEl(el) {
    const id = +el.dataset.gmId || 0;
    if (id) { const k = gm.keys.get(id); if (k) gm.pendingKeys.delete(k.key); gm.keys.delete(id); gm.els.delete(id); }
    delete el.dataset.gmId; delete el.dataset.gmKey; delete el.dataset.gmState; delete el.dataset.gmUrl; delete el.dataset.gmOrig;
  }

  function prune() {
    for (const [id, el] of gm.els) {
      if (el.isConnected) continue;
      const k = gm.keys.get(id); if (k) gm.pendingKeys.delete(k.key);
      gm.els.delete(id); gm.keys.delete(id);
      const c = gm.canv.get(id);
      if (c) { try { c.tr && c.tr.close(); c.orig && c.orig.close(); } catch (e) {} gm.canv.delete(id); }
    }
  }

  function pruneDone() {          // SPA chapter switch: free translated blobs nothing shows any more
    const live = new Set();
    collect(document, "img", [], 0, true).forEach(i => i.dataset.gmUrl && live.add(i.dataset.gmUrl));
    for (const [k, url] of gm.done) if (!live.has(url)) { URL.revokeObjectURL(url); gm.done.delete(k); }
  }

  gm.scan = () => {
    gm.scans++;
    if (gm.scans % 10 === 0) prune();
    const path = location.pathname;
    if (gm.lastPath && gm.lastPath !== path) pruneDone();
    gm.lastPath = path;
    blockedFrames.length = 0;
    const out = [];
    const now = performance.now();
    const vh = innerHeight;
    // one list in document order so <img> and <canvas> share the same page numbering
    const all = collect(document, "img,canvas", [], 0, gm.scans % 5 === 0);
    all.forEach((el, ord) => {
      if (el.tagName === "CANVAS") { scanCanvas(el, ord, now, out, vh); return; }
      if (el.dataset.gmState === "done") return;
      forceLazy(el);
      if (!el.complete || !el.naturalWidth) return;
      const nw = el.naturalWidth, nh = el.naturalHeight;
      if (Math.max(nw, nh) < MIN_SIDE || Math.min(nw, nh) < 120) return;
      const k = keyOf(el);
      if (!k || /\.svg(\?|#|$)/i.test(k) || k.startsWith("data:image/svg")) return;
      if (gm.done.has(k)) { markDone(el, k, gm.done.get(k)); return; }   // site re-created the node
      if (el.dataset.gmId && gm.els.get(+el.dataset.gmId) === el && el.dataset.gmKey === k) return;
      if (gm.pendingKeys.has(k)) return;                                   // same image already queued
      if (!visible(el)) return;
      const r = viewRect(el);
      if (r.width < 80 || r.height < 60) return;
      const id = ++gm.n;
      el.dataset.gmId = id; el.dataset.gmKey = k;
      gm.els.set(id, el); gm.keys.set(id, { key: k, srcAttr: el.src }); gm.pendingKeys.add(k);
      out.push({ id, kind: "img", src: k, ord, vtop: r.top, vbot: r.bottom, version: 0 });
    });
    return JSON.stringify({ token: gm.token, path: location.pathname + location.search, vh, items: out,
                            frames: blockedFrames.slice(0, 3) });
  };

  function scanCanvas(cv, ord, now, out, vh) {
    if (cv.width < MIN_SIDE || cv.height < 150 || !cv.__gmDrawn || !visible(cv)) return;
    if (now - cv.__gmDrawn < 600) return;                       // still drawing
    if (cv.__gmStamp === cv.__gmDrawn) return;                  // this version already queued
    const r = viewRect(cv);
    if (r.width < 120 || r.height < 120) return;
    cv.__gmStamp = cv.__gmDrawn;
    let id = +cv.dataset.gmId || 0;
    if (!id) { id = ++gm.n; cv.dataset.gmId = id; }
    gm.els.set(id, cv);
    gm.keys.set(id, { key: "", srcAttr: "", version: cv.__gmDrawn });
    out.push({ id, kind: "canvas", src: "", ord, vtop: r.top, vbot: r.bottom, version: cv.__gmDrawn });
  }

  // live viewport positions of queued elements (for "translate what I'm looking at first").
  // null = element gone, "stale" = element now shows another page than the job was made for.
  gm.pos = (token, ids) => {
    if (token !== gm.token) return null;
    const out = {};
    for (const id of ids) {
      const el = gm.els.get(id), k = gm.keys.get(id);
      if (!el || !el.isConnected || !k) { out[id] = null; continue; }
      if (!current(id, k.key, k.version)) { out[id] = "stale"; continue; }
      const r = viewRect(el);
      out[id] = [r.top, r.bottom];
    }
    return out;
  };

  // Python dropped these jobs: forget their queue state so the images can be queued again
  gm.drop = (token, ids) => {
    if (token !== gm.token) return false;
    for (const id of ids) {
      const k = gm.keys.get(id), el = gm.els.get(id);
      if (el && el.tagName === "CANVAS") continue;   // canvas ids are reused by newer drawings
      if (k && k.key) gm.pendingKeys.delete(k.key);
      gm.keys.delete(id); gm.els.delete(id);
      if (el && +el.dataset.gmId === id && el.dataset.gmState !== "done") {
        delete el.dataset.gmId; delete el.dataset.gmKey;
      }
    }
    return true;
  };

  // Is element `id` still showing what the job was made for?
  function current(id, src, version) {
    const el = gm.els.get(id);
    if (!el || !el.isConnected || +el.dataset.gmId !== id) return null;
    if (el.tagName === "CANVAS") return el.__gmDrawn === version ? el : null;
    const k = gm.keys.get(id);
    if (!k || k.key !== src) return null;
    if (el.src !== k.srcAttr) return null;                       // site switched to another page
    if (el.complete && el.currentSrc && el.currentSrc !== src) return null;
    return el;
  }

  const toDataURL = blob => new Promise((ok, bad) => { const f = new FileReader(); f.onload = () => ok(f.result); f.onerror = bad; f.readAsDataURL(blob); });

  gm.grab = async (token, id, src, version, allowFetch) => {
    if (token !== gm.token) return { err: "gone" };
    const el = current(id, src, version);
    if (!el) return { err: "gone" };
    if (el.tagName === "CANVAS") {
      try { return { data: el.toDataURL("image/png") }; } catch (e) { return { err: "canvas: " + e.name }; }
    }
    let err = "";
    if (allowFetch || src.startsWith("blob:") || src.startsWith("data:")) {
      try {
        const same = src.startsWith("blob:") || src.startsWith("data:") || new URL(src, location.href).origin === location.origin;
        const res = await fetch(src, { credentials: same ? "include" : "omit", mode: same ? "same-origin" : "cors", cache: "force-cache" });
        if (res.ok) return { data: await toDataURL(await res.blob()) };
        err = "http " + res.status;
      } catch (e) { err = "fetch: " + e.name; }
    }
    try {   // re-encode through a canvas (same-origin / CORS-enabled images)
      const c = document.createElement("canvas");
      c.width = el.naturalWidth; c.height = el.naturalHeight;
      c.getContext("2d").drawImage(el, 0, 0);
      return { data: c.toDataURL("image/png"), reencoded: true };
    } catch (e) { return { err: (err ? err + " | " : "") + "canvas: " + e.name }; }
  };

  gm.rect = (token, id, src, version) => {
    if (token !== gm.token) return null;
    const el = current(id, src, version);
    if (!el) return null;
    const r = viewRect(el);
    return { x: r.left + scrollX, y: r.top + scrollY, w: r.width, h: r.height,
             nw: el.naturalWidth || el.width, nh: el.naturalHeight || el.height, dpr: devicePixelRatio };
  };

  function b64ToBlob(b64, mime) {
    const bin = atob(b64), u8 = new Uint8Array(bin.length);
    for (let i = 0; i < bin.length; i++) u8[i] = bin.charCodeAt(i);
    return new Blob([u8], { type: mime });
  }

  async function drawCanvas(cv, bmp) {
    const ctx = cv.getContext("2d");
    gm.selfDraw = true;
    try { ctx.drawImage(bmp, 0, 0, cv.width, cv.height); } finally { gm.selfDraw = false; }
  }

  function setDone(key, url) {
    const old = gm.done.get(key);
    gm.done.set(key, url);
    if (old && old !== url) {
      let used = false;
      for (const el of gm.els.values()) if (el.dataset && el.dataset.gmUrl === old && el.isConnected) { used = true; break; }
      if (!used) URL.revokeObjectURL(old);
    }
  }

  gm.apply = async (token, id, src, version, b64, mime, origB64) => {
    if (token !== gm.token) return "gone";
    const el = current(id, src, version);
    if (el && el.tagName === "CANVAS") {
      const tr = await createImageBitmap(b64ToBlob(b64, mime));
      const orig = origB64 ? await createImageBitmap(b64ToBlob(origB64, "image/png")) : null;
      if (el.__gmDrawn !== version) return "stale";              // site drew a newer page meanwhile
      const prev = gm.canv.get(id);
      if (prev) { try { prev.tr.close(); prev.orig && prev.orig.close(); } catch (e) {} }
      gm.canv.set(id, { tr, orig, version });
      if (!gm.showOrig) await drawCanvas(el, tr);
      return "ok";
    }
    const url = URL.createObjectURL(b64ToBlob(b64, mime));
    setDone(src, url);                                           // reusable if that page comes back
    gm.pendingKeys.delete(src);
    if (!el) {
      // the element moved on; the translation is kept and shown whenever that page comes back
      return "stored";
    }
    markDone(el, src, url);
    return "ok";
  };

  // a job failed: let other <img>s that show the same source be queued on their own
  gm.release = (token, keys) => {
    if (token !== gm.token) return false;
    for (const k of keys) gm.pendingKeys.delete(k);
    return true;
  };

  gm.forget = (token, ids) => {
    if (token !== gm.token) return false;
    for (const id of ids) { const el = gm.els.get(id); if (el && el.tagName !== "CANVAS") forgetEl(el); }
    return true;
  };

  gm.toggle = async (force) => {
    gm.showOrig = typeof force === "boolean" ? force : !gm.showOrig;
    const imgs = collect(document, "img", [], 0, true);
    for (const el of imgs) if (el.dataset.gmState === "done") show(el);
    for (const [id, c] of gm.canv) {
      const el = gm.els.get(id);
      if (!el || !el.isConnected || el.__gmDrawn !== c.version) continue;
      const bmp = gm.showOrig ? c.orig : c.tr;
      if (bmp) await drawCanvas(el, bmp);
    }
    return gm.showOrig;
  };

  // status chip
  gm.setStatus = (text) => {
    if (!document.documentElement) return;
    if (!gm.chip) {
      const c = gm.chip = document.createElement("div");
      c.style.cssText = "position:fixed;right:10px;bottom:10px;z-index:2147483647;pointer-events:none;" +
        "background:rgba(20,24,33,.82);color:#fff;font:12px/1.4 'Leelawadee UI',Tahoma,sans-serif;" +
        "padding:4px 9px;border-radius:12px;box-shadow:0 1px 4px rgba(0,0,0,.4);max-width:60vw";
    }
    if (!gm.chip.isConnected) document.documentElement.appendChild(gm.chip);
    gm.chip.textContent = text;
    gm.chip.style.display = text ? "block" : "none";
  };
  gm.hideChip = (h) => { if (gm.chip) gm.chip.style.visibility = h ? "hidden" : "visible"; };

  addEventListener("keydown", e => {
    if (e.altKey && !e.ctrlKey && e.code === "KeyT") { e.preventDefault(); gm.toggle(); }
  }, true);

  // keep our translation when lazy-loaders / frameworks swap sources back
  function onMutation(ms) {
    for (const m of ms) {
      let t = m.target;
      if (!t || !t.tagName) continue;
      if (t.tagName === "SOURCE") {
        const img = t.parentElement && t.parentElement.tagName === "PICTURE" ? t.parentElement.querySelector("img") : null;
        if (img && img.dataset.gmState === "done" && t.hasAttribute("srcset")) strip(img, img.dataset.gmOrig, img.dataset.gmUrl);
        continue;
      }
      if (t.tagName !== "IMG" || t.dataset.gmState !== "done") continue;
      const tr = t.dataset.gmUrl, orig = t.dataset.gmOrig;
      if (m.attributeName === "srcset" && t.hasAttribute("srcset")) { strip(t, orig, tr); show(t); continue; }
      const now = t.getAttribute("src") || "";
      if (!now || abs(now) === tr || abs(now) === orig && gm.showOrig) continue;
      if (abs(now) === orig) { show(t); continue; }               // lazy-loader put the original back
      if (gm.done.has(abs(now))) { markDone(t, abs(now), gm.done.get(abs(now))); continue; }
      forgetEl(t);                                                 // a genuinely new page in a recycled <img>
    }
  }
  try {
    new MutationObserver(onMutation).observe(document, { subtree: true, attributes: true, attributeFilter: ["src", "srcset"] });
  } catch (e) { note(gm.errors, "observer: " + e); }
})();
