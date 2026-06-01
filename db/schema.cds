namespace sz;

using { managed } from '@sap/cds/common';

entity ConfiguredApps : managed {
  key appId        : String(200);
      displayName  : String(200);
      appVersion   : String(50);
      iconUri      : String(200);                   // sap-icon://<name>
      runtimeUrl   : String(500);
      appType      : String(20) default 'btp';      // 'btp' | 'url'
      enabled      : Boolean default true;
      sortOrder    : Integer default 0;
}

// Stores user-supplied display name overrides (keyed by XSUAA user ID).
// XSUAA attributes are read-only; this table lets users personalise their
// name shown in the launchpad without touching IdP attributes.
entity UserProfile {
  key userId    : String(200);
      firstname : String(100);
      lastname  : String(100);
}

// ─────────────────────────────────────────────────────────────────────────────
// Security — role definitions and the CRUD-style permissions granted to them.
// A role can be scoped to a single onboarded BTP app (appId set) or left blank
// to act as a *common* role that applies across every app. The 22 seeded roles
// carry isSystem = true and cannot be deleted (their permissions stay editable);
// admins may also create additional custom roles.
// ─────────────────────────────────────────────────────────────────────────────
entity SecurityRoles : managed {
  key ID          : UUID;
      roleName    : String(100) @mandatory;
      department  : String(100);                  // hardcoded department list (UI)
      appId       : String(200);                  // FK→ConfiguredApps; null/'' ⇒ common role
      description : String(500);
      isSystem    : Boolean default false;        // true ⇒ one of the 22 fixed roles
      canRead     : Boolean default false;
      canWrite    : Boolean default false;
      canDelete   : Boolean default false;
      canApprove  : Boolean default false;
      canExecute  : Boolean default false;
}
