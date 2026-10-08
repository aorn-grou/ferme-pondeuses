/* Logiciel ferme pondeuses — comportements de l'interface (sans bibliothèque externe). */
(function () {
  "use strict";
  const $ = (sel, root = document) => root.querySelector(sel);
  const $$ = (sel, root = document) => Array.from(root.querySelectorAll(sel));
  const store = {
    get(key) { try { return localStorage.getItem(key); } catch (e) { return null; } },
    set(key, value) { try { localStorage.setItem(key, value); } catch (e) { /* stockage indisponible */ } },
    remove(key) { try { localStorage.removeItem(key); } catch (e) { /* rien */ } },
  };

  /* ---------- Menu sur téléphone ---------- */
  const app = $("[data-app]");
  $$("[data-menu-open]").forEach((btn) => btn.addEventListener("click", () => app && app.classList.add("menu-open")));
  $$("[data-menu-close]").forEach((el) => el.addEventListener("click", () => app && app.classList.remove("menu-open")));
  document.addEventListener("keydown", (e) => {
    if (e.key === "Escape" && app) app.classList.remove("menu-open");
  });

  /* Fermer le menu utilisateur en cliquant ailleurs */
  document.addEventListener("click", (e) => {
    $$("details.user-menu[open], details.inline-confirm[open]").forEach((d) => {
      if (!d.contains(e.target)) d.removeAttribute("open");
    });
  });

  /* ---------- Thème : automatique → clair → sombre ---------- */
  const root = document.documentElement;
  if (!window.THEME_URL) {
    const saved = store.get("ferme-theme");
    if (saved) root.dataset.theme = saved;
  }
  $$("[data-theme-toggle]").forEach((btn) => btn.addEventListener("click", () => {
    const order = ["auto", "light", "dark"];
    const next = order[(order.indexOf(root.dataset.theme) + 1) % order.length];
    root.dataset.theme = next;
    store.set("ferme-theme", next);
    if (window.THEME_URL) {
      fetch(window.THEME_URL, {
        method: "POST",
        headers: { "Content-Type": "application/json", "X-CSRF-Token": window.CSRF_TOKEN },
        body: JSON.stringify({ theme: next }),
      }).catch(() => {});
    }
  }));

  /* ---------- Messages ---------- */
  $$("[data-dismiss]").forEach((btn) => btn.addEventListener("click", () => btn.closest(".alert").remove()));
  $$(".alert-success").forEach((el) => {
    if (!el.closest(".flashes")) return;
    if (el.textContent.length < 120) setTimeout(() => el.remove(), 6000);
  });

  /* ---------- Confirmation avant une action importante ---------- */
  $$("form[data-confirm]").forEach((form) => form.addEventListener("submit", (e) => {
    if (!window.confirm(form.dataset.confirm)) e.preventDefault();
  }));

  /* ---------- Afficher / masquer un mot de passe ---------- */
  $$("[data-toggle-password]").forEach((btn) => btn.addEventListener("click", () => {
    const input = document.getElementById(btn.dataset.togglePassword);
    if (input) input.type = input.type === "password" ? "text" : "password";
  }));

  /* ---------- Copier ---------- */
  $$("[data-copy]").forEach((btn) => btn.addEventListener("click", () => {
    const input = document.getElementById(btn.dataset.copy);
    if (!input) return;
    const done = () => { const old = btn.textContent; btn.textContent = "Copié !"; setTimeout(() => (btn.textContent = old), 1500); };
    if (navigator.clipboard) navigator.clipboard.writeText(input.value).then(done, () => { input.select(); document.execCommand("copy"); done(); });
    else { input.select(); document.execCommand("copy"); done(); }
  }));

  /* ---------- Question secrète personnalisée ---------- */
  $$("[data-question-select]").forEach((select) => {
    const custom = select.closest("form").querySelector("[data-custom-question]");
    const sync = () => { if (custom) custom.hidden = select.value !== "__autre__"; };
    select.addEventListener("change", sync);
    sync();
  });

  /* ---------- Couleurs (aperçu en direct) ---------- */
  $$("[data-live-var]").forEach((input) => input.addEventListener("input", () => {
    root.style.setProperty(input.dataset.liveVar, input.value);
  }));
  $$("[data-swatch]").forEach((btn) => btn.addEventListener("click", () => {
    const input = document.getElementById(btn.dataset.swatch);
    input.value = btn.dataset.color;
    input.dispatchEvent(new Event("input"));
  }));

  /* ---------- Droits d'accès selon le rôle ---------- */
  const roleSelect = $("[data-role-select]");
  if (roleSelect) {
    const defaults = JSON.parse(($("#role-defaults") || {}).textContent || "{}");
    const permsCard = $("[data-perms]");
    const adminNote = $("[data-admin-note]");
    const useDefaults = $("[data-use-defaults]");
    const selects = $$("[data-perm]");
    const applyDefaults = () => {
      const levels = defaults[roleSelect.value] || {};
      selects.forEach((s) => { s.value = String(levels[s.dataset.perm] || 0); });
    };
    const sync = (fromRoleChange) => {
      const isAdmin = ["admin", "super_admin"].includes(roleSelect.value);
      if (permsCard) permsCard.hidden = isAdmin;
      if (adminNote) adminNote.hidden = !isAdmin;
      if (useDefaults && useDefaults.checked) applyDefaults();
      selects.forEach((s) => { s.disabled = useDefaults ? useDefaults.checked : false; });
      const list = $(".perm-list");
      if (list) list.classList.toggle("locked", !!(useDefaults && useDefaults.checked));
      if (fromRoleChange && useDefaults && !useDefaults.checked) applyDefaults();
    };
    roleSelect.addEventListener("change", () => sync(true));
    if (useDefaults) useDefaults.addEventListener("change", () => sync(false));
    sync(false);
    /* Les listes désactivées ne sont pas envoyées : on les réactive juste avant l'envoi */
    roleSelect.form.addEventListener("submit", () => selects.forEach((s) => { s.disabled = false; }));
  }

  /* ---------- Nombres ---------- */
  const num = (v) => {
    const n = parseFloat(String(v || "").replace(/[\s  ]/g, "").replace(",", "."));
    return isNaN(n) ? 0 : n;
  };
  const money = (n) => Math.round(n).toLocaleString("fr-FR").replace(/ | /g, " ") + " Ar";
  const qty = (n) => (Math.round(n * 100) / 100).toLocaleString("fr-FR", { maximumFractionDigits: 2 }).replace(/ | /g, " ");

  /* ---------- Lignes répétées (achats, formules, programmes) ---------- */
  function blankRow(row) {
    const clone = row.cloneNode(true);
    $$("[data-free-input], [data-free-tip]", clone).forEach((el) => el.remove());
    $$("select[data-free]", clone).forEach((sel) => {
      delete sel.dataset.freeDone;
      Array.from(sel.options).filter((o) => o.value.startsWith("__new__")).forEach((o) => o.remove());
    });
    $$("input", clone).forEach((i) => { i.value = ""; if (i.matches("[data-unit-input], [data-qty-unit]")) { i.readOnly = false; i.dataset.auto = "1"; } });
    $$("select", clone).forEach((s) => { s.selectedIndex = 0; });
    $$("[data-line-total], [data-share]", clone).forEach((el) => { el.textContent = "—"; });
    $$("[data-unit-label]", clone).forEach((el) => { el.textContent = "—"; });
    return clone;
  }
  function addLine(container, values) {
    const rows = $$("[data-line]", container);
    const row = blankRow(rows[rows.length - 1]);
    container.appendChild(row);
    if (window.freeEnhance) window.freeEnhance(row);
    if (values) Object.entries(values).forEach(([name, value]) => { const el = $(`[name="${name}"]`, row); if (el) el.value = value; });
    bindRow(row, container);
    container.dispatchEvent(new Event("lines-changed"));
    return row;
  }
  function bindRow(row, container) {
    const remove = $("[data-remove-line]", row);
    if (remove) remove.addEventListener("click", () => {
      if ($$("[data-line]", container).length > 1) row.remove();
      else $$("input, select", row).forEach((el) => { el.value = ""; });
      container.dispatchEvent(new Event("lines-changed"));
    });
    const unitSelect = $("[data-unit-select]", row);
    if (unitSelect) {
      const sync = () => {
        const opt = unitSelect.selectedOptions[0];
        const unit = opt && opt.dataset.unit ? opt.dataset.unit : "—";
        $$("[data-unit-label]", row).forEach((el) => { el.textContent = unit; });
        if (opt && opt.dataset.unit) $$("[data-unit-input]", row).forEach((el) => { el.value = opt.dataset.unit; el.readOnly = true; });
        const price = $("[data-price]", row);
        if (price && opt && opt.dataset.avg && num(opt.dataset.avg) > 0) price.placeholder = "prix moyen : " + qty(num(opt.dataset.avg));
      };
      unitSelect.addEventListener("change", sync);
      sync();
    }
    $$("input, select", row).forEach((el) => el.addEventListener("input", () => container.dispatchEvent(new Event("lines-changed"))));
    $$("select", row).forEach((el) => el.addEventListener("change", () => container.dispatchEvent(new Event("lines-changed"))));
  }
  $$("[data-lines]").forEach((container) => {
    $$("[data-line]", container).forEach((row) => bindRow(row, container));
    const form = container.closest("form");
    $$("[data-add-line]", form).forEach((btn) => btn.addEventListener("click", () => {
      let values = null;
      if (btn.hasAttribute("data-next-week")) {
        const ends = $$("[data-week-to]", container).map((i, idx) => num(i.value) || num($$("[data-week-from]", container)[idx].value));
        const next = Math.max(0, ...ends) + 1;
        values = { week_from: String(next), week_to: String(next) };
      }
      const row = addLine(container, values);
      const first = $("[data-free-input], select, input", row);
      if (first) first.focus();
    }));
  });

  /* Programme : une ligne par semaine */
  $$("[data-fill-weeks]").forEach((btn) => btn.addEventListener("click", () => {
    const form = btn.closest("form");
    const container = $("[data-lines]", form);
    const n = Math.min(120, Math.floor(num($("#fill-weeks", form).value)));
    if (!n || n < 1) return;
    if (!window.confirm(`Remplacer les lignes par ${n} ligne(s), une par semaine ?`)) return;
    const rows = $$("[data-line]", container);
    rows.slice(1).forEach((r) => r.remove());
    const first = rows[0];
    $$("input", first).forEach((i) => { i.value = ""; });
    $("[name=week_from]", first).value = "1";
    $("[name=week_to]", first).value = "1";
    for (let w = 2; w <= n; w++) addLine(container, { week_from: String(w), week_to: String(w) });
  }));

  /* ---------- Achat : totaux et paiement ---------- */
  $$("form[data-purchase-form]").forEach((form) => {
    const container = $("[data-lines]", form);
    const extra = $("[data-extra]", form);
    const paidField = $("[data-paid-field]", form);
    const accountField = $("[data-account-field]", form);
    const update = () => {
      let goods = 0;
      $$("[data-line]", container).forEach((row) => {
        const total = num($("[data-qty]", row).value) * num($("[data-price]", row).value);
        goods += total;
        $("[data-line-total]", row).textContent = money(total);
      });
      const transport = num(extra.value);
      $$("[data-goods-total]", form).forEach((el) => { el.textContent = money(goods); });
      $$("[data-extra-total]", form).forEach((el) => { el.textContent = money(transport); });
      $$("[data-grand-total]", form).forEach((el) => { el.textContent = money(goods + transport); });
      const mode = ($("[data-pay-mode]:checked", form) || {}).value || "tout";
      if (paidField) paidField.hidden = mode !== "partiel";
      if (accountField) accountField.hidden = mode === "credit";
    };
    container.addEventListener("lines-changed", update);
    extra.addEventListener("input", update);
    $$("[data-pay-mode]", form).forEach((r) => r.addEventListener("change", update));
    update();
  });

  /* ---------- Formule : total, parts et coût par kg (unités libres : g, kg, L…) ---------- */
  $$("form[data-formula-form]").forEach((form) => {
    const container = $("[data-lines]", form);
    const base = $("[data-base-qty]", form);
    const table = JSON.parse(($("#units-table", form) || {}).textContent || "{}");
    const key = (u) => (u || "").trim().toLowerCase().replace(/\s+/g, " ").replace(/\.$/, "");
    const convert = (q, from, to) => {
      if (!from || key(from) === key(to)) return q;
      const a = table[key(from)], b = table[key(to)];
      return a && b && a[0] === b[0] ? q * a[1] / b[1] : null;
    };
    const toKg = (q, u) => { const a = table[key(u)]; return a ? q * a[1] : null; };
    let baseTouched = base.value !== "";
    let lastAuto = null;
    base.addEventListener("input", () => { baseTouched = base.value !== ""; });
    const rowInfo = (row) => {
      const q = num($("[data-qty]", row).value);
      const opt = $("[data-unit-select]", row).selectedOptions[0];
      const unitInput = $("[data-qty-unit]", row);
      const matUnit = opt && opt.dataset.unit ? opt.dataset.unit : "";
      if (unitInput && unitInput.dataset.auto && matUnit) unitInput.value = matUnit;
      const unit = (unitInput && unitInput.value.trim()) || matUnit || "kg";
      const inMat = matUnit ? convert(q, unit, matUnit) : q;
      let kg = toKg(q, unit);
      if (kg === null) kg = 0;
      return { q, kg, inMat, cost: inMat === null ? 0 : inMat * num(opt && opt.dataset.cost), bad: inMat === null && q > 0, matUnit, unit };
    };
    const update = () => {
      const rows = $$("[data-line]", container);
      const infos = rows.map(rowInfo);
      const sum = infos.reduce((t, i) => t + i.kg, 0);
      const cost = infos.reduce((t, i) => t + i.cost, 0);
      rows.forEach((row, idx) => {
        const i = infos[idx];
        $("[data-share]", row).textContent = sum > 0 && i.kg > 0 ? qty((i.kg / sum) * 100) + " %" : "—";
        let warn = $("[data-unit-warn]", row);
        if (i.bad) {
          if (!warn) { warn = document.createElement("small"); warn.className = "hint text-danger"; warn.setAttribute("data-unit-warn", ""); $("[data-qty]", row).closest(".field").appendChild(warn); }
          warn.textContent = `Le stock est en « ${i.matUnit} » : impossible de convertir des « ${i.unit} ».`;
        } else if (warn) warn.remove();
      });
      if (!baseTouched || base.value === lastAuto) { base.value = sum > 0 ? qty(sum) : ""; lastAuto = base.value; baseTouched = false; }
      const b = num(base.value) || sum;
      $("[data-sum]", form).textContent = qty(sum) + " kg";
      const note = $("[data-sum-note]", form);
      const notKg = infos.filter((i) => i.q > 0 && toKg(1, i.unit) === null).map((i) => i.unit);
      if (note) {
        note.hidden = !notKg.length;
        note.textContent = notKg.length ? `Les quantités en « ${[...new Set(notKg)].join(", ")} » ne sont pas comptées dans ce total en kg : indiquez vous-même le poids du mélange dans « Ce mélange donne ».` : "";
      }
      $("[data-cost-kg]", form).textContent = b > 0 ? money(cost / b) : "0 Ar";
    };
    container.addEventListener("lines-changed", update);
    container.addEventListener("change", update);
    container.addEventListener("input", (e) => {
      if (e.target.matches("[data-qty-unit]")) e.target.dataset.auto = "";
      update();
    });
    base.addEventListener("input", update);
    update();
  });

  /* ---------- Fabrication : aperçu des matières nécessaires ---------- */
  $$("form[data-production-form]").forEach((form) => {
    const data = JSON.parse(($("#formula-data", form) || {}).textContent || "{}");
    const select = $("[data-formula-select]", form);
    const qtyInput = $("[data-prod-qty]", form);
    const panel = $("[data-needs]", form);
    const body = $("[data-needs-body]", form);
    const update = () => {
      const f = data[select.value];
      const q = num(qtyInput.value);
      if (!f || q <= 0) { panel.hidden = true; return; }
      panel.hidden = false;
      body.innerHTML = "";
      let total = 0, missing = 0;
      f.lines.forEach((l) => {
        const need = (l.qty * q) / (f.base || 100);
        const short = need - l.stock > 1e-9;
        if (short) missing++;
        total += need * l.cost;
        const tr = document.createElement("tr");
        if (short) tr.className = "row-short";
        const cells = [["Matière", l.name], ["Il faut", qty(need) + " " + l.unit],
          ["En stock", qty(l.stock) + " " + l.unit + (short ? " — manque " + qty(need - l.stock) + " " + l.unit : "")], ["Coût", money(need * l.cost)]];
        cells.forEach(([label, text], i) => {
          const td = document.createElement("td");
          td.dataset.label = label;
          if (i > 0) td.className = "right";
          td.textContent = text;
          if (i === 2 && short) td.classList.add("neg");
          tr.appendChild(td);
        });
        body.appendChild(tr);
      });
      $("[data-needs-total]", form).textContent = money(total);
      $("[data-needs-kg]", form).textContent = money(total / q);
      const status = $("[data-needs-status]", form);
      status.innerHTML = missing ? `<span class="badge badge-danger">${missing} matière(s) insuffisante(s)</span>` : '<span class="badge badge-ok">Stock suffisant</span>';
    };
    select.addEventListener("change", update);
    qtyInput.addEventListener("input", update);
    update();
  });

  /* ---------- Inventaire : écart en direct ---------- */
  $$("[data-inventory-row]").forEach((row) => {
    const input = $("[data-count]", row);
    const out = $("[data-diff]", row);
    const stock = num(row.dataset.stock);
    const update = () => {
      if (input.value.trim() === "") { out.textContent = "—"; out.className = "right"; return; }
      const d = num(input.value) - stock;
      out.textContent = (d > 0 ? "+" : "") + qty(d);
      out.className = "right " + (Math.abs(d) < 1e-9 ? "" : d > 0 ? "pos" : "neg");
    };
    input.addEventListener("input", update);
    update();
  });

  /* ---------- Champ visible seulement pour une entrée ---------- */
  $$("[data-entry-only]").forEach((field) => {
    const form = field.closest("form");
    const sync = () => { const k = $("input[name=kind]:checked", form); field.hidden = !k || k.value !== "stock_initial"; };
    $$("input[name=kind]", form).forEach((r) => r.addEventListener("change", sync));
    sync();
  });

  /* ---------- Lignes de tableau cliquables ---------- */
  $$("tr[data-href]").forEach((tr) => tr.addEventListener("click", (e) => {
    if (e.target.closest("a, button, input, select, form")) return;
    window.location.href = tr.dataset.href;
  }));

  /* ---------- Brouillon automatique toutes les 15 secondes ----------
     Les formulaires marqués data-autosave gardent leur saisie sur l'appareil
     en cas de coupure (réseau, batterie, page fermée par erreur). */
  $$("form[data-autosave]").forEach((form) => {
    const key = "brouillon:" + form.dataset.autosave;
    const fields = () => $$("input, select, textarea", form).filter((el) =>
      el.name && !["password", "file", "hidden"].includes(el.type) && el.name !== "_csrf" && !el.disabled);
    const snapshot = () => {
      const lines = $$("[data-lines]", form).map((c) => $$("[data-line]", c).length);
      const values = fields().map((el) => [el.name, (el.type === "checkbox" || el.type === "radio") ? el.checked : el.value]);
      return JSON.stringify({ lines, values });
    };
    const initial = snapshot();
    const saved = store.get(key);
    let restored = false;
    if (saved && saved !== initial) {
      let data = null;
      try { data = JSON.parse(saved); } catch (e) { store.remove(key); }
      if (data && Array.isArray(data.values)) {
        const bar = document.createElement("div");
        bar.className = "alert alert-warning static";
        bar.innerHTML = '<span>Un brouillon non enregistré a été retrouvé pour ce formulaire.</span>';
        const restore = document.createElement("button");
        restore.type = "button"; restore.className = "btn sm"; restore.textContent = "Reprendre le brouillon";
        const drop = document.createElement("button");
        drop.type = "button"; drop.className = "btn ghost sm"; drop.textContent = "Ignorer";
        bar.append(restore, drop);
        form.prepend(bar);
        restore.addEventListener("click", () => {
          $$("[data-lines]", form).forEach((c, i) => {
            const want = (data.lines || [])[i] || 1;
            while ($$("[data-line]", c).length < want) addLine(c);
          });
          const els = fields();
          data.values.forEach(([name, value], i) => {
            const el = els[i];
            if (!el || el.name !== name) return;
            if (el.type === "checkbox" || el.type === "radio") el.checked = !!value; else el.value = value;
          });
          els.forEach((el) => { el.dispatchEvent(new Event("change")); el.dispatchEvent(new Event("input")); });
          bar.remove();
          restored = true;
        });
        drop.addEventListener("click", () => { store.remove(key); bar.remove(); });
      }
    }
    let last = initial;
    const save = () => {
      const now = snapshot();
      if (now !== last && now !== initial) { store.set(key, now); last = now; }
    };
    setInterval(save, 15000);
    window.addEventListener("pagehide", save);
    let submitting = false;
    form.addEventListener("submit", () => { submitting = true; store.remove(key); });
    window.addEventListener("pagehide", () => { if (submitting) store.remove(key); });
    void restored;
  });

  /* ---------- Discussion ---------- */
  const setUnread = (n) => {
    $$("[data-unread-badge]").forEach((b) => { b.textContent = n; b.hidden = !n; });
  };
  const chat = $("[data-chat]");
  if (chat) {
    const list = $("[data-chat-list]", chat);
    const scrollDown = () => { list.scrollTop = list.scrollHeight; };
    scrollDown();
    const lastId = () => { const items = $$("[data-msg-id]", list); return items.length ? items[items.length - 1].dataset.msgId : 0; };
    const bindDeletes = (root) => $$("form[data-confirm]", root).forEach((f) => f.addEventListener("submit", (e) => { if (!window.confirm(f.dataset.confirm)) e.preventDefault(); }));
    const append = (html) => {
      if (!html) return;
      const empty = $("[data-chat-empty]", list);
      if (empty) empty.remove();
      const box = document.createElement("div");
      box.innerHTML = html;
      const lastDay = $$(".msg-day", list).map((d) => d.dataset.day).pop();
      $$(".msg-day", box).forEach((d) => { if (d.dataset.day === lastDay) d.remove(); });
      const nearBottom = list.scrollHeight - list.scrollTop - list.clientHeight < 120;
      bindDeletes(box);
      while (box.firstChild) list.appendChild(box.firstChild);
      if (nearBottom) scrollDown();
    };
    const poll = () => fetch(`${chat.dataset.newerUrl}?apres=${lastId()}&vu=1`, { headers: { "X-Requested-With": "fetch" } })
      .then((r) => r.ok ? r.json() : null).then((d) => { if (d) { append(d.html); setUnread(d.unread); } }).catch(() => {});
    setInterval(poll, 8000);
    const form = $("[data-chat-form]", chat);
    if (form) {
      const input = $("[data-chat-input]", form);
      input.addEventListener("keydown", (e) => {
        if (e.key === "Enter" && !e.shiftKey && !e.isComposing) { e.preventDefault(); form.requestSubmit(); }
      });
      form.addEventListener("submit", (e) => {
        if (!navigator.onLine) return;
        e.preventDefault();
        const text = input.value.trim();
        if (!text) return;
        const data = new FormData(form);
        input.value = "";
        fetch(form.action, { method: "POST", body: data, headers: { "X-Requested-With": "fetch" } })
          .then((r) => { if (!r.ok) throw new Error(); return poll(); })
          .then(scrollDown)
          .catch(() => { input.value = text; window.alert("Message non envoyé. Vérifiez la connexion et réessayez."); });
      });
    }
    $$("[data-older]", chat).forEach((a) => a.addEventListener("click", (e) => {
      e.preventDefault();
      fetch(a.href).then((r) => r.text()).then((html) => {
        const box = document.createElement("div");
        box.innerHTML = html;
        bindDeletes(box);
        const before = list.scrollHeight;
        const firstDay = $(".msg-day", list);
        const days = $$(".msg-day", box);
        if (firstDay && days.length && days[days.length - 1].dataset.day === firstDay.dataset.day) firstDay.remove();
        list.prepend(...box.childNodes);
        list.scrollTop = list.scrollHeight - before;
        const first = $("[data-msg-id]", list);
        if (!html.trim() || !first) a.remove(); else a.href = a.href.replace(/avant=\d+/, "avant=" + first.dataset.msgId);
      });
    }));
  } else if (window.UNREAD_URL) {
    setInterval(() => {
      if (document.hidden) return;
      fetch(window.UNREAD_URL).then((r) => r.ok ? r.json() : null).then((d) => { if (d) setUnread(d.unread); }).catch(() => {});
    }, 45000);
  }
})();

