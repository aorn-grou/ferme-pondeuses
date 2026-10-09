/* Petits graphiques intégrés au logiciel (SVG, sans bibliothèque externe : marchent même hors connexion).
   FermeChart.barsLine(conteneur, {labels, bars, line, now, unit}) : barres (passé) + ligne pointillée (prévision). */
(function () {
  const NS = "http://www.w3.org/2000/svg";
  const el = (tag, attrs, parent) => {
    const n = document.createElementNS(NS, tag);
    Object.entries(attrs || {}).forEach(([k, v]) => n.setAttribute(k, v));
    if (parent) parent.appendChild(n);
    return n;
  };
  const fmt = (v) => (Math.round(v * 10) / 10).toLocaleString("fr-FR");
  const css = (name, fallback) => getComputedStyle(document.documentElement).getPropertyValue(name).trim() || fallback;
  function niceMax(v) {
    if (v <= 0) return 1;
    const p = Math.pow(10, Math.floor(Math.log10(v)));
    for (const m of [1, 2, 2.5, 5, 10]) if (m * p >= v) return m * p;
    return 10 * p;
  }

  function barsLine(box, data) {
    box.innerHTML = "";
    const W = Math.max(320, box.clientWidth || 600), H = box.clientHeight || 260;
    const m = { l: 52, r: 12, t: 22, b: 28 };
    const n = data.labels.length;
    if (!n) return;
    const vals = data.bars.concat(data.line).filter((v) => v != null);
    const max = niceMax(Math.max(1, ...vals));
    const iw = W - m.l - m.r, ih = H - m.t - m.b, step = iw / n;
    const x = (i) => m.l + step * i + step / 2, y = (v) => m.t + ih - (v / max) * ih;
    const svg = el("svg", { viewBox: `0 0 ${W} ${H}`, width: "100%", height: H, role: "img" }, box);
    const grid = css("--border", "#e8e2cf"), muted = css("--text-2", "#5d6a5f");
    const green = data.barColor || "#3a8a2e", gold = data.lineColor || css("--gold", "#c9971c");
    for (let k = 0; k <= 4; k++) {
      const v = (max / 4) * k, yy = y(v);
      el("line", { x1: m.l, x2: W - m.r, y1: yy, y2: yy, stroke: grid, "stroke-width": 1 }, svg);
      const t = el("text", { x: m.l - 6, y: yy + 4, "text-anchor": "end", "font-size": 11, fill: muted }, svg);
      t.textContent = fmt(v) + (data.unit ? " " + data.unit : "");
    }
    const every = Math.ceil(n / Math.max(3, Math.floor(iw / 48)));
    data.labels.forEach((lab, i) => {
      if (i % every) return;
      const t = el("text", { x: x(i), y: H - 8, "text-anchor": "middle", "font-size": 11, fill: muted }, svg);
      t.textContent = lab;
    });
    const bw = Math.max(3, Math.min(16, step * 0.6));
    data.bars.forEach((v, i) => {
      if (v == null || v <= 0) return;
      el("rect", { x: x(i) - bw / 2, y: y(v), width: bw, height: Math.max(1, m.t + ih - y(v)), rx: 3, fill: green }, svg);
    });
    const pts = data.line.map((v, i) => (v == null ? null : [x(i), y(v)])).filter(Boolean);
    if (pts.length > 1) {
      const d = pts.map((p, i) => (i ? "L" : "M") + p[0].toFixed(1) + " " + p[1].toFixed(1)).join(" ");
      el("path", { d: d + ` L${pts[pts.length - 1][0]} ${m.t + ih} L${pts[0][0]} ${m.t + ih} Z`, fill: gold, "fill-opacity": 0.12 }, svg);
      el("path", { d, fill: "none", stroke: gold, "stroke-width": 2, "stroke-dasharray": "6 4", "stroke-linejoin": "round" }, svg);
    }
    drawMarks(svg, data.marks, data.labels, x, m.t, ih);
    const nowIdx = data.labels.indexOf(data.now);
    if (nowIdx >= 0) {
      el("line", { x1: x(nowIdx), x2: x(nowIdx), y1: m.t, y2: m.t + ih, stroke: gold, "stroke-dasharray": "4 4" }, svg);
      const t = el("text", { x: x(nowIdx), y: m.t - 6, "text-anchor": "middle", "font-size": 11, "font-weight": 700, fill: gold }, svg);
      t.textContent = data.nowLabel || "Aujourd'hui";
    }
    // info-bulle au survol / au toucher
    const tip = document.createElement("div");
    tip.className = "chart-tip";
    tip.hidden = true;
    box.style.position = "relative";
    box.appendChild(tip);
    const hit = el("rect", { x: m.l, y: m.t, width: iw, height: ih, fill: "transparent" }, svg);
    const show = (evt) => {
      const r = svg.getBoundingClientRect();
      const px = ((evt.touches ? evt.touches[0].clientX : evt.clientX) - r.left) * (W / r.width);
      const i = Math.max(0, Math.min(n - 1, Math.floor((px - m.l) / step)));
      const parts = [`<strong>${data.labels[i]}</strong>`];
      if (data.bars[i] != null) parts.push(`${data.barLabel || "Réel"} : ${fmt(data.bars[i])} ${data.unit || ""}`);
      if (data.line[i] != null) parts.push(`${data.lineLabel || "Prévision"} : ${fmt(data.line[i])} ${data.unit || ""}`);
      if (data.tips && data.tips[i]) parts.push(data.tips[i]);
      tip.innerHTML = parts.join("<br>");
      tip.hidden = false;
      const left = (x(i) / W) * r.width;
      tip.style.left = Math.min(r.width - 190, Math.max(0, left - 70)) + "px";
      tip.style.top = "0px";
    };
    hit.addEventListener("mousemove", show);
    hit.addEventListener("touchstart", show, { passive: true });
    hit.addEventListener("mouseleave", () => { tip.hidden = true; });
  }

  /* Repères verticaux (ex. « 🥚 début de ponte ») : [{at: "S18", text: "🥚 Ponte"}] */
  function drawMarks(svg, marks, labels, x, top, ih) {
    (marks || []).forEach((mk, k) => {
      const i = labels.indexOf(mk.at);
      if (i < 0) return;
      const c = mk.color || "#b7791f";
      el("line", { x1: x(i), x2: x(i), y1: top, y2: top + ih, stroke: c, "stroke-width": 2, "stroke-dasharray": "2 3" }, svg);
      const t = el("text", { x: x(i) + 4, y: top + 12 + (k % 3) * 13, "font-size": 11, "font-weight": 700, fill: c }, svg);
      t.textContent = mk.text;
    });
  }

  const PALETTE = ["#2e7d32", "#c9971c", "#1f6fa8", "#a33b8f", "#c0352b", "#0f8a80", "#6b4fbb", "#7a5c2e"];

  /* Plusieurs courbes (ex. taux de ponte de chaque lot), avec pic ▲ et creux ▼ de chacune */
  function multi(box, data) {
    box.innerHTML = "";
    const W = Math.max(320, box.clientWidth || 600), H = box.clientHeight || 260;
    const m = { l: 46, r: 14, t: 22, b: 26 }, n = data.labels.length;
    if (!n || !data.series.length) return;
    const all = data.series.flatMap((s) => s.values.filter((v) => v != null));
    const max = data.max || niceMax(Math.max(1, ...all)), iw = W - m.l - m.r, ih = H - m.t - m.b;
    const x = (i) => m.l + (n === 1 ? iw / 2 : (iw * i) / (n - 1)), y = (v) => m.t + ih - (v / max) * ih;
    const svg = el("svg", { viewBox: `0 0 ${W} ${H}`, width: "100%", height: H, role: "img" }, box);
    const grid = css("--border", "#e8e2cf"), muted = css("--text-2", "#5d6a5f");
    for (let k = 0; k <= 4; k++) {
      const v = (max / 4) * k, yy = y(v);
      el("line", { x1: m.l, x2: W - m.r, y1: yy, y2: yy, stroke: grid }, svg);
      el("text", { x: m.l - 6, y: yy + 4, "text-anchor": "end", "font-size": 11, fill: muted }, svg).textContent = fmt(v) + (data.unit === "%" ? " %" : "");
    }
    const every = Math.ceil(n / Math.max(3, Math.floor(iw / 62)));
    data.labels.forEach((lab, i) => { if ((i % every === 0 && n - 1 - i >= every / 2) || i === n - 1) el("text", { x: x(i), y: H - 6, "text-anchor": "middle", "font-size": 11, fill: muted }, svg).textContent = lab; });
    drawMarks(svg, data.marks, data.labels, x, m.t, ih);
    data.series.forEach((s, k) => {
      const col = s.color || PALETTE[k % PALETTE.length];
      s._col = col;
      let d = "", pen = false;
      s.values.forEach((v, i) => {
        if (v == null) { pen = false; return; }
        d += (pen ? "L" : "M") + x(i).toFixed(1) + " " + y(v).toFixed(1) + " ";
        pen = true;
      });
      el("path", { d, fill: "none", stroke: col, "stroke-width": 2.5, "stroke-linejoin": "round", "stroke-linecap": "round" }, svg);
      const vals = s.values.map((v, i) => [v, i]).filter((p) => p[0] != null);
      if (vals.length > 1 && data.peaks !== false) {
        const hi = vals.reduce((a, b) => (b[0] > a[0] ? b : a)), lo = vals.reduce((a, b) => (b[0] < a[0] ? b : a));
        [[hi, -9, "▲"], [lo, 17, "▼"]].forEach(([p, o, sym]) => {
          if (sym === "▼" && lo[0] === hi[0]) return;
          el("circle", { cx: x(p[1]), cy: y(p[0]), r: 4, fill: col, stroke: css("--surface", "#fff"), "stroke-width": 2 }, svg);
          el("text", { x: Math.min(W - 24, Math.max(m.l + 16, x(p[1]))), y: y(p[0]) + o, "text-anchor": "middle", "font-size": 10.5, "font-weight": 700, fill: col }, svg).textContent = sym + " " + fmt(p[0]);
        });
      }
    });
    tooltip(box, svg, W, n, (px) => Math.max(0, Math.min(n - 1, Math.round((px - m.l) / (iw / Math.max(1, n - 1))))),
      (i) => `<strong>${data.labels[i]}</strong>` + data.series.map((s) => s.values[i] == null ? "" :
        `<br><span style="color:${s._col}">●</span> ${s.name} : ${fmt(s.values[i])} ${data.unit || ""}${s.extra && s.extra[i] ? " · " + s.extra[i] : ""}`).join(""), x);
  }

  /* Barres (ex. œufs par jour) + courbe sur un 2e axe à droite (ex. taux de ponte %) */
  function combo(box, data) {
    box.innerHTML = "";
    const W = Math.max(320, box.clientWidth || 600), H = box.clientHeight || 260;
    const m = { l: 50, r: 46, t: 22, b: 26 }, n = data.labels.length;
    if (!n) return;
    const bars = data.bars.map((v) => v || 0), line = data.line;
    const maxB = niceMax(Math.max(1, ...bars)), maxL = data.lineMax || niceMax(Math.max(1, ...line.filter((v) => v != null)));
    const iw = W - m.l - m.r, ih = H - m.t - m.b, step = iw / n;
    const x = (i) => m.l + step * i + step / 2, yB = (v) => m.t + ih - (v / maxB) * ih, yL = (v) => m.t + ih - (v / maxL) * ih;
    const svg = el("svg", { viewBox: `0 0 ${W} ${H}`, width: "100%", height: H, role: "img" }, box);
    const grid = css("--border", "#e8e2cf"), muted = css("--text-2", "#5d6a5f");
    const cB = data.barColor || "#e0b64a", cL = data.lineColor || "#2e7d32";
    for (let k = 0; k <= 4; k++) {
      const yy = m.t + ih - (ih / 4) * k;
      el("line", { x1: m.l, x2: W - m.r, y1: yy, y2: yy, stroke: grid }, svg);
      el("text", { x: m.l - 6, y: yy + 4, "text-anchor": "end", "font-size": 11, fill: muted }, svg).textContent = fmt((maxB / 4) * k);
      el("text", { x: W - m.r + 6, y: yy + 4, "font-size": 11, fill: cL }, svg).textContent = fmt((maxL / 4) * k) + (data.lineUnit === "%" ? " %" : "");
    }
    const every = Math.ceil(n / Math.max(3, Math.floor(iw / 54)));
    data.labels.forEach((lab, i) => { if ((i % every === 0 && n - 1 - i >= every / 2) || i === n - 1) el("text", { x: x(i), y: H - 6, "text-anchor": "middle", "font-size": 11, fill: muted }, svg).textContent = lab; });
    const bw = Math.max(2, Math.min(18, step * 0.62));
    bars.forEach((v, i) => { if (v > 0) el("rect", { x: x(i) - bw / 2, y: yB(v), width: bw, height: Math.max(1, m.t + ih - yB(v)), rx: 2, fill: cB }, svg); });
    drawMarks(svg, data.marks, data.labels, x, m.t, ih);
    let d = "", pen = false;
    line.forEach((v, i) => { if (v == null) { pen = false; return; } d += (pen ? "L" : "M") + x(i).toFixed(1) + " " + yL(v).toFixed(1) + " "; pen = true; });
    el("path", { d, fill: "none", stroke: cL, "stroke-width": 2.5, "stroke-linejoin": "round" }, svg);
    const pts = line.map((v, i) => [v, i]).filter((p) => p[0] != null);
    if (pts.length > 1) {
      const hi = pts.reduce((a, b) => (b[0] > a[0] ? b : a)), lo = pts.reduce((a, b) => (b[0] < a[0] ? b : a));
      [[hi, -9, "#2e7d32", "▲"], [lo, 17, "#c0352b", "▼"]].forEach(([p, o, c, sym]) => {
        if (sym === "▼" && lo[0] === hi[0]) return;
        el("circle", { cx: x(p[1]), cy: yL(p[0]), r: 5, fill: c, stroke: css("--surface", "#fff"), "stroke-width": 2 }, svg);
        el("text", { x: Math.min(W - 30, Math.max(m.l + 20, x(p[1]))), y: yL(p[0]) + o, "text-anchor": "middle", "font-size": 11, "font-weight": 700, fill: c }, svg).textContent = sym + " " + fmt(p[0]) + (data.lineUnit === "%" ? " %" : "");
      });
      const avg = pts.reduce((a, p) => a + p[0], 0) / pts.length;
      el("line", { x1: m.l, x2: W - m.r, y1: yL(avg), y2: yL(avg), stroke: cL, "stroke-dasharray": "6 4", "stroke-opacity": 0.6 }, svg);
    }
    tooltip(box, svg, W, n, (px) => Math.max(0, Math.min(n - 1, Math.floor((px - m.l) / step))),
      (i) => `<strong>${data.labels[i]}</strong><br>${data.barLabel || ""} : ${fmt(bars[i])} ${data.unit || ""}` +
        (line[i] != null ? `<br>${data.lineLabel || ""} : ${fmt(line[i])} ${data.lineUnit || ""}` : "") + (data.tips && data.tips[i] ? "<br>" + data.tips[i] : ""), x);
  }

  /* Courbe avec moyenne (pointillés), pic ▲ et creux ▼ */
  function line(box, data) {
    box.innerHTML = "";
    const W = Math.max(320, box.clientWidth || 600), H = box.clientHeight || 240;
    const m = { l: 48, r: 14, t: 24, b: 26 }, n = data.labels.length;
    if (!n) return;
    const vals = data.values.map((v) => v || 0);
    const max = niceMax(Math.max(1, ...vals)), iw = W - m.l - m.r, ih = H - m.t - m.b;
    const x = (i) => m.l + (n === 1 ? iw / 2 : (iw * i) / (n - 1)), y = (v) => m.t + ih - (v / max) * ih;
    const svg = el("svg", { viewBox: `0 0 ${W} ${H}`, width: "100%", height: H, role: "img" }, box);
    const grid = css("--border", "#e8e2cf"), muted = css("--text-2", "#5d6a5f");
    const col = data.color || "#3a8a2e", gold = css("--gold", "#c9971c");
    for (let k = 0; k <= 4; k++) {
      const v = (max / 4) * k, yy = y(v);
      el("line", { x1: m.l, x2: W - m.r, y1: yy, y2: yy, stroke: grid }, svg);
      el("text", { x: m.l - 6, y: yy + 4, "text-anchor": "end", "font-size": 11, fill: muted }, svg).textContent = fmt(v);
    }
    const every = Math.ceil(n / Math.max(3, Math.floor(iw / 62)));
    data.labels.forEach((lab, i) => { if ((i % every === 0 && (n - 1 - i >= every / 2 || i === n - 1)) || i === n - 1) el("text", { x: x(i), y: H - 6, "text-anchor": "middle", "font-size": 11, fill: muted }, svg).textContent = lab; });
    const pts = vals.map((v, i) => [x(i), y(v)]);
    const d = pts.map((p, i) => (i ? "L" : "M") + p[0].toFixed(1) + " " + p[1].toFixed(1)).join(" ");
    el("path", { d: d + ` L${pts[n - 1][0]} ${m.t + ih} L${pts[0][0]} ${m.t + ih} Z`, fill: col, "fill-opacity": 0.13 }, svg);
    el("path", { d, fill: "none", stroke: col, "stroke-width": 2.5, "stroke-linejoin": "round", "stroke-linecap": "round" }, svg);
    const nonZero = vals.filter((v) => v > 0);
    if (nonZero.length) {
      const avg = vals.reduce((a, b) => a + b, 0) / n;
      el("line", { x1: m.l, x2: W - m.r, y1: y(avg), y2: y(avg), stroke: gold, "stroke-dasharray": "6 4", "stroke-width": 1.5 }, svg);
      const hi = vals.indexOf(Math.max(...vals));
      const loVal = Math.min(...nonZero), lo = vals.indexOf(loVal);
      const marks = [[hi, "#2e7d32", -10, "▲ " + fmt(vals[hi])]];
      if (nonZero.length > 1 && loVal < vals[hi]) marks.push([lo, "#c0352b", 18, "▼ " + fmt(loVal)]);
      marks.forEach(([i, c, o, t]) => {
        el("circle", { cx: x(i), cy: y(vals[i]), r: 5, fill: c, stroke: css("--surface", "#fff"), "stroke-width": 2 }, svg);
        el("text", { x: Math.min(W - 30, Math.max(m.l + 20, x(i))), y: y(vals[i]) + o, "text-anchor": "middle", "font-size": 11, "font-weight": 700, fill: c }, svg).textContent = t;
      });
    }
    tooltip(box, svg, W, n, (px) => Math.max(0, Math.min(n - 1, Math.round((px - m.l) / (iw / Math.max(1, n - 1))))),
      (i) => `<strong>${data.labels[i]}</strong><br>${fmt(vals[i])} ${data.unit || ""}`, x);
  }

  function tooltip(box, svg, W, n, indexAt, html, xOf) {
    const tip = document.createElement("div");
    tip.className = "chart-tip"; tip.hidden = true;
    box.style.position = "relative"; box.appendChild(tip);
    const move = (evt) => {
      const r = svg.getBoundingClientRect();
      const px = ((evt.touches ? evt.touches[0].clientX : evt.clientX) - r.left) * (W / r.width);
      const i = indexAt(px);
      tip.innerHTML = html(i); tip.hidden = false;
      tip.style.left = Math.min(r.width - 190, Math.max(0, (xOf(i) / W) * r.width - 70)) + "px"; tip.style.top = "0px";
    };
    svg.addEventListener("mousemove", move);
    svg.addEventListener("touchstart", move, { passive: true });
    svg.addEventListener("mouseleave", () => { tip.hidden = true; });
  }

  /* Mini-courbe pour les cartes */
  function spark(box, data) {
    box.innerHTML = "";
    const vals = data.values.map((v) => v || 0), n = vals.length;
    if (n < 2) return;
    const W = 160, H = 34, max = Math.max(...vals), min = Math.min(...vals), span = max - min || 1;
    const x = (i) => (W * i) / (n - 1), y = (v) => H - 3 - ((v - min) / span) * (H - 6);
    const svg = el("svg", { viewBox: `0 0 ${W} ${H}`, width: "100%", height: H, preserveAspectRatio: "none" }, box);
    const d = vals.map((v, i) => (i ? "L" : "M") + x(i).toFixed(1) + " " + y(v).toFixed(1)).join(" ");
    const col = data.color || "#3a8a2e";
    el("path", { d: d + ` L${W} ${H} L0 ${H} Z`, fill: col, "fill-opacity": 0.12 }, svg);
    el("path", { d, fill: "none", stroke: col, "stroke-width": 2, "vector-effect": "non-scaling-stroke" }, svg);
  }

  window.FermeChart = { barsLine, line, spark, multi, combo };
  const kinds = { barsLine, line, spark, multi, combo };
  document.querySelectorAll("[data-chart]").forEach((box) => {
    const data = JSON.parse(box.dataset.chart);
    const fn = kinds[box.dataset.chartType || "barsLine"] || barsLine;
    const draw = () => fn(box, data);
    draw();
    let t;
    window.addEventListener("resize", () => { clearTimeout(t); t = setTimeout(draw, 150); });
  });
})();
