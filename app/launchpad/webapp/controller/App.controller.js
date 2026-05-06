sap.ui.define([
  "sap/ui/core/mvc/Controller",
  "sap/ui/core/Fragment",
  "sap/ui/core/Theming",
  "sap/ui/core/IconPool",
  "sap/m/MessageToast",
  "sap/m/MessageBox"
], function (Controller, Fragment, Theming, IconPool, MessageToast, MessageBox) {
  "use strict";

  const SERVICE_BASE = "/odata/v4/launchpad";
  const THEME_KEY = "syzygyLaunchpadTheme";

  return Controller.extend("syzygylaunchpad.controller.App", {

    onInit: function () {
      this._uiModel = this.getOwnerComponent().getModel("ui");
      this._loadCurrentUser();
    },

    _loadCurrentUser: async function () {
      try {
        const res = await fetch(`${SERVICE_BASE}/currentUser()`, { headers: { Accept: "application/json" } });
        if (!res.ok) return;
        const u = await res.json();
        const first = u.firstname || "";
        const last  = u.lastname  || "";
        const full  = (first + " " + last).trim() || u.name || u.email || "User";
        const initials = ((first[0] || u.name?.[0] || "U") + (last[0] || "")).toUpperCase();
        this._uiModel.setProperty("/user/name", full);
        this._uiModel.setProperty("/user/initials", initials);
      } catch (e) { /* keep defaults */ }
    },

    /* ---------- shell bar ---------- */

    onSideToggle: function () {
      this._uiModel.setProperty("/sideExpanded", !this._uiModel.getProperty("/sideExpanded"));
    },

    onThemeChange: function (oEvent) {
      const next = oEvent.getParameter("item").getKey();
      this._applyTheme(next);
    },

    _applyTheme: function (next) {
      this._uiModel.setProperty("/theme", next);
      Theming.setTheme(next);
      // UI5 1.136 doesn't set data-sap-ui-theme on <html> at runtime, so our
      // theme-scoped CSS would never match. Mirror the theme onto a custom
      // attribute we control.
      document.documentElement.setAttribute("data-sz-theme", next);
      try { localStorage.setItem(THEME_KEY, next); } catch (e) { /* ignore */ }
      const url = this._uiModel.getProperty("/currentAppUrl");
      if (url) this._loadAppIntoFrame(url, next);
    },

    onOpenAiBot: function () {
      MessageBox.information("AI Assistant is coming soon.", { title: "AI Assistant" });
    },

    onOpenNotifications: function () {
      MessageBox.information("You have no new notifications.", { title: "Notifications" });
    },

    onUserPress: async function (oEvent) {
      if (!this._userPopover) {
        this._userPopover = await Fragment.load({
          id: this.getView().getId(),
          name: "syzygylaunchpad.view.UserPopover",
          controller: this
        });
        this.getView().addDependent(this._userPopover);
      }
      this._userPopover.openBy(oEvent.getSource());
    },

    onUserMenuManageApps: function () {
      if (this._userPopover) this._userPopover.close();
      this.onOpenManageDialog();
    },

    onUserMenuSettings: function () {
      if (this._userPopover) this._userPopover.close();
      MessageBox.information("Settings coming soon.", { title: "Settings" });
    },

    /* ---------- nav ---------- */

    onNavItemSelect: function () { /* per-item handlers fire */ },

    onSelectHome: function () {
      this._uiModel.setProperty("/currentAppId", null);
      this._uiModel.setProperty("/currentAppUrl", null);
      this._uiModel.setProperty("/selectedNavKey", "home");
      this.byId("mainNav").to(this.byId("homePage"));
    },

    onSelectApp: async function (oEvent) {
      const item = oEvent.getSource();
      const ctx = item.getBindingContext();
      if (!ctx) return;
      let app = ctx.getObject();
      let url = app.runtimeUrl;
      if (!url) {
        try { url = await ctx.requestProperty("runtimeUrl"); } catch (e) { /* fall through */ }
      }
      if (!url) {
        MessageBox.warning(`No URL configured for "${app.displayName || app.appId}". Edit it from Manage Apps.`);
        return;
      }
      this._uiModel.setProperty("/currentAppId", app.appId);
      this._uiModel.setProperty("/currentAppTitle", app.displayName);
      this._uiModel.setProperty("/currentAppUrl", url);
      this._uiModel.setProperty("/selectedNavKey", app.appId);
      this.byId("mainNav").to(this.byId("appPage"));
      this._loadAppIntoFrame(url, this._uiModel.getProperty("/theme"));
    },

    /* ---------- iframe injection ---------- */

    _loadAppIntoFrame: function (url, theme) {
      const host = this.byId("appFrameHost");
      if (!host) return;
      this._uiModel.setProperty("/frameLoading", true);
      const apply = () => this._injectIframe(host.getDomRef(), url, theme);
      const dom = host.getDomRef();
      if (dom) apply();
      else host.addEventDelegate({ onAfterRendering: apply });
    },

    _injectIframe: function (hostEl, url, theme) {
      if (!hostEl) return;
      let frame = hostEl.querySelector("iframe.szAppFrame");
      if (!frame) {
        frame = document.createElement("iframe");
        frame.className = "szAppFrame";
        frame.setAttribute("frameborder", "0");
        hostEl.appendChild(frame);
      }
      frame.onload = () => this._uiModel.setProperty("/frameLoading", false);
      frame.onerror = () => this._uiModel.setProperty("/frameLoading", false);
      frame.src = this._withThemeParam(url, theme);
    },

    _withThemeParam: function (url, theme) {
      if (!url) return url;
      try {
        const u = new URL(url);
        if (theme) u.searchParams.set("sap-ui-theme", theme);
        // cache-bust on theme change so the iframe actually reloads
        u.searchParams.set("_t", Date.now().toString());
        return u.toString();
      } catch (e) {
        return url;
      }
    },

    /* ---------- manage apps dialog ---------- */

    onOpenManageDialog: async function () {
      if (!this._manageDialog) {
        this._manageDialog = await Fragment.load({
          id: this.getView().getId(),
          name: "syzygylaunchpad.view.ManageAppsDialog",
          controller: this
        });
        this.getView().addDependent(this._manageDialog);
      }
      this._manageDialog.open();
      await this._reloadManageList();
    },

    onCloseManageDialog: function () { this._manageDialog.close(); },
    onRefreshDiscover: function () { this._reloadManageList(); },

    onSearchApps: function (oEvent) {
      this._manageSearch = (oEvent.getParameter("newValue") || "").toLowerCase();
      this._applyManageFilters();
    },

    onManageFilterChange: function () {
      this._applyManageFilters();
    },

    _applyManageFilters: function () {
      const q = this._manageSearch || "";
      const all = this._uiModel.getProperty("/manage/allItems") || [];
      const view = this.getView();
      const typeKeys   = (view.byId("filterAppType")?.getSelectedKeys() || []);
      const statusKeys = (view.byId("filterStatus")?.getSelectedKeys() || []);

      const filtered = all.filter(a => {
        if (q && !a.displayName.toLowerCase().includes(q) && !a.appId.toLowerCase().includes(q)) return false;
        if (typeKeys.length && !typeKeys.includes(a.appType)) return false;
        if (statusKeys.length) {
          const s = a.enabled ? "active" : "inactive";
          if (!statusKeys.includes(s)) return false;
        }
        return true;
      });
      this._uiModel.setProperty("/manage/items", filtered);
    },

    _reloadManageList: async function () {
      try {
        const res = await fetch(`${SERVICE_BASE}/ConfiguredApps?$select=appId,displayName,runtimeUrl,iconUri,appType,enabled,sortOrder&$orderby=sortOrder`, {
          headers: { Accept: "application/json" }
        });
        if (!res.ok) throw new Error(`${res.status} ${await res.text()}`);
        const json = await res.json();
        const items = (json.value || []).map(a => ({
          ...a,
          iconUri: a.iconUri || 'sap-icon://grid'
        }));
        this._uiModel.setProperty("/manage/allItems", items);
        this._applyManageFilters();
      } catch (err) {
        MessageBox.error("Could not load apps.\n" + err.message);
      }
    },

    onToggleApp: async function (oEvent) {
      const sw = oEvent.getSource();
      const newState = oEvent.getParameter("state");
      const appId = sw.getCustomData()[0]?.getValue();
      try {
        await this._callAction("toggleApp", { appId, enabled: newState });
        // refresh sidebar
        this.getView().getModel().refresh();
        // local update
        const all = this._uiModel.getProperty("/manage/allItems") || [];
        const updated = all.map(a => a.appId === appId ? { ...a, enabled: newState } : a);
        this._uiModel.setProperty("/manage/allItems", updated);
        this._applyManageFilters();
        // if we just disabled the active app, go home
        if (!newState && this._uiModel.getProperty("/currentAppId") === appId) this.onSelectHome();
        MessageToast.show(`App ${newState ? 'enabled' : 'disabled'}`);
      } catch (err) {
        sw.setState(!newState);
        MessageBox.error("Could not update app.\n" + err.message);
      }
    },

    onDeleteApp: function (oEvent) {
      const btn = oEvent.getSource();
      const data = {};
      btn.getCustomData().forEach(cd => { data[cd.getKey()] = cd.getValue(); });
      MessageBox.confirm(`Remove "${data.displayName}" from the launchpad?`, {
        title: "Confirm Removal",
        onClose: async (action) => {
          if (action !== MessageBox.Action.OK) return;
          try {
            await this._callAction("unregisterApp", { appId: data.appId });
            if (this._uiModel.getProperty("/currentAppId") === data.appId) this.onSelectHome();
            this.getView().getModel().refresh();
            await this._reloadManageList();
            MessageToast.show("App removed");
          } catch (err) {
            MessageBox.error("Could not remove app.\n" + err.message);
          }
        }
      });
    },

    /* ---------- app form (add / edit) ---------- */

    onOpenAddApp: async function () {
      this._resetAppForm({ isEdit: false, title: "Add New App" });
      await this._openAppFormDialog();
    },

    onEditApp: async function (oEvent) {
      const btn = oEvent.getSource();
      const appId = btn.getCustomData()[0]?.getValue();
      const all = this._uiModel.getProperty("/manage/allItems") || [];
      const app = all.find(a => a.appId === appId);
      if (!app) return;
      this._resetAppForm({
        isEdit: true,
        title: `Edit "${app.displayName}"`,
        appId: app.appId,
        displayName: app.displayName,
        runtimeUrl: app.runtimeUrl,
        iconUri: app.iconUri || 'sap-icon://grid',
        appType: app.appType || 'btp'
      });
      await this._openAppFormDialog();
    },

    _resetAppForm: function (overrides) {
      const base = {
        isEdit: false,
        title: "Add New App",
        appId: "",
        displayName: "",
        runtimeUrl: "",
        iconUri: "sap-icon://grid",
        appType: "btp"
      };
      this._uiModel.setProperty("/appForm", Object.assign(base, overrides || {}));
    },

    _openAppFormDialog: async function () {
      if (!this._appFormDialog) {
        this._appFormDialog = await Fragment.load({
          id: this.getView().getId(),
          name: "syzygylaunchpad.view.AppFormDialog",
          controller: this
        });
        this.getView().addDependent(this._appFormDialog);
      }
      this._appFormDialog.open();
    },

    onCancelAppForm: function () { this._appFormDialog.close(); },

    onSubmitAppForm: async function () {
      const f = this._uiModel.getProperty("/appForm");
      if (!f.appId || !f.displayName || !f.runtimeUrl) {
        MessageBox.warning("Please fill in App ID, Display Name and URL.");
        return;
      }
      this._appFormDialog.setBusy(true);
      try {
        if (f.isEdit) {
          await this._callAction("updateApp", {
            appId: f.appId,
            displayName: f.displayName,
            runtimeUrl: f.runtimeUrl,
            iconUri: f.iconUri,
            enabled: true
          }, { retryOnce: true, timeoutMs: 30000 });
          MessageToast.show("App updated");
        } else {
          await this._callAction("registerApp", {
            appId: f.appId,
            displayName: f.displayName,
            runtimeUrl: f.runtimeUrl,
            iconUri: f.iconUri,
            appType: f.appType,
            enabled: true
          }, { retryOnce: true, timeoutMs: 30000 });
          MessageToast.show("App added");
        }
        this._appFormDialog.close();
        this.getView().getModel().refresh();
        await this._reloadManageList();
      } catch (err) {
        MessageBox.error("Could not save app.\n" + err.message);
      } finally {
        this._appFormDialog.setBusy(false);
      }
    },

    /* ---------- icon picker ---------- */

    onOpenIconPicker: async function () {
      if (!this._iconPickerDialog) {
        this._iconPickerDialog = await Fragment.load({
          id: this.getView().getId(),
          name: "syzygylaunchpad.view.IconPickerDialog",
          controller: this
        });
        this.getView().addDependent(this._iconPickerDialog);
      }
      // build full icon list once
      let all = this._uiModel.getProperty("/iconPicker/all");
      if (!all || !all.length) {
        all = IconPool.getIconNames().map(n => `sap-icon://${n}`);
        this._uiModel.setProperty("/iconPicker/all", all);
      }
      // show first 200 by default for snappy load
      this._uiModel.setProperty("/iconPicker/filtered", all.slice(0, 200));
      this._iconPickerDialog.open();
    },

    onIconSearch: function (oEvent) {
      const q = (oEvent.getParameter("newValue") || "").toLowerCase().trim();
      const all = this._uiModel.getProperty("/iconPicker/all") || [];
      const filtered = q
        ? all.filter(i => i.includes(q)).slice(0, 400)
        : all.slice(0, 200);
      this._uiModel.setProperty("/iconPicker/filtered", filtered);
    },

    onIconChosen: function (oEvent) {
      const icon = oEvent.getSource();
      const ctx = icon.getBindingContext("ui");
      const value = ctx ? ctx.getProperty("") : null;
      if (value) {
        this._uiModel.setProperty("/appForm/iconUri", value);
        this._iconPickerDialog.close();
      }
    },

    onCloseIconPicker: function () { this._iconPickerDialog.close(); },

    /* ---------- helpers ---------- */

    _callAction: async function (name, payload, opts) {
      const o = opts || {};
      const attempt = async () => {
        const ctrl = new AbortController();
        const t = setTimeout(() => ctrl.abort(), o.timeoutMs || 20000);
        try {
          const res = await fetch(`${SERVICE_BASE}/${name}`, {
            method: "POST",
            headers: { "Content-Type": "application/json", "Accept": "application/json" },
            body: JSON.stringify(payload || {}),
            signal: ctrl.signal
          });
          if (!res.ok) {
            const txt = await res.text();
            let msg = txt;
            try { msg = JSON.parse(txt).error?.message || txt; } catch {}
            throw new Error(`${res.status}: ${msg}`);
          }
          const ct = res.headers.get("content-type") || "";
          return ct.includes("application/json") ? res.json() : null;
        } finally {
          clearTimeout(t);
        }
      };
      try {
        return await attempt();
      } catch (err) {
        // retry once on transient failures (cold-start latency, abort)
        if (o.retryOnce && (err.name === "AbortError" || /fetch|network|ECONN|timeout/i.test(err.message))) {
          await new Promise(r => setTimeout(r, 500));
          return await attempt();
        }
        throw err;
      }
    }
  });
});
