sap.ui.define([
  "sap/ui/core/UIComponent",
  "sap/ui/model/json/JSONModel",
  "sap/ui/model/odata/v4/ODataModel",
  "sap/ui/core/Theming"
], function (UIComponent, JSONModel, ODataModel, Theming) {
  "use strict";

  const THEME_KEY = "syzygyLaunchpadTheme";

  return UIComponent.extend("syzygylaunchpad.Component", {
    metadata: { manifest: "json" },

    init: function () {
      UIComponent.prototype.init.apply(this, arguments);

      // Replace the masterdata OData model with one that uses an absolute,
      // origin-rooted serviceUrl. The manifest dataSource URI is resolved
      // relative to the component (HTML5 app) path by UI5, which would
      // produce `/syzygylaunchpad/masterdata/` — that path is not routed by
      // the approuter and returns 404. Origin-rooted `/api/masterdata/` matches
      // the approuter route in app/router/xs-app.json. The /api/ prefix avoids
      // collision with the html5-apps-repo app named `masterdata`.
      this.setModel(new ODataModel({
        serviceUrl: window.location.origin + "/api/masterdata/",
        synchronizationMode: "None",
        operationMode: "Server",
        autoExpandSelect: true
      }), "masterdata");

      const savedTheme = (typeof localStorage !== 'undefined' && localStorage.getItem(THEME_KEY)) || "sap_horizon";
      Theming.setTheme(savedTheme);
      document.documentElement.setAttribute("data-sz-theme", savedTheme);

      this.setModel(new JSONModel({
        currentAppId: null,
        currentAppUrl: null,
        currentAppTitle: null,
        selectedNavKey: "home",
        sideExpanded: false,
        frameLoading: false,
        aiPanelOpen: false,
        aiPanelFull: false,
        theme: savedTheme,
        // AI Assistant chat state — kept in-memory only (per browser tab).
        // `messages` shape: [{ role: "user"|"assistant", content: "...", error?: bool }]
        aiChat: {
          messages: [],
          busy: false,
          input: "",
          model: "gpt-4.1",
          tokens: 0
        },
        user: {
          name: "John Smith",
          initials: "JS",
          role: "Admin",
          email: "",
          firstname: "",
          lastname: ""
        },
        settings: {
          section: "userAccount",
          firstname: "",
          lastname: ""
        },
        manage: {
          items: [],
          allItems: [],
          totalLabel: "0 total",
          configuredLabel: "0 enabled"
        },
        appForm: {
          isEdit: false,
          title: "Add New App",
          appId: "",
          displayName: "",
          runtimeUrl: "",
          iconUri: "sap-icon://grid",
          appType: "btp"
        },
        iconPicker: {
          all: [],
          filtered: []
        },
        audit: {
          dateFrom: null,        // Date object — defaults to today (00:00)
          dateTo: null,          // Date object — defaults to today (23:59)
          appFilter: "",
          moduleFilter: "",
          actionTypeFilter: "",
          userFilter: "",
          entityFilter: "",
          items: [],             // current page rows
          page: 1,
          pageSize: 100,
          totalCount: 0,
          totalPages: 1,
          pageInput: "1",
          busy: false,
          hasFirstSearch: false, // becomes true after the first successful fetch
          kpis: {
            total:        "0",
            dataChanges:  "0",
            statusChanges:"0",
            userActions:  "0",
            failedActions:"0",
            totalSub:        "This period",
            dataChangesSub:  "0% of total",
            statusChangesSub:"0% of total",
            userActionsSub:  "0% of total",
            failedActionsSub:"0% of total"
          }
        },
        admin: {
          // Dashboard overview KPIs (populated by GET /api/admin/getOverview())
          overview: {
            users:           "0",
            activeUsers:     "0",
            inactiveUsers:   "0",
            groups:          "0",
            applications:    "0",
            roleCollections: "0",
            activePercent:   "0%",
            iasHost:         "",
            iasOrigin:       "",
            healthy:         false,
            btpHealthy:      false,
            healthyText:     "Checking…",
            btpHealthyText:  "Checking…",
            error:           "",
            loaded:          false
          },
          // Users sub-page
          users: {
            search:  "",
            items:   [],
            total:   0,
            busy:    false,
            loaded:  false
          },
          // User detail sub-page
          userDetail: {
            id:               "",
            userName:         "",
            email:            "",
            firstName:        "",
            lastName:         "",
            active:           true,
            created:          "",
            groups:           [],          // IAS SCIM groups (kept for compatibility)
            roleCollections:  [],          // BTP role-collection assignments
            busy:             false
          },
          // Invite user dialog
          invite: {
            firstName:           "",
            lastName:            "",
            email:               "",
            active:              true,
            // BTP role-collection multi-select on the invite dialog
            availableRC:         [],       // [{name, description}]
            selectedRCKeys:      [],       // string[] of rc names
            busy:                false
          },
          // BTP Role Collections sub-page (replaces the IAS-groups Roles page)
          roleCollections: {
            search:  "",
            items:   [],                   // mapped RoleCollectionSummary[]
            total:   0,
            busy:    false,
            loaded:  false
          },
          // Role-collection detail sub-page
          roleCollectionDetail: {
            name:        "",
            description: "",
            isReadOnly:  false,
            roles:       [],               // [{name, applicationId, description, ...}]
            raw:         "",
            busy:        false
          },
          // Create role-collection dialog
          newRoleCollection: {
            name:        "",
            description: "",
            busy:        false
          },
          // Applications sub-page
          apps: {
            items:  [],
            total:  0,
            busy:   false,
            loaded: false
          },
          appDetail: {
            id:          "",
            displayName: "",
            raw:         "",
            busy:        false
          },
          // Sub-page breadcrumb title (e.g. "User Management")
          subTitle: "",
          // Security · role & permission management sub-page
          security: {
            search:        "",
            roles:         [],   // all roles loaded from the backend
            filteredRoles: [],   // roles after the search filter
            total:         0,
            filteredTotal: 0,
            deptFilter:    "",   // "" ⇒ all departments
            appFilter:     "__all__", // "__all__" | "__common__" | <appId>
            busy:          false,
            loaded:        false,
            // Onboarded BTP apps for the "Application scope" dropdown.
            // First entry is the "common role" sentinel (empty key).
            apps:          [{ appId: "", displayName: "Common — all apps" }],
            // Filter dropdown options (rebuilt as data loads).
            deptFilterOptions: [
              { key: "",                 text: "All departments" },
              { key: "Comp Admin",       text: "Comp Admin" },
              { key: "Sales",            text: "Sales" },
              { key: "Finance",          text: "Finance" },
              { key: "HR",               text: "HR" },
              { key: "IT",               text: "IT" },
              { key: "Sales Operations", text: "Sales Operations" }
            ],
            appFilterOptions: [
              { key: "__all__",    text: "All applications" },
              { key: "__common__", text: "Common (all apps)" }
            ],
            // Hardcoded department list (mirrors the source matrix).
            departments: [
              { key: "",                 text: "— None —" },
              { key: "Comp Admin",       text: "Comp Admin" },
              { key: "Sales",            text: "Sales" },
              { key: "Finance",          text: "Finance" },
              { key: "HR",               text: "HR" },
              { key: "IT",               text: "IT" },
              { key: "Sales Operations", text: "Sales Operations" }
            ],
            detail: {
              ID:           "",
              roleName:     "",
              department:   "",
              appId:        "",
              description:  "",
              isSystem:     false,
              canRead:      false,
              canWrite:     false,
              canDelete:    false,
              canApprove:   false,
              canExecute:   false,
              hasSelection: false,
              busy:         false
            },
            newRole: {
              roleName:    "",
              department:  "",
              appId:       "",
              description: "",
              busy:        false
            }
          }
        }
      }), "ui");
    }
  });
});
