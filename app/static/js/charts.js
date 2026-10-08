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
    const every = Math.ceil(n / 14);
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

  window.FermeChart = { barsLine };
  document.querySelectorAll("[data-chart]").forEach((box) => {
    const data = JSON.parse(box.dataset.chart);
    const draw = () => barsLine(box, data);
    draw();
    let t;
    window.addEventListener("resize", () => { clearTimeout(t); t = setTimeout(draw, 150); });
  });
})();
