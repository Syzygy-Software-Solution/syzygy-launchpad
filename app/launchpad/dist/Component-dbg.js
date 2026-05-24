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
        sideExpanded: true,
        frameLoading: false,
        aiPanelOpen: false,
        theme: savedTheme,
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
        }
      }), "ui");
    }
  });
});
