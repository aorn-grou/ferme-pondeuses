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
    const nowIdx = data.labels.indexOf(data.now);
    if (nowIdx >= 0) {
      el("line", { x1: x(nowIdx), x2: x(nowIdx), y1: m.t, y2: m.t + ih, stroke: gold, "stroke-dasharray": "4 4" }, svg);
      const t = el("text", { x: x(nowIdx), y: m.t - 6, "text-anchor": "middle", "font-size": 11, "font-weight": 700, fill: gold }, svg);
      t.textContent = "Aujourd'hui";
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
      tip.innerHTML = parts.join("<br>");
      tip.hidden = false;
      const left = (x(i) / W) * r.width;
      tip.style.left = Math.min(r.width - 150, Math.max(0, left - 70)) + "px";
      tip.style.top = "0px";
    };
    hit.addEventListener("mousemove", show);
    hit.addEventListener("touchstart", show, { passive: true });
    hit.addEventListener("mouseleave", () => { tip.hidden = true; });
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
      tip.style.left = Math.min(r.width - 150, Math.max(0, (xOf(i) / W) * r.width - 70)) + "px"; tip.style.top = "0px";
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

  window.FermeChart = { barsLine, line, spark };
  const kinds = { barsLine, line, spark };
  document.querySelectorAll("[data-chart]").forEach((box) => {
    const data = JSON.parse(box.dataset.chart);
    const fn = kinds[box.dataset.chartType || "barsLine"] || barsLine;
    const draw = () => fn(box, data);
    draw();
    let t;
    window.addEventListener("resize", () => { clearTimeout(t); t = setTimeout(draw, 150); });
  });
})();
