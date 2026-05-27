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
  // Admin service — proxies SCIM / Applications calls to IAS via the approuter.
  // Matches the ^/api/admin/(.*)$ route in app/router/xs-app.json.
  const ADMIN_BASE = "/api/admin/";
  // Public URL of the IAS tenant’s admin console (deep-linked from tiles).
  const IAS_CONSOLE_URL = "https://a6cvqiuhb.accounts.ondemand.com/admin";
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
      this._initAuditDefaults();
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

    _collapseSideNav: function () {
      // Always collapse the side panel after navigating to any item so the
      // user gets maximum content area for the selected app/page.
      this._uiModel.setProperty("/sideExpanded", false);
    },

    onSelectHome: function () {
      this._uiModel.setProperty("/currentAppId", null);
      this._uiModel.setProperty("/currentAppUrl", null);
      this._uiModel.setProperty("/selectedNavKey", "home");
      this.byId("mainNav").to(this.byId("homePage"));
      this._collapseSideNav();
    },

    onSelectAuditLogs: function () {
      this._uiModel.setProperty("/currentAppId", null);
      this._uiModel.setProperty("/currentAppUrl", null);
      this._uiModel.setProperty("/selectedNavKey", "auditLogs");
      this.byId("mainNav").to(this.byId("auditLogsPage"));
      this._collapseSideNav();
      // Auto-load the first page (today's logs) the first time the user
      // opens the page in this session.
      if (!this._uiModel.getProperty("/audit/hasFirstSearch")) {
        this._fetchAuditData(1);
      }
    },

    onSelectAdmin: function () {
      this._uiModel.setProperty("/currentAppId", null);
      this._uiModel.setProperty("/currentAppUrl", null);
      this._uiModel.setProperty("/selectedNavKey", "administration");
      this.byId("mainNav").to(this.byId("adminPage"));
      this._collapseSideNav();
      // Load (or refresh) dashboard KPIs whenever the user lands on the
      // Administration home page.
      this._loadAdminOverview();
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
      this._collapseSideNav();
      this._loadAppIntoFrame(url, this._uiModel.getProperty("/theme"));
    },

    /* ---------- audit logs ---------- */

    _initAuditDefaults: function () {
      // Default the date range to "today" so the page always shows the
      // current day's audit trail on first open.
      const today = new Date();
      const from = new Date(today.getFullYear(), today.getMonth(), today.getDate(), 0, 0, 0, 0);
      const to   = new Date(today.getFullYear(), today.getMonth(), today.getDate(), 23, 59, 59, 999);
      this._uiModel.setProperty("/audit/dateFrom", from);
      this._uiModel.setProperty("/audit/dateTo",   to);
    },

    formatAuditTimestamp: function (sIso) {
      if (!sIso) return "";
      // Tolerate both Date objects and ISO strings from CAP/OData v4.
      const d = (sIso instanceof Date) ? sIso : new Date(sIso);
      if (isNaN(d.getTime())) return String(sIso);
      // Locale-aware, short numeric format with seconds.
      const datePart = d.toLocaleDateString(undefined, {
        year: "numeric", month: "short", day: "2-digit"
      });
      const timePart = d.toLocaleTimeString(undefined, {
        hour: "2-digit", minute: "2-digit", second: "2-digit", hour12: false
      });
      return `${datePart}, ${timePart}`;
    },

    _validateAuditRange: function () {
      const from = this._uiModel.getProperty("/audit/dateFrom");
      const to   = this._uiModel.getProperty("/audit/dateTo");
      if (!from || !to) {
        MessageBox.warning("Please choose a date range to load audit records. Date range is mandatory.");
        return null;
      }
      if (to < from) {
        MessageBox.warning("End date cannot be before the start date.");
        return null;
      }
      // 6 months max — count using calendar months for accuracy.
      const maxTo = new Date(from);
      maxTo.setMonth(maxTo.getMonth() + 6);
      if (to > maxTo) {
        MessageBox.warning("Date range cannot exceed 6 months. Please narrow your selection.");
        return null;
      }
      // Normalise to day boundaries so the filter is inclusive of full days.
      const fromDay = new Date(from.getFullYear(), from.getMonth(), from.getDate(), 0, 0, 0, 0);
      const toDay   = new Date(to.getFullYear(),   to.getMonth(),   to.getDate(),   23, 59, 59, 999);
      return { from: fromDay, to: toDay };
    },

    _buildAuditFilter: function (range, extraFilter) {
      const parts = [];
      parts.push(`EVENT_TIMESTAMP ge ${range.from.toISOString()}`);
      parts.push(`EVENT_TIMESTAMP le ${range.to.toISOString()}`);
      // Optional UI filters
      const app    = this._uiModel.getProperty("/audit/appFilter");
      const mod    = this._uiModel.getProperty("/audit/moduleFilter");
      const action = this._uiModel.getProperty("/audit/actionTypeFilter");
      const user   = this._uiModel.getProperty("/audit/userFilter");
      const entity = (this._uiModel.getProperty("/audit/entityFilter") || "").trim();
      if (app)    parts.push(`APPLICATION eq '${app.replace(/'/g, "''")}'`);
      if (mod)    parts.push(`MODULE eq '${mod.replace(/'/g, "''")}'`);
      if (action) parts.push(`ACTION_TYPE eq '${action.replace(/'/g, "''")}'`);
      if (user)   parts.push(`USER_ID eq '${user.replace(/'/g, "''")}'`);
      if (entity) {
        const q = entity.replace(/'/g, "''");
        parts.push(`(contains(ENTITY_NAME,'${q}') or contains(ENTITY_ID,'${q}'))`);
      }
      if (extraFilter) parts.push(`(${extraFilter})`);
      return parts.join(" and ");
    },

    _fetchAuditCount: async function (filter) {
      try {
        const url = `${MASTERDATA_BASE}AuditLogs/$count?$filter=${encodeURIComponent(filter)}`;
        const res = await fetch(window.location.origin + url, {
          headers: { Accept: "text/plain" },
          credentials: "include"
        });
        if (!res.ok) return 0;
        const txt = (await res.text()).trim();
        const n = parseInt(txt, 10);
        return isNaN(n) ? 0 : n;
      } catch (e) { return 0; }
    },

    _fetchAuditData: async function (page) {
      const range = this._validateAuditRange();
      if (!range) return;

      this._uiModel.setProperty("/audit/busy", true);
      try {
        const baseFilter = this._buildAuditFilter(range);

        // Category filters (excluding the date range that is already baked in)
        // Data changes: real CRUD on business data — exclude Authentication module
        const dataFilter   = `${baseFilter} and ACTION_TYPE in ('CREATE','UPDATE','DELETE') and MODULE ne 'Authentication'`;
        const statusFilter = `${baseFilter} and ACTION_TYPE eq 'STATUS_CHANGE'`;
        const userFilter   = `${baseFilter} and (ACTION_TYPE eq 'LOGIN' or ACTION_TYPE eq 'LOGOUT')`;
        const failFilter   = `${baseFilter} and STATUS ne 'Success'`;

        const pageSize = this._uiModel.getProperty("/audit/pageSize") || 100;
        const safePage = Math.max(1, parseInt(page, 10) || 1);
        const skip = (safePage - 1) * pageSize;

        const itemsUrl =
          `${MASTERDATA_BASE}AuditLogs` +
          `?$filter=${encodeURIComponent(baseFilter)}` +
          `&$orderby=EVENT_TIMESTAMP desc` +
          `&$top=${pageSize}&$skip=${skip}&$count=true`;

        const itemsPromise = fetch(window.location.origin + itemsUrl, {
          headers: { Accept: "application/json" },
          credentials: "include"
        }).then(r => r.ok ? r.json() : { value: [], "@odata.count": 0 });

        const [total, dataChanges, statusChanges, userActions, failedActions, itemsResp] = await Promise.all([
          this._fetchAuditCount(baseFilter),
          this._fetchAuditCount(dataFilter),
          this._fetchAuditCount(statusFilter),
          this._fetchAuditCount(userFilter),
          this._fetchAuditCount(failFilter),
          itemsPromise
        ]);

        const pct = (n) => total > 0 ? `${((n / total) * 100).toFixed(1)}% of total` : "0% of total";

        this._uiModel.setProperty("/audit/kpis", {
          total:            this._formatNumber(total),
          dataChanges:      this._formatNumber(dataChanges),
          statusChanges:    this._formatNumber(statusChanges),
          userActions:      this._formatNumber(userActions),
          failedActions:    this._formatNumber(failedActions),
          totalSub:         "Selected period",
          dataChangesSub:   pct(dataChanges),
          statusChangesSub: pct(statusChanges),
          userActionsSub:   pct(userActions),
          failedActionsSub: pct(failedActions)
        });

        const items = (itemsResp.value || []).map(r => ({
          EVENT_TIMESTAMP: r.EVENT_TIMESTAMP,
          APPLICATION:     r.APPLICATION || "",
          MODULE:          r.MODULE || "",
          ENTITY_NAME:     r.ENTITY_NAME || "",
          ENTITY_ID:       r.ENTITY_ID || "",
          ACTION_TYPE:     r.ACTION_TYPE || "",
          FIELD_NAME:      r.FIELD_NAME || "",
          USER_NAME:       r.USER_NAME || r.USER_ID || "",
          STATUS:          r.STATUS || ""
        }));

        const totalRows = typeof itemsResp["@odata.count"] === "number" ? itemsResp["@odata.count"] : total;
        const totalPages = Math.max(1, Math.ceil(totalRows / pageSize));
        const currentPage = Math.min(safePage, totalPages);

        this._uiModel.setProperty("/audit/items", items);
        this._uiModel.setProperty("/audit/page", currentPage);
        this._uiModel.setProperty("/audit/pageInput", String(currentPage));
        this._uiModel.setProperty("/audit/totalCount", totalRows);
        this._uiModel.setProperty("/audit/totalPages", totalPages);
        this._uiModel.setProperty("/audit/hasFirstSearch", true);
      } catch (err) {
        MessageBox.error("Could not load audit logs.\n" + (err && err.message ? err.message : err));
      } finally {
        this._uiModel.setProperty("/audit/busy", false);
      }
    },

    _formatNumber: function (n) {
      try { return (n || 0).toLocaleString(); } catch (e) { return String(n || 0); }
    },

    onAuditDateRangeChange: function () {
      // Live re-fetch when the user changes the date range — but only if
      // both ends are set; bail silently otherwise (validation happens on
      // explicit Search and during the fetch itself).
      const from = this._uiModel.getProperty("/audit/dateFrom");
      const to   = this._uiModel.getProperty("/audit/dateTo");
      if (!from || !to) {
        MessageBox.warning("Please choose a date range to load audit records. Date range is mandatory.");
        return;
      }
      this._fetchAuditData(1);
    },

    onAuditSearch: function () {
      this._fetchAuditData(1);
    },

    onAuditReset: function () {
      this._uiModel.setProperty("/audit/appFilter", "");
      this._uiModel.setProperty("/audit/moduleFilter", "");
      this._uiModel.setProperty("/audit/actionTypeFilter", "");
      this._uiModel.setProperty("/audit/userFilter", "");
      this._uiModel.setProperty("/audit/entityFilter", "");
      this._initAuditDefaults();
      this._fetchAuditData(1);
      MessageToast.show("Filters reset.");
    },

    onAuditExport: function () {
      MessageToast.show("Export functionality coming soon.");
    },

    onAuditColumns: function () {
      MessageToast.show("Column configuration coming soon.");
    },

    /* ---- pagination ---- */

    onAuditPageFirst: function () { this._gotoPage(1); },
    onAuditPagePrev:  function () { this._gotoPage((this._uiModel.getProperty("/audit/page") || 1) - 1); },
    onAuditPageNext:  function () { this._gotoPage((this._uiModel.getProperty("/audit/page") || 1) + 1); },
    onAuditPageLast:  function () { this._gotoPage(this._uiModel.getProperty("/audit/totalPages") || 1); },
    onAuditPageGoto:  function () {
      const raw = this._uiModel.getProperty("/audit/pageInput");
      const n = parseInt(raw, 10);
      if (isNaN(n)) {
        MessageToast.show("Enter a valid page number.");
        return;
      }
      this._gotoPage(n);
    },

    _gotoPage: function (page) {
      const total = this._uiModel.getProperty("/audit/totalPages") || 1;
      const cur   = this._uiModel.getProperty("/audit/page") || 1;
      const next  = Math.max(1, Math.min(total, page));
      if (next === cur) return;
      this._fetchAuditData(next);
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

    /* ═════════════════════════════════════════════════════════════════════
       ADMINISTRATION — Users / Roles / Applications
       All endpoints go through /api/admin/ which the approuter proxies to
       the AdminService (CAP) which in turn calls SAP Cloud Identity Services.
       ═════════════════════════════════════════════════════════════════════ */

    /* ---------- generic helper for admin actions ---------- */

    _callAdmin: async function (name, payload, opts) {
      const o = opts || {};
      const url = window.location.origin + ADMIN_BASE + name;
      const isGet = !!o.get;
      let res;
      try {
        res = await fetch(url, {
          method: isGet ? "GET" : "POST",
          headers: isGet
            ? { Accept: "application/json" }
            : { "Content-Type": "application/json", Accept: "application/json" },
          body: isGet ? undefined : JSON.stringify(payload || {}),
          credentials: "include"
        });
      } catch (e) {
        throw new Error(`Network error calling ${name}: ${e.message}`);
      }
      if (!res.ok) {
        let msg = `${res.status} ${res.statusText}`;
        try {
          const t = await res.text();
          if (t) {
            try { msg = JSON.parse(t).error?.message || t; } catch { msg = t; }
          }
        } catch {}
        throw new Error(msg);
      }
      const ct = res.headers.get("content-type") || "";
      if (res.status === 204 || !ct.includes("application/json")) return null;
      return res.json();
    },

    /* ---------- Admin Home ---------- */

    onAdminRefresh: function () {
      this._loadAdminOverview(true);
    },

    _loadAdminOverview: async function (bForce) {
      const ov = this._uiModel.getProperty("/admin/overview");
      if (ov && ov.loaded && !bForce) {
        // Refresh in the background but don't block the UI
      }
      try {
        const data = await this._callAdmin("getOverview()", null, { get: true });
        // CAP wraps function results in { value: ... }
        const v = (data && data.value) || data || {};
        const total  = Number(v.users) || 0;
        const active = Number(v.activeUsers) || 0;
        const pct    = total > 0 ? Math.round((active / total) * 100) + "%" : "0%";
        this._uiModel.setProperty("/admin/overview", {
          users:           String(v.users || 0),
          activeUsers:     String(v.activeUsers || 0),
          inactiveUsers:   String(v.inactiveUsers || 0),
          groups:          String(v.groups || 0),
          applications:    String(v.applications || 0),
          roleCollections: String(v.roleCollections || 0),
          activePercent:   pct,
          iasHost:         v.iasHost || "",
          iasOrigin:       v.iasOrigin || "",
          healthy:         !!v.healthy,
          btpHealthy:      !!v.btpHealthy,
          healthyText:     v.healthy ? "Healthy" : "Unavailable",
          btpHealthyText:  v.btpHealthy ? "Healthy" : "Unavailable",
          error:           v.error || "",
          loaded:          true
        });
      } catch (e) {
        this._uiModel.setProperty("/admin/overview", {
          users: "0", activeUsers: "0", inactiveUsers: "0", groups: "0", applications: "0",
          roleCollections: "0",
          activePercent: "0%",
          iasHost: "",
          iasOrigin: "",
          healthy: false,
          btpHealthy: false,
          healthyText: "Unavailable",
          btpHealthyText: "Unavailable",
          error: e.message,
          loaded: true
        });
      }
    },

    /* ---------- Tile presses ---------- */

    onAdminTileUsers: function () {
      this.byId("mainNav").to(this.byId("adminUsersPage"));
      this._loadAdminUsers();
    },

    onAdminTileRoles: function () {
      this.byId("mainNav").to(this.byId("adminRolesPage"));
      this._loadAdminRoleCollections();
    },

    onAdminTileApps: function () {
      this.byId("mainNav").to(this.byId("adminAppsPage"));
      this._loadAdminApps();
    },

    onAdminTileAuditLogs: function () {
      // Reuse existing audit-logs handler so the side-nav and state stay in sync.
      this._uiModel.setProperty("/selectedNavKey", "auditLogs");
      this.onSelectAuditLogs();
    },

    onAdminTileIasConsole: function () {
      window.open(IAS_CONSOLE_URL, "_blank", "noopener");
    },

    onAdminTilePasswordPolicy: function () {
      MessageToast.show("Password policy management is coming soon. Use the IAS console for now.");
      window.open(IAS_CONSOLE_URL + "/passwordPolicies", "_blank", "noopener");
    },

    onAdminTileEmailTemplates: function () {
      MessageToast.show("Email template management is coming soon. Use the IAS console for now.");
      window.open(IAS_CONSOLE_URL + "/emailTemplates", "_blank", "noopener");
    },

    onAdminTileSystemConfig: function () {
      MessageToast.show("System configuration screen is coming soon.");
    },

    /* ---------- Breadcrumb navigation ---------- */

    onAdminBackToHome: function () {
      this.byId("mainNav").to(this.byId("adminPage"));
    },
    onAdminBackToUsers: function () {
      this.byId("mainNav").to(this.byId("adminUsersPage"));
    },
    onAdminBackToRoles: function () {
      this.byId("mainNav").to(this.byId("adminRolesPage"));
    },

    /* ---------- Quick actions from Admin Home ---------- */

    onAdminInviteUserFromHome: async function () {
      if (!this._uiModel.getProperty("/admin/roleCollections/loaded")) {
        this._loadAdminRoleCollections();
      }
      this.onAdminOpenInviteDialog();
    },

    onAdminCreateRoleFromHome: function () {
      this.onAdminOpenCreateRoleCollectionDialog();
    },

    onAdminCreateRoleCollectionFromHome: function () {
      this.onAdminOpenCreateRoleCollectionDialog();
    },

    /* ─────────────────────────────────────────────────────────────────────
       USER MANAGEMENT
       ───────────────────────────────────────────────────────────────────── */

    _loadAdminUsers: async function () {
      this._uiModel.setProperty("/admin/users/busy", true);
      try {
        const filter = (this._uiModel.getProperty("/admin/users/search") || "").trim();
        // SCIM filter syntax: `userName co "foo" or emails co "foo"`
        const qs = filter
          ? `(filter='${encodeURIComponent(`userName co "${filter}" or emails co "${filter}"`)}')`
          : "()";
        const data = await this._callAdmin("listUsers" + qs, null, { get: true });
        const v = (data && data.value) || data || {};
        this._uiModel.setProperty("/admin/users/items", v.items || []);
        this._uiModel.setProperty("/admin/users/total", v.totalResults || (v.items || []).length);
        this._uiModel.setProperty("/admin/users/loaded", true);
      } catch (e) {
        MessageBox.error("Failed to load users: " + e.message);
      } finally {
        this._uiModel.setProperty("/admin/users/busy", false);
      }
    },

    onAdminReloadUsers: function () { this._loadAdminUsers(); },

    onAdminUserSearch: function () {
      // Debounce — 300 ms after the user stops typing.
      if (this._userSearchTimer) clearTimeout(this._userSearchTimer);
      this._userSearchTimer = setTimeout(() => this._loadAdminUsers(), 300);
    },

    onAdminUserOpen: function (oEvent) {
      const ctx = oEvent.getSource().getBindingContext("ui");
      if (!ctx) return;
      const u = ctx.getObject();
      this._uiModel.setProperty("/admin/userDetail", {
        id: u.id, userName: u.userName, email: u.email,
        firstName: u.firstName, lastName: u.lastName,
        active: u.active, created: u.created,
        groups: [], roleCollections: [], busy: true
      });
      // Make sure role collections are loaded so the assign dropdown has options.
      if (!this._uiModel.getProperty("/admin/roleCollections/loaded")) {
        this._loadAdminRoleCollections();
      }
      this.byId("mainNav").to(this.byId("adminUserDetailPage"));
      this._loadAdminUserDetail(u.id);
      this._loadAdminUserRoleCollections(u.userName || u.email);
    },

    _loadAdminUserDetail: async function (sId) {
      try {
        const data = await this._callAdmin("getUserDetails(id='" + encodeURIComponent(sId) + "')", null, { get: true });
        const raw = (data && data.value) || data || "{}";
        const user = typeof raw === "string" ? JSON.parse(raw) : raw;
        const groups = (user.groups || []).map(g => ({
          value:   g.value,
          display: g.display || g.value
        }));
        this._uiModel.setProperty("/admin/userDetail/groups", groups);
      } catch (e) {
        MessageBox.error("Failed to load user details: " + e.message);
      } finally {
        this._uiModel.setProperty("/admin/userDetail/busy", false);
      }
    },

    onAdminUserDetailSave: async function () {
      const u = this._uiModel.getProperty("/admin/userDetail");
      if (!u.id) return;
      this._uiModel.setProperty("/admin/userDetail/busy", true);
      try {
        await this._callAdmin("updateUserProfile", {
          id: u.id, firstName: u.firstName, lastName: u.lastName, email: u.email
        });
        MessageToast.show("User updated");
        this._loadAdminUsers();
      } catch (e) {
        MessageBox.error("Failed to update user: " + e.message);
      } finally {
        this._uiModel.setProperty("/admin/userDetail/busy", false);
      }
    },

    onAdminUserDetailToggleActive: async function () {
      const u = this._uiModel.getProperty("/admin/userDetail");
      if (!u.id) return;
      this._uiModel.setProperty("/admin/userDetail/busy", true);
      try {
        await this._callAdmin("setUserActive", { id: u.id, active: !u.active });
        this._uiModel.setProperty("/admin/userDetail/active", !u.active);
        MessageToast.show(!u.active ? "User activated" : "User deactivated");
        this._loadAdminUsers();
      } catch (e) {
        MessageBox.error(e.message);
      } finally {
        this._uiModel.setProperty("/admin/userDetail/busy", false);
      }
    },

    onAdminUserToggleActive: async function (oEvent) {
      const ctx = oEvent.getSource().getBindingContext("ui");
      if (!ctx) return;
      const u = ctx.getObject();
      try {
        await this._callAdmin("setUserActive", { id: u.id, active: !u.active });
        MessageToast.show(!u.active ? "User activated" : "User deactivated");
        this._loadAdminUsers();
        this._loadAdminOverview(true);
      } catch (e) {
        MessageBox.error(e.message);
      }
    },

    onAdminUserDelete: function (oEvent) {
      const ctx = oEvent.getSource().getBindingContext("ui");
      if (!ctx) return;
      const u = ctx.getObject();
      const label = (u.firstName + " " + u.lastName).trim() || u.userName;
      MessageBox.confirm(
        `Delete user "${label}"?\n\nThis permanently removes the user from your IAS tenant. This action cannot be undone.`,
        {
          icon: MessageBox.Icon.WARNING,
          title: "Delete User",
          actions: [MessageBox.Action.DELETE, MessageBox.Action.CANCEL],
          emphasizedAction: MessageBox.Action.DELETE,
          onClose: async (action) => {
            if (action !== MessageBox.Action.DELETE) return;
            try {
              await this._callAdmin("deleteUser", { id: u.id });
              MessageToast.show("User deleted");
              this._loadAdminUsers();
              this._loadAdminOverview(true);
            } catch (e) {
              MessageBox.error(e.message);
            }
          }
        }
      );
    },

    /* ---------- Invite User dialog ---------- */

    onAdminOpenInviteDialog: async function () {
      // Reset form fields each time the dialog opens.
      this._uiModel.setProperty("/admin/invite", {
        firstName: "", lastName: "", email: "", active: true,
        availableRC: [], selectedRCKeys: [], busy: false
      });
      // Make sure role-collection list is loaded for the multi-select.
      if (!this._uiModel.getProperty("/admin/roleCollections/loaded")) {
        this._loadAdminRoleCollections();
      }
      if (!this._adminInviteDialog) {
        this._adminInviteDialog = await Fragment.load({
          id:         this.getView().getId(),
          name:       "syzygylaunchpad.view.AdminInviteUserDialog",
          controller: this
        });
        this.getView().addDependent(this._adminInviteDialog);
      }
      this._adminInviteDialog.open();
    },

    onAdminInviteUserCancel: function () { this._adminInviteDialog.close(); },

    onAdminInviteUserSubmit: async function () {
      const v = this._uiModel.getProperty("/admin/invite");
      if (!v.email || !/^[^@\s]+@[^@\s]+\.[^@\s]+$/.test(v.email)) {
        MessageBox.warning("Please enter a valid email address.");
        return;
      }
      this._uiModel.setProperty("/admin/invite/busy", true);
      try {
        const rcKeys = v.selectedRCKeys || [];
        const resp = await this._callAdmin("inviteUser", {
          userName:        v.email,
          email:           v.email,
          firstName:       v.firstName,
          lastName:        v.lastName,
          active:          v.active,
          roleCollections: rcKeys
        });
        // Response is a JSON string from CAP: { user, assignmentErrors }
        const raw = (resp && resp.value) || resp || "{}";
        let parsed = {};
        try { parsed = typeof raw === "string" ? JSON.parse(raw) : raw; } catch { parsed = {}; }
        const errs = parsed.assignmentErrors || [];
        this._adminInviteDialog.close();
        if (errs.length) {
          const lines = errs.map(e => `• ${e.roleCollection}: ${e.error}`).join("\n");
          MessageBox.warning(`User created in IAS, but some role-collection assignments failed:\n\n${lines}`);
        } else if (rcKeys.length) {
          MessageToast.show(`User invited and assigned to ${rcKeys.length} role collection(s)`);
        } else {
          MessageToast.show("User invited");
        }
        this._loadAdminUsers();
        this._loadAdminOverview(true);
      } catch (e) {
        MessageBox.error("Failed to invite user: " + e.message);
      } finally {
        this._uiModel.setProperty("/admin/invite/busy", false);
      }
    },

    /* ---------- Group membership (on user detail page) ---------- */

    onAdminUserAddToGroup: async function () {
      const sel = this.byId("adminUserAddGroupSelect");
      const groupId = sel && sel.getSelectedKey();
      const userId  = this._uiModel.getProperty("/admin/userDetail/id");
      if (!groupId) { MessageToast.show("Pick a group first"); return; }
      if (!userId)  return;
      try {
        await this._callAdmin("addUserToGroup", { userId, groupId });
        MessageToast.show("Added to group");
        this._loadAdminUserDetail(userId);
      } catch (e) { MessageBox.error(e.message); }
    },

    onAdminUserRemoveFromGroup: async function (oEvent) {
      const ctx = oEvent.getSource().getBindingContext("ui");
      if (!ctx) return;
      const g = ctx.getObject();
      const userId = this._uiModel.getProperty("/admin/userDetail/id");
      if (!userId || !g.value) return;
      try {
        await this._callAdmin("removeUserFromGroup", { userId, groupId: g.value });
        MessageToast.show("Removed from group");
        this._loadAdminUserDetail(userId);
      } catch (e) { MessageBox.error(e.message); }
    },

    /* ─────────────────────────────────────────────────────────────────────
       ROLE / GROUP MANAGEMENT
       ───────────────────────────────────────────────────────────────────── */

    _loadAdminRoles: async function () {
      this._uiModel.setProperty("/admin/roles/busy", true);
      try {
        const filter = (this._uiModel.getProperty("/admin/roles/search") || "").trim();
        const qs = filter
          ? `(filter='${encodeURIComponent(`displayName co "${filter}"`)}')`
          : "()";
        const data = await this._callAdmin("listGroups" + qs, null, { get: true });
        const v = (data && data.value) || data || {};
        this._uiModel.setProperty("/admin/roles/items", v.items || []);
        this._uiModel.setProperty("/admin/roles/total", v.totalResults || (v.items || []).length);
        this._uiModel.setProperty("/admin/roles/loaded", true);
      } catch (e) {
        MessageBox.error("Failed to load roles: " + e.message);
      } finally {
        this._uiModel.setProperty("/admin/roles/busy", false);
      }
    },

    onAdminReloadRoles: function () { this._loadAdminRoles(); },

    onAdminRoleSearch: function () {
      if (this._roleSearchTimer) clearTimeout(this._roleSearchTimer);
      this._roleSearchTimer = setTimeout(() => this._loadAdminRoles(), 300);
    },

    onAdminRoleOpen: function (oEvent) {
      const ctx = oEvent.getSource().getBindingContext("ui");
      if (!ctx) return;
      const g = ctx.getObject();
      this._uiModel.setProperty("/admin/roleDetail", {
        id:          g.id,
        displayName: g.displayName,
        description: g.description,
        members:     [],
        busy:        true
      });
      // Make sure users are loaded for the "Add member" dropdown.
      if (!this._uiModel.getProperty("/admin/users/loaded")) {
        this._loadAdminUsers();
      }
      this.byId("mainNav").to(this.byId("adminRoleDetailPage"));
      this._loadAdminRoleDetail(g.id);
    },

    _loadAdminRoleDetail: async function (sId) {
      try {
        const data = await this._callAdmin("getGroupDetails(id='" + encodeURIComponent(sId) + "')", null, { get: true });
        const raw  = (data && data.value) || data || "{}";
        const group = typeof raw === "string" ? JSON.parse(raw) : raw;
        const members = (group.members || []).map(m => ({
          value:   m.value,
          display: m.display || m.value
        }));
        this._uiModel.setProperty("/admin/roleDetail/members", members);
      } catch (e) {
        MessageBox.error("Failed to load role details: " + e.message);
      } finally {
        this._uiModel.setProperty("/admin/roleDetail/busy", false);
      }
    },

    onAdminRoleSave: async function () {
      const g = this._uiModel.getProperty("/admin/roleDetail");
      if (!g.id) return;
      this._uiModel.setProperty("/admin/roleDetail/busy", true);
      try {
        await this._callAdmin("updateGroup", {
          id: g.id, displayName: g.displayName, description: g.description
        });
        MessageToast.show("Role updated");
        this._loadAdminRoles();
      } catch (e) {
        MessageBox.error(e.message);
      } finally {
        this._uiModel.setProperty("/admin/roleDetail/busy", false);
      }
    },

    onAdminRoleDelete: function (oEvent) {
      const ctx = oEvent.getSource().getBindingContext("ui");
      if (!ctx) return;
      const g = ctx.getObject();
      MessageBox.confirm(
        `Delete role "${g.displayName}"?\n\nMembers of this group will lose any access granted by it. This action cannot be undone.`,
        {
          icon: MessageBox.Icon.WARNING,
          title: "Delete Role",
          actions: [MessageBox.Action.DELETE, MessageBox.Action.CANCEL],
          emphasizedAction: MessageBox.Action.DELETE,
          onClose: async (action) => {
            if (action !== MessageBox.Action.DELETE) return;
            try {
              await this._callAdmin("deleteGroup", { id: g.id });
              MessageToast.show("Role deleted");
              this._loadAdminRoles();
              this._loadAdminOverview(true);
            } catch (e) { MessageBox.error(e.message); }
          }
        }
      );
    },

    /* ---------- Create Role dialog ---------- */

    onAdminOpenCreateRoleDialog: async function () {
      this._uiModel.setProperty("/admin/newRole", { displayName: "", description: "", busy: false });
      if (!this._adminCreateRoleDialog) {
        this._adminCreateRoleDialog = await Fragment.load({
          id:         this.getView().getId(),
          name:       "syzygylaunchpad.view.AdminCreateRoleDialog",
          controller: this
        });
        this.getView().addDependent(this._adminCreateRoleDialog);
      }
      this._adminCreateRoleDialog.open();
    },

    onAdminCreateRoleCancel: function () { this._adminCreateRoleDialog.close(); },

    onAdminCreateRoleSubmit: async function () {
      const v = this._uiModel.getProperty("/admin/newRole");
      if (!v.displayName || !v.displayName.trim()) {
        MessageBox.warning("Role name is required.");
        return;
      }
      this._uiModel.setProperty("/admin/newRole/busy", true);
      try {
        await this._callAdmin("createGroup", { displayName: v.displayName.trim(), description: v.description || "" });
        MessageToast.show("Role created");
        this._adminCreateRoleDialog.close();
        this._loadAdminRoles();
        this._loadAdminOverview(true);
      } catch (e) {
        MessageBox.error("Failed to create role: " + e.message);
      } finally {
        this._uiModel.setProperty("/admin/newRole/busy", false);
      }
    },

    /* ---------- Role members ---------- */

    onAdminRoleAddMember: async function () {
      const sel = this.byId("adminRoleAddMemberSelect");
      const userId  = sel && sel.getSelectedKey();
      const groupId = this._uiModel.getProperty("/admin/roleDetail/id");
      if (!userId)  { MessageToast.show("Pick a user first"); return; }
      if (!groupId) return;
      try {
        await this._callAdmin("addUserToGroup", { userId, groupId });
        MessageToast.show("Member added");
        this._loadAdminRoleDetail(groupId);
      } catch (e) { MessageBox.error(e.message); }
    },

    onAdminRoleRemoveMember: async function (oEvent) {
      const ctx = oEvent.getSource().getBindingContext("ui");
      if (!ctx) return;
      const m = ctx.getObject();
      const groupId = this._uiModel.getProperty("/admin/roleDetail/id");
      if (!groupId || !m.value) return;
      try {
        await this._callAdmin("removeUserFromGroup", { userId: m.value, groupId });
        MessageToast.show("Member removed");
        this._loadAdminRoleDetail(groupId);
      } catch (e) { MessageBox.error(e.message); }
    },

    /* ─────────────────────────────────────────────────────────────────────
       BTP ROLE COLLECTIONS
       ───────────────────────────────────────────────────────────────────── */

    _loadAdminRoleCollections: async function () {
      this._uiModel.setProperty("/admin/roleCollections/busy", true);
      try {
        const data = await this._callAdmin("listRoleCollections()", null, { get: true });
        const v = (data && data.value) || data || {};
        let items = v.items || [];
        const search = (this._uiModel.getProperty("/admin/roleCollections/search") || "").trim().toLowerCase();
        if (search) {
          items = items.filter(rc =>
            (rc.name || "").toLowerCase().includes(search) ||
            (rc.description || "").toLowerCase().includes(search));
        }
        this._uiModel.setProperty("/admin/roleCollections/items", items);
        this._uiModel.setProperty("/admin/roleCollections/total", v.totalResults || items.length);
        this._uiModel.setProperty("/admin/roleCollections/loaded", true);
      } catch (e) {
        MessageBox.error("Failed to load role collections: " + e.message);
      } finally {
        this._uiModel.setProperty("/admin/roleCollections/busy", false);
      }
    },

    onAdminReloadRoleCollections: function () { this._loadAdminRoleCollections(); },

    onAdminRoleCollectionSearch: function () {
      if (this._rcSearchTimer) clearTimeout(this._rcSearchTimer);
      this._rcSearchTimer = setTimeout(() => this._loadAdminRoleCollections(), 300);
    },

    onAdminRoleCollectionOpen: function (oEvent) {
      const ctx = oEvent.getSource().getBindingContext("ui");
      if (!ctx) return;
      const rc = ctx.getObject();
      this._uiModel.setProperty("/admin/roleCollectionDetail", {
        name:        rc.name,
        description: rc.description,
        isReadOnly:  !!rc.isReadOnly,
        roles:       [],
        raw:         "",
        busy:        true
      });
      this.byId("mainNav").to(this.byId("adminRoleDetailPage"));
      this._loadAdminRoleCollectionDetail(rc.name);
    },

    _loadAdminRoleCollectionDetail: async function (sName) {
      try {
        const data = await this._callAdmin(
          "getRoleCollectionDetails(name='" + encodeURIComponent(sName) + "')",
          null, { get: true });
        const raw = (data && data.value) || data || "{}";
        const rc  = typeof raw === "string" ? JSON.parse(raw) : raw;
        const roles = (rc.roleReferences || []).map(r => ({
          name:          r.name || "",
          applicationId: r.applicationIdentifier || r.applicationId || "",
          description:   r.description || ""
        }));
        this._uiModel.setProperty("/admin/roleCollectionDetail/description", rc.description || "");
        this._uiModel.setProperty("/admin/roleCollectionDetail/isReadOnly",  rc.isReadOnly === true);
        this._uiModel.setProperty("/admin/roleCollectionDetail/roles",       roles);
        this._uiModel.setProperty("/admin/roleCollectionDetail/raw",         typeof raw === "string" ? raw : JSON.stringify(rc, null, 2));
      } catch (e) {
        MessageBox.error("Failed to load role collection: " + e.message);
      } finally {
        this._uiModel.setProperty("/admin/roleCollectionDetail/busy", false);
      }
    },

    onAdminRoleCollectionDelete: function (oEvent) {
      const ctx = oEvent.getSource().getBindingContext("ui");
      if (!ctx) return;
      const rc = ctx.getObject();
      if (rc.isReadOnly) {
        MessageBox.warning("This is a built-in role collection and cannot be deleted.");
        return;
      }
      MessageBox.confirm(
        `Delete role collection "${rc.name}"?\n\nAll users currently assigned to it will lose the access it grants. This cannot be undone.`,
        {
          icon: MessageBox.Icon.WARNING,
          title: "Delete Role Collection",
          actions: [MessageBox.Action.DELETE, MessageBox.Action.CANCEL],
          emphasizedAction: MessageBox.Action.DELETE,
          onClose: async (action) => {
            if (action !== MessageBox.Action.DELETE) return;
            try {
              await this._callAdmin("deleteRoleCollection", { name: rc.name });
              MessageToast.show("Role collection deleted");
              this._loadAdminRoleCollections();
              this._loadAdminOverview(true);
            } catch (e) { MessageBox.error(e.message); }
          }
        }
      );
    },

    /* ---------- Create Role Collection dialog ---------- */

    onAdminOpenCreateRoleCollectionDialog: async function () {
      this._uiModel.setProperty("/admin/newRoleCollection", { name: "", description: "", busy: false });
      if (!this._adminCreateRCDialog) {
        this._adminCreateRCDialog = await Fragment.load({
          id:         this.getView().getId(),
          name:       "syzygylaunchpad.view.AdminCreateRoleCollectionDialog",
          controller: this
        });
        this.getView().addDependent(this._adminCreateRCDialog);
      }
      this._adminCreateRCDialog.open();
    },

    onAdminCreateRoleCollectionCancel: function () { this._adminCreateRCDialog.close(); },

    onAdminCreateRoleCollectionSubmit: async function () {
      const v = this._uiModel.getProperty("/admin/newRoleCollection");
      if (!v.name || !v.name.trim()) {
        MessageBox.warning("Role collection name is required.");
        return;
      }
      this._uiModel.setProperty("/admin/newRoleCollection/busy", true);
      try {
        await this._callAdmin("createRoleCollection", {
          name:        v.name.trim(),
          description: v.description || ""
        });
        MessageToast.show("Role collection created");
        this._adminCreateRCDialog.close();
        this._loadAdminRoleCollections();
        this._loadAdminOverview(true);
      } catch (e) {
        MessageBox.error("Failed to create role collection: " + e.message);
      } finally {
        this._uiModel.setProperty("/admin/newRoleCollection/busy", false);
      }
    },

    /* ---------- User ↔ Role Collection assignment ---------- */

    _loadAdminUserRoleCollections: async function (sUserName) {
      if (!sUserName) {
        this._uiModel.setProperty("/admin/userDetail/roleCollections", []);
        return;
      }
      try {
        const data = await this._callAdmin(
          "getUserRoleCollections(userName='" + encodeURIComponent(sUserName) + "')",
          null, { get: true });
        const v = (data && data.value) || data || {};
        this._uiModel.setProperty("/admin/userDetail/roleCollections", v.items || []);
      } catch (e) {
        // Don't error-toast — common case is no shadow user yet.
        this._uiModel.setProperty("/admin/userDetail/roleCollections", []);
      }
    },

    onAdminUserAssignRC: async function () {
      const sel = this.byId("adminUserAddRCSelect");
      const rcName  = sel && sel.getSelectedKey();
      const userName = this._uiModel.getProperty("/admin/userDetail/userName")
                    || this._uiModel.getProperty("/admin/userDetail/email");
      if (!rcName)   { MessageToast.show("Pick a role collection first"); return; }
      if (!userName) return;
      this._uiModel.setProperty("/admin/userDetail/busy", true);
      try {
        await this._callAdmin("assignRoleCollection", { userName, roleCollectionName: rcName });
        MessageToast.show("Role collection assigned");
        this._loadAdminUserRoleCollections(userName);
      } catch (e) {
        MessageBox.error("Failed to assign role collection: " + e.message);
      } finally {
        this._uiModel.setProperty("/admin/userDetail/busy", false);
      }
    },

    onAdminUserUnassignRC: async function (oEvent) {
      const ctx = oEvent.getSource().getBindingContext("ui");
      if (!ctx) return;
      const rc = ctx.getObject();
      const userName = this._uiModel.getProperty("/admin/userDetail/userName")
                    || this._uiModel.getProperty("/admin/userDetail/email");
      if (!userName || !rc.name) return;
      this._uiModel.setProperty("/admin/userDetail/busy", true);
      try {
        await this._callAdmin("unassignRoleCollection", { userName, roleCollectionName: rc.name });
        MessageToast.show("Role collection removed");
        this._loadAdminUserRoleCollections(userName);
      } catch (e) {
        MessageBox.error("Failed to remove role collection: " + e.message);
      } finally {
        this._uiModel.setProperty("/admin/userDetail/busy", false);
      }
    },

    /* ─────────────────────────────────────────────────────────────────────
       APPLICATION ACCESS
       ───────────────────────────────────────────────────────────────────── */

    _loadAdminApps: async function () {
      this._uiModel.setProperty("/admin/apps/busy", true);
      try {
        const data = await this._callAdmin("listApplications()", null, { get: true });
        const v = (data && data.value) || data || {};
        this._uiModel.setProperty("/admin/apps/items", v.items || []);
        this._uiModel.setProperty("/admin/apps/total", v.totalResults || (v.items || []).length);
        this._uiModel.setProperty("/admin/apps/loaded", true);
      } catch (e) {
        MessageBox.error("Failed to load applications: " + e.message);
      } finally {
        this._uiModel.setProperty("/admin/apps/busy", false);
      }
    },

    onAdminReloadApps: function () { this._loadAdminApps(); },

    onAdminAppOpen: async function (oEvent) {
      const ctx = oEvent.getSource().getBindingContext("ui");
      if (!ctx) return;
      const a = ctx.getObject();
      try {
        const data = await this._callAdmin(
          "getApplicationDetails(id='" + encodeURIComponent(a.id) + "')",
          null,
          { get: true }
        );
        const raw = (data && data.value) || data || "{}";
        MessageBox.information(raw, { title: a.displayName, contentWidth: "640px" });
      } catch (e) { MessageBox.error(e.message); }
    },

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
