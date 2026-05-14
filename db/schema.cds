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
