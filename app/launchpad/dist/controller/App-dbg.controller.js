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
  // Absolute, origin-rooted path to the master-data service via the approuter
  // (see ^/api/masterdata/(.*)$ route in app/router/xs-app.json). We deliberately
  // use the /api/ prefix to avoid collision with the html5-apps-repo app named
  // `masterdata` from the master_data project — a bare /masterdata/ would be
  // served by html5-apps-repo runtime instead of being proxied to the CAP service.
  const MASTERDATA_BASE = "/api/masterdata/";
  const AUDIT_TENANT_ID = "G325";
  const AUDIT_APPLICATION = "Syzygy Launchpad";
  const AUDIT_MODULE = "Authentication";
  const AUDIT_SOURCE = "Launchpad";
  const THEME_KEY    = "syzygyLaunchpadTheme";

  // ---- idle-timeout (mirrors approuter sessionTimeout) ----
  // Keep this in sync with the sessionTimeout value in app/router/xs-app.json.
  // Set to 10 minutes.
  const IDLE_TIMEOUT_MS = 10 * 60 * 1000;
  const IDLE_EVENTS     = ["mousemove", "mousedown", "keydown", "touchstart", "scroll", "click"];

  return Controller.extend("syzygylaunchpad.controller.App", {

    onInit: function () {
      this._uiModel = this.getOwnerComponent().getModel("ui");
      this._auditLogoutFired = false;
      this._loadCurrentUser();
      this._startIdleTimer();
      this._installUnloadLogout();
    },

    onExit: function () {
      this._clearIdleTimer();
      this._uninstallUnloadLogout();
    },

    /* ---------- idle-session timeout ---------- */

    _startIdleTimer: function () {
      this._idleResetHandler = this._resetIdleTimer.bind(this);
      IDLE_EVENTS.forEach(ev =>
        document.addEventListener(ev, this._idleResetHandler, { passive: true })
      );
      this._resetIdleTimer();
    },

    _resetIdleTimer: function () {
      if (this._idleTimerId) clearTimeout(this._idleTimerId);
      this._idleTimerId = setTimeout(async () => {
        // Session has timed out on the client side — log the event, then
        // redirect through the approuter logout endpoint so the server
        // session is also cleared, and the user lands on the custom logout page.
        // Await the audit POST: once /do/logout fires the XSUAA session is
        // destroyed and any in-flight POST would be rejected with 401.
        try { await this._logLogout("IdleTimeout"); } catch (e) { /* never block */ }
        window.location.href = "/do/logout";
      }, IDLE_TIMEOUT_MS);
    },

    _clearIdleTimer: function () {
      if (this._idleTimerId) clearTimeout(this._idleTimerId);
      IDLE_EVENTS.forEach(ev =>
        document.removeEventListener(ev, this._idleResetHandler)
      );
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
        this._uiModel.setProperty("/user/name",      full);
        this._uiModel.setProperty("/user/initials",  initials);
        this._uiModel.setProperty("/user/email",     u.email || "");
        this._uiModel.setProperty("/user/firstname", first);
        this._uiModel.setProperty("/user/lastname",  last);
        this._uiModel.setProperty("/user/id",        u.id || "");
        // Pre-populate the settings form
        this._uiModel.setProperty("/settings/firstname", first);
        this._uiModel.setProperty("/settings/lastname",  last);
        // Cache for audit logging and emit the LOGIN event
        this._auditUserId   = u.id || u.email || "";
        this._auditUserName = full;
        this._logLogin();
      } catch (e) { /* keep defaults */ }
    },

    /* ---------- audit logging (login / logout) ---------- */

    _buildAuditEntry: function (extras) {
      return Object.assign({
        TENANT_ID:       AUDIT_TENANT_ID,
        EVENT_TIMESTAMP: new Date().toISOString(),
        USER_ID:         this._auditUserId   || "",
        USER_NAME:       this._auditUserName || "",
        SOURCE:          AUDIT_SOURCE,
        APPLICATION:     AUDIT_APPLICATION,
        MODULE:          AUDIT_MODULE,
        STATUS:          "Success"
      }, extras || {});
    },

    _fetchCsrfToken: async function () {
      // Cache the token for the lifetime of the controller — CAP/XSUAA sessions
      // last well beyond a single login/logout pair.
      if (this._csrfToken) return this._csrfToken;
      try {
        // Use the origin-rooted absolute URL so the browser sends the request
        // to the approuter root (not the html5-apps-repo app path).
        const res = await fetch(window.location.origin + MASTERDATA_BASE, {
          method:      "GET",
          headers:     { "X-CSRF-Token": "Fetch", "Accept": "application/json" },
          credentials: "include"
        });
        // Header name is case-insensitive; fetch() normalises it.
        const tok = res.headers.get("x-csrf-token");
        if (tok && tok.toLowerCase() !== "required") this._csrfToken = tok;
      } catch (e) { /* swallow — POST will surface the real error */ }
      return this._csrfToken;
    },

    _postAuditLog: async function (entry, bKeepalive) {
      try {
        // CAP enforces CSRF on session-bearing browser POSTs. Fetch a real
        // token first; only attach the header if we actually got one back.
        const csrfToken = await this._fetchCsrfToken();
        const headers = {
          "Content-Type": "application/json",
          "Accept":       "application/json"
        };
        if (csrfToken) headers["X-CSRF-Token"] = csrfToken;

        return fetch(window.location.origin + MASTERDATA_BASE + "createAuditLogs", {
          method:      "POST",
          headers:     headers,
          body:        JSON.stringify({ items: [entry] }),
          credentials: "include",
          keepalive:   !!bKeepalive
        });
      } catch (e) {
        // never break the UI for a logging failure
        return Promise.resolve();
      }
    },

    _logLogin: function () {
      if (!this._auditUserId) return;
      this._postAuditLog(this._buildAuditEntry({
        ACTION_TYPE: "LOGIN",
        ENTITY_NAME: "Login",
        ENTITY_TYPE: "login",
        DESCRIPTION: "User signed in to the launchpad"
      }), false);
    },

    _logLogout: function (sReason) {
      // Guard against duplicate emissions when multiple code paths converge
      // (e.g. user clicks Sign Out → beforeunload also fires).
      if (this._auditLogoutFired) return Promise.resolve();
      this._auditLogoutFired = true;
      if (!this._auditUserId) return Promise.resolve();
      const reason = sReason || "Success";
      const description =
        reason === "IdleTimeout" ? "Session timed out due to inactivity" :
        reason === "BrowserClose" ? "User closed the browser or tab" :
        "User signed out of the launchpad";
      // Return the POST promise so explicit sign-out flows can await it
      // before navigating to /do/logout (which destroys the XSUAA session
      // and would otherwise cause the in-flight POST to 401).
      return this._postAuditLog(this._buildAuditEntry({
        ACTION_TYPE: "LOGOUT",
        ENTITY_NAME: "Logout",
        ENTITY_TYPE: "logout",
        STATUS:      reason,
        DESCRIPTION: description
      }), true);
    },

    _installUnloadLogout: function () {
      // Best-effort logout log on browser/tab close. Uses fetch keepalive so
      // the POST survives the page navigation.
      this._unloadLogoutHandler = () => this._logLogout("BrowserClose");
      window.addEventListener("beforeunload", this._unloadLogoutHandler);
    },

    _uninstallUnloadLogout: function () {
      if (this._unloadLogoutHandler) {
        window.removeEventListener("beforeunload", this._unloadLogoutHandler);
        this._unloadLogoutHandler = null;
      }
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
      this._uiModel.setProperty("/aiPanelOpen", !this._uiModel.getProperty("/aiPanelOpen"));
    },

    onCloseAiPanel: function () {
      this._uiModel.setProperty("/aiPanelOpen", false);
    },

    onOpenNotifications: async function (oEvent) {
      if (!this._notifPopover) {
        this._notifPopover = await Fragment.load({
          id: this.getView().getId(),
          name: "syzygylaunchpad.view.NotificationsPopover",
          controller: this
        });
        this.getView().addDependent(this._notifPopover);
      }
      this._notifPopover.openBy(oEvent.getSource());
    },

    onGlobalSearch: function () {
      MessageToast.show("Search is coming soon.");
    },

    onClearAllNotifications: function () {
      MessageToast.show("All notifications cleared.");
      if (this._notifPopover) this._notifPopover.close();
    },

    onNotificationClose: function () {
      MessageToast.show("Notification dismissed.");
    },

    onSeeAllNotifications: function () {
      MessageToast.show("Full notifications panel is coming soon.");
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

    onUserMenuSettings: async function () {
      if (this._userPopover) this._userPopover.close();
      await this._openSettingsDialog();
    },

    onUserMenuSignOut: async function () {
      if (this._userPopover) this._userPopover.close();
      // Log the logout, then hand off to the approuter, which terminates
      // the XSUAA session and redirects the browser to the configured logoutPage.
      // We must AWAIT the audit POST — once /do/logout is hit the session is
      // gone and any in-flight POST is rejected with 401.
      try { await this._logLogout("Success"); } catch (e) { /* never block sign-out */ }
      window.location.href = "/do/logout";
    },

    /* ---------- settings dialog ---------- */

    _openSettingsDialog: async function () {
      if (!this._settingsDialog) {
        this._settingsDialog = await Fragment.load({
          id: this.getView().getId(),
          name: "syzygylaunchpad.view.SettingsDialog",
          controller: this
        });
        this.getView().addDependent(this._settingsDialog);
      }
      this._uiModel.setProperty("/settings/section", "userAccount");
      this._settingsDialog.open();
    },

    onCloseSettings: function () {
      if (this._settingsDialog) this._settingsDialog.close();
    },

    onSettingsSectionPress: function (oEvent) {
      const section = oEvent.getSource().data("section");
      if (section) this._uiModel.setProperty("/settings/section", section);
    },

    onSaveUserProfile: async function () {
      const s = this._uiModel.getProperty("/settings");
      const firstname = (s.firstname || "").trim();
      const lastname  = (s.lastname  || "").trim();
      if (this._settingsDialog) this._settingsDialog.setBusy(true);
      try {
        const updated = await this._callAction("updateUserProfile", { firstname, lastname });
        const full     = (updated.firstname + " " + updated.lastname).trim() || updated.name || "User";
        const initials = ((updated.firstname?.[0] || "U") + (updated.lastname?.[0] || "")).toUpperCase();
        this._uiModel.setProperty("/user/name",       full);
        this._uiModel.setProperty("/user/initials",   initials);
        this._uiModel.setProperty("/user/firstname",  updated.firstname);
        this._uiModel.setProperty("/user/lastname",   updated.lastname);
        this._uiModel.setProperty("/settings/firstname", updated.firstname);
        this._uiModel.setProperty("/settings/lastname",  updated.lastname);
        MessageToast.show("Profile saved");
      } catch (err) {
        MessageBox.error("Could not save profile.\n" + err.message);
      } finally {
        if (this._settingsDialog) this._settingsDialog.setBusy(false);
      }
    },

    onThemeSelectionChange: function (oEvent) {
      const items = oEvent.getSource().getItems();
      const selected = oEvent.getParameter("listItem");
      const idx = items.indexOf(selected);
      this._applyTheme(idx === 0 ? "sap_horizon" : "sap_horizon_dark");
    },

    /* ---------- nav ---------- */

    onNavItemSelect: function () { /* per-item handlers fire */ },

    onSelectHome: function () {
      this._uiModel.setProperty("/currentAppId", null);
      this._uiModel.setProperty("/currentAppUrl", null);
      this._uiModel.setProperty("/selectedNavKey", "home");
      this.byId("mainNav").to(this.byId("homePage"));
    },

    onSelectAuditLogs: function () {
      this._uiModel.setProperty("/currentAppId", null);
      this._uiModel.setProperty("/currentAppUrl", null);
      this._uiModel.setProperty("/selectedNavKey", "auditLogs");
      this.byId("mainNav").to(this.byId("auditLogsPage"));
    },

    onSelectAdmin: function () {
      this._uiModel.setProperty("/currentAppId", null);
      this._uiModel.setProperty("/currentAppUrl", null);
      this._uiModel.setProperty("/selectedNavKey", "administration");
      this.byId("mainNav").to(this.byId("adminPage"));
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

    /* ---------- audit logs ---------- */

    onAuditSearch: function () {
      MessageToast.show("Search functionality coming soon.");
    },

    onAuditReset: function () {
      var oView = this.getView();
      oView.byId("auditFilterDateRange").setValue("");
      oView.byId("auditFilterApp").setSelectedKey("");
      oView.byId("auditFilterModule").setSelectedKey("");
      oView.byId("auditFilterActionType").setSelectedKey("");
      oView.byId("auditFilterUser").setSelectedKey("");
      oView.byId("auditFilterEntity").setValue("");
      MessageToast.show("Filters reset.");
    },

    onAuditExport: function () {
      MessageToast.show("Export functionality coming soon.");
    },

    onAuditColumns: function () {
      MessageToast.show("Column configuration coming soon.");
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