/* Listes déroulantes libres : l'option « Autre : écrire… » ouvre une case à remplir */
(function () {
  document.querySelectorAll("[data-free-for]").forEach((input) => {
    const select = input.form && input.form.elements[input.dataset.freeFor];
    if (!select) return;
    const sync = (focus) => {
      const open = select.value === "__new__";
      input.hidden = !open;
      input.required = open;
      if (open && focus) input.focus();
    };
    select.addEventListener("change", () => sync(true));
    sync(false);
  });
})();

/* Cases libres avec suggestions : au clic, toutes les suggestions s'affichent */
(function () {
  document.querySelectorAll("input[data-combo]").forEach((input) => {
    const hint = input.placeholder;
    let saved = "", typed = false;
    input.addEventListener("focus", () => {
      saved = input.value; typed = false;
      if (saved) { input.placeholder = saved; input.value = ""; }
    });
    input.addEventListener("input", () => { typed = true; });
    input.addEventListener("blur", () => {
      if (!typed && !input.value) input.value = saved;  // rien tapé : on garde l'ancienne valeur
      input.placeholder = hint;
    });
  });
})();

/* ==========================================================================
   Saisie libre partout : chaque liste [data-free] devient une case où l'on écrit.
   Un nom connu choisit l'élément existant ; un nom nouveau est envoyé comme
   « __new__:nom|unité » et le serveur le crée tout seul.
   ========================================================================== */
