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

  /* ---------- Brouillon automatique toutes les 15 secondes ----------
     Les formulaires marqués data-autosave gardent leur saisie sur l'appareil
     en cas de coupure (réseau, batterie, page fermée par erreur). */
  $$("form[data-autosave]").forEach((form) => {
    const key = "brouillon:" + form.dataset.autosave;
    const fields = () => $$("input, select, textarea", form).filter((el) =>
      el.name && !["password", "file", "hidden"].includes(el.type) && el.name !== "_csrf" && !el.disabled);
    const snapshot = () => {
      const data = {};
      fields().forEach((el) => { data[el.name] = el.type === "checkbox" ? el.checked : el.value; });
      return JSON.stringify(data);
    };
    const initial = snapshot();
    const saved = store.get(key);
    if (saved && saved !== initial) {
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
        const data = JSON.parse(saved);
        fields().forEach((el) => {
          if (!(el.name in data)) return;
          if (el.type === "checkbox") el.checked = !!data[el.name]; else el.value = data[el.name];
          el.dispatchEvent(new Event("change"));
        });
        bar.remove();
      });
      drop.addEventListener("click", () => { store.remove(key); bar.remove(); });
    }
    let last = initial;
    setInterval(() => {
      const now = snapshot();
      if (now !== last && now !== initial) { store.set(key, now); last = now; }
    }, 15000);
    form.addEventListener("submit", () => store.remove(key));
  });
})();
