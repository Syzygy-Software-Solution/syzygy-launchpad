sap.ui.define([
  "sap/ui/core/UIComponent",
  "sap/ui/model/json/JSONModel",
  "sap/ui/core/Theming"
], function (UIComponent, JSONModel, Theming) {
  "use strict";

  const THEME_KEY = "syzygyLaunchpadTheme";

  return UIComponent.extend("syzygylaunchpad.Component", {
    metadata: { manifest: "json" },

    init: function () {
      UIComponent.prototype.init.apply(this, arguments);

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
