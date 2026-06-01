using { sz as db } from '../db/schema';

service LaunchpadService @(path: '/odata/v4/launchpad') {

  entity ConfiguredApps as projection on db.ConfiguredApps;

  action registerApp(
    appId: String,
    displayName: String,
    runtimeUrl: String,
    iconUri: String,
    appType: String,
    enabled: Boolean
  ) returns ConfiguredApps;

  action updateApp(
    appId: String,
    displayName: String,
    runtimeUrl: String,
    iconUri: String,
    enabled: Boolean
  ) returns ConfiguredApps;

  action unregisterApp(appId: String) returns Boolean;

  action toggleApp(appId: String, enabled: Boolean) returns Boolean;

  type CurrentUser {
    id        : String;
    email     : String;
    firstname : String;
    lastname  : String;
    name      : String;
  }

  function currentUser() returns CurrentUser;

  // Saves/updates the display name for the currently logged-in user.
  action updateUserProfile(firstname: String, lastname: String) returns CurrentUser;

  // ─── Security · Role & permission management ───────────────────────────────
  // CRUD over the SecurityRoles table used by the Administration › Security
  // screen. Reads happen through the projection; mutations go through actions
  // so we can protect the 22 seeded (isSystem) roles from deletion server-side.
  entity SecurityRoles as projection on db.SecurityRoles;

  action createSecurityRole(
    roleName    : String,
    department  : String,
    appId       : String,
    description : String,
    canRead     : Boolean,
    canWrite    : Boolean,
    canDelete   : Boolean,
    canApprove  : Boolean,
    canExecute  : Boolean
  ) returns SecurityRoles;

  action updateSecurityRole(
    ID          : UUID,
    roleName    : String,
    department  : String,
    appId       : String,
    description : String,
    canRead     : Boolean,
    canWrite    : Boolean,
    canDelete   : Boolean,
    canApprove  : Boolean,
    canExecute  : Boolean
  ) returns SecurityRoles;

  action deleteSecurityRole(ID: UUID) returns Boolean;
}