(function () {
  const norm = (s) => (s || "").trim().replace(/\s+/g, " ").toLowerCase();
  let counter = 0;
  const label = (opt) => (opt.dataset.label || opt.textContent || "").trim();

  function datalistFor(select) {
    const id = "free-dl-" + (select.dataset.free || "x") + "-" + (++counter);
    const dl = document.createElement("datalist");
    dl.id = id;
    Array.from(select.options).forEach((o) => {
      if (!o.value || o.value.startsWith("__new__")) return;
      const opt = document.createElement("option");
      opt.value = label(o);
      dl.appendChild(opt);
    });
    document.body.appendChild(dl);
    return id;
  }

  function unitInputOf(select) {
    const row = select.closest("[data-line]") || select.closest("form");
    return row ? row.querySelector("[data-unit-input]") : null;
  }

  function apply(select, input) {
    const text = input.value.trim().replace(/\s+/g, " ");
    Array.from(select.options).filter((o) => o.value.startsWith("__new__")).forEach((o) => o.remove());
    const unitInput = unitInputOf(select);
    let match = null;
    if (text) match = Array.from(select.options).find((o) => o.value && norm(label(o)) === norm(text));
    if (!match && text) {
      // le nom écrit peut aussi être le nom sans la précision entre parenthèses
      match = Array.from(select.options).find((o) => o.value && norm(label(o).replace(/\s*\(.*\)\s*$/, "")) === norm(text));
    }
    if (match) {
      select.value = match.value;
      input.classList.remove("is-new");
      if (unitInput) { unitInput.value = match.dataset.unit || unitInput.value; unitInput.readOnly = true; }
    } else if (text) {
      const unit = unitInput ? (unitInput.value.trim() || "kg") : "";
      const opt = document.createElement("option");
      opt.value = "__new__:" + text + (unit ? "|" + unit : "");
      opt.textContent = text;
      select.appendChild(opt);
      select.value = opt.value;
      input.classList.add("is-new");
      if (unitInput) { unitInput.readOnly = false; if (!unitInput.value || unitInput.dataset.auto) unitInput.value = unit; }
    } else {
      select.value = "";
      input.classList.remove("is-new");
      if (unitInput) unitInput.readOnly = false;
    }
    const tip = input.parentElement.querySelector("[data-free-tip]");
    if (tip) tip.hidden = !input.classList.contains("is-new");
    select.dispatchEvent(new Event("change", { bubbles: true }));
  }

  function enhance(select) {
    if (select.dataset.freeDone) return;
    select.dataset.freeDone = "1";
    const input = document.createElement("input");
    input.type = "text";
    input.className = "input free-input";
    input.autocomplete = "off";
    input.maxLength = 80;
    input.placeholder = select.dataset.placeholder || "Écrivez ou choisissez…";
    input.setAttribute("list", datalistFor(select));
    input.setAttribute("data-free-input", "");
    if (select.id) { input.id = select.id; select.removeAttribute("id"); }
    if (select.required) input.required = true;
    select.required = false;
    const cur = select.selectedOptions[0];
    input.value = cur && cur.value ? label(cur) : "";
    select.hidden = true;
    select.tabIndex = -1;
    select.after(input);
    const tip = document.createElement("small");
    tip.className = "hint new-tip";
    tip.setAttribute("data-free-tip", "");
    tip.hidden = true;
    tip.textContent = select.dataset.newTip || "Nouveau : sera créé automatiquement.";
    input.after(tip);
    // au clic : toutes les suggestions (on garde l'ancienne valeur si rien n'est tapé)
    let saved = "", typed = false;
    input.addEventListener("focus", () => { saved = input.value; typed = false; if (saved) { input.placeholder = saved; input.value = ""; } });
    input.addEventListener("input", () => { typed = true; apply(select, input); });
    input.addEventListener("change", () => apply(select, input));
    input.addEventListener("blur", () => {
      if (!typed && !input.value) input.value = saved;
      input.placeholder = select.dataset.placeholder || "Écrivez ou choisissez…";
      apply(select, input);
    });
    if (cur && cur.value) apply(select, input);
  }

  function scan(root) { (root || document).querySelectorAll("select[data-free]").forEach(enhance); }
  scan();
  // unité modifiée pour une matière nouvelle : on met à jour la valeur envoyée
  document.addEventListener("input", (e) => {
    const unit = e.target.closest("[data-unit-input]");
    if (!unit) return;
    unit.dataset.auto = "";
    const row = unit.closest("[data-line]") || unit.closest("form");
    const input = row && row.querySelector("[data-free-input]");
    const select = input && input.previousElementSibling;
    if (select && select.matches("select[data-free]")) apply(select, input);
  });
  window.freeEnhance = scan;
})();

/* Fabrication : nombre de sacs × kg par sac → quantité */
(function () {
  const n = document.querySelector("[data-bags-n]"), kg = document.querySelector("[data-bags-kg]");
  const target = document.querySelector("[data-prod-qty]");
  if (!n || !kg || !target) return;
  const parse = (v) => parseFloat(String(v || "").replace(/\s/g, "").replace(",", ".")) || 0;
  const sync = () => {
    const total = parse(n.value) * (parse(kg.value) || 50);
    if (parse(n.value) > 0) { target.value = String(Math.round(total * 1000) / 1000).replace(".", ","); target.dispatchEvent(new Event("input", { bubbles: true })); }
  };
  n.addEventListener("input", sync); kg.addEventListener("input", sync);
})();
