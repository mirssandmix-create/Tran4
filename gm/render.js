// Ghost Manga 5 typesetter. Defines window.__gmRender in a headless Chrome page;
// Python calls it through CDP Runtime.evaluate(awaitPromise=true).
// Chrome gives us correct Thai shaping (stacked vowels/tone marks) and
// dictionary-based word breaking through Intl.Segmenter.
//
// blocks: [{text, box:[x0,y0,x1,y1], fg:"#000", stroke:"#fff", strokeRatio, maxPx}] in image pixels
// opts:   {lang, font, bold, minPx, maxPx, lineHeight, format, quality}
window.__gmRender = async (imageDataUrl, blocks, opts) => { try {
  const img = new Image();
  img.decoding = "sync";
  await new Promise((ok, bad) => { img.onload = ok; img.onerror = () => bad(new Error("decode failed")); img.src = imageDataUrl; });
  const W = img.naturalWidth, H = img.naturalHeight;
  const canvas = document.createElement("canvas");
  canvas.width = W; canvas.height = H;
  const ctx = canvas.getContext("2d");
  ctx.drawImage(img, 0, 0);

  const lang = opts.lang || "th";
  const weight = opts.bold ? "700" : "400";
  const family = `"${opts.font || "Leelawadee UI"}", "Leelawadee UI", "Tahoma", "Noto Sans Thai", sans-serif`;
  try { await document.fonts.load(`${weight} 20px ${family}`); } catch (e) {}
  const lineHeight = opts.lineHeight || 1.18;
  const noSpaceLang = /^(th|ja|zh|ko|lo|km|my)/i.test(lang);

  let wordSeg = null, graphemeSeg = null;
  try { wordSeg = new Intl.Segmenter(lang, { granularity: "word" }); } catch (e) {}
  try { graphemeSeg = new Intl.Segmenter(lang, { granularity: "grapheme" }); } catch (e) {}

  const graphemes = (s) => graphemeSeg ? [...graphemeSeg.segment(s)].map(x => x.segment) : [...s];

  // Break text into wrap units. Spaces stay attached to the preceding unit so
  // measuring "unit + next" is exact; Thai/CJK get dictionary word units.
  function units(text) {
    const out = [];
    const paras = text.replace(/\r/g, "").split("\n");
    paras.forEach((p, pi) => {
      if (pi > 0) out.push("\n");
      if (wordSeg) {
        for (const s of wordSeg.segment(p)) {
          if (/^\s+$/.test(s.segment) && out.length && out[out.length - 1] !== "\n") out[out.length - 1] += s.segment;
          else out.push(s.segment);
        }
      } else {
        p.split(/(\s+)/).forEach(t => { if (!t) return; if (/^\s+$/.test(t) && out.length) out[out.length - 1] += t; else out.push(t); });
      }
    });
    return out;
  }

  // Closing punctuation must not start a line; opening must not end one.
  const NO_START = /^[)\]}」』】〉》、。，．！？!?,.:;…ๆฯ~\-]/;

  function layout(text, px, maxW) {
    ctx.font = `${weight} ${px}px ${family}`;
    const us = units(text);
    const lines = [];
    let cur = "";
    let broke = false;
    const width = (s) => ctx.measureText(s.trimEnd()).width;
    const pushLong = (u) => { // a single unit wider than the box: split by grapheme
      broke = true;
      let piece = "";
      for (const g of graphemes(u)) {
        if (piece && width(piece + g) > maxW) { lines.push(piece); piece = g; }
        else piece += g;
      }
      return piece;
    };
    for (let i = 0; i < us.length; i++) {
      const u = us[i];
      if (u === "\n") { lines.push(cur); cur = ""; continue; }
      if (!cur) { cur = width(u) > maxW ? pushLong(u) : u; continue; }
      if (width(cur + u) <= maxW || NO_START.test(u)) { cur += u; continue; }
      lines.push(cur);
      cur = width(u) > maxW ? pushLong(u) : u;
    }
    if (cur) lines.push(cur);
    const cleaned = lines.map(l => l.trim()).filter((l, i, a) => l.length || (i > 0 && i < a.length - 1));
    let widest = 0;
    for (const l of cleaned) widest = Math.max(widest, width(l));
    return { lines: cleaned, widest, height: cleaned.length * px * lineHeight, broke };
  }

  function fit(text, w, h, blockMax) {
    const minPx = Math.max(6, opts.minPx || 11);
    const maxPx = Math.max(minPx, Math.min(blockMax || opts.maxPx || 64, h));
    // First try without splitting words; only allow mid-word breaks if nothing fits.
    for (const allowBreak of [false, true]) {
      let lo = minPx, hi = maxPx, best = null;
      while (lo <= hi) {
        const mid = Math.floor((lo + hi) / 2);
        const L = layout(text, mid, w);
        if (L.height <= h && L.widest <= w && (allowBreak || !L.broke)) { best = { px: mid, L }; lo = mid + 1; }
        else hi = mid - 1;
      }
      if (best) return best;
    }
    return { px: minPx, L: layout(text, minPx, w) }; // overflow: smallest size, centered
  }

  const results = [];
  for (const b of blocks) {
    const text = (b.text || "").trim();
    if (!text) { results.push(null); continue; }
    let [x0, y0, x1, y1] = b.box;
    // inner padding so glyphs never touch the bubble outline
    const pad = Math.max(2, Math.round(Math.min(x1 - x0, y1 - y0) * 0.06));
    x0 += pad; y0 += pad; x1 -= pad; y1 -= pad;
    const w = Math.max(10, x1 - x0), h = Math.max(10, y1 - y0);
    const { px, L } = fit(text, w, h, b.maxPx);
    ctx.font = `${weight} ${px}px ${family}`;
    ctx.textAlign = "center";
    ctx.textBaseline = "middle";
    ctx.lineJoin = "round";
    ctx.miterLimit = 2;
    const cx = (x0 + x1) / 2;
    const lh = px * lineHeight;
    const top = (y0 + y1) / 2 - (L.lines.length * lh) / 2 + lh / 2;
    const strokeW = Math.max(2, Math.round(px * (b.strokeRatio || 0.16)));
    L.lines.forEach((line, i) => {
      const y = top + i * lh;
      if (b.stroke) { ctx.strokeStyle = b.stroke; ctx.lineWidth = strokeW; ctx.strokeText(line, cx, y); }
      ctx.fillStyle = b.fg || "#000";
      ctx.fillText(line, cx, y);
    });
    results.push({ px, lines: L.lines.length, overflow: L.height > h || L.widest > w });
  }
  const fmt = opts.format || "image/jpeg";
  return { ok: true, dataUrl: canvas.toDataURL(fmt, opts.quality || 0.92), blocks: results };
} catch (e) { return { ok: false, error: String(e && e.message || e) }; } };
true;
