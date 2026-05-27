// ─────────────────────────────────────────────────────────────────────────────
// AdminService — secured proxy onto SAP Cloud Identity Services (SCIM 2.0 +
// Applications API). Every endpoint requires the `Admin` scope, mapped via the
// `LaunchpadAdmin` role collection in xs-security.json.
//
// Custom types are intentionally permissive (open structures) — the IAS SCIM
// payloads are large and may evolve; we forward what the IAS API returns so
// the UI can surface new attributes without a backend redeploy.
// ─────────────────────────────────────────────────────────────────────────────

@requires: 'Admin'
service AdminService @(path: '/odata/v4/admin') {

  // ─── Read-only / dashboard helpers ──────────────────────────────────────

  type Overview {
    users         : Integer;
    activeUsers   : Integer;
    inactiveUsers : Integer;
    groups        : Integer;
    applications  : Integer;
    roleCollections : Integer;
    iasHost       : String;
    iasOrigin     : String;
    healthy       : Boolean;
    btpHealthy    : Boolean;
    error         : String;
  }
  function getOverview() returns Overview;

  // ─── Users ──────────────────────────────────────────────────────────────

  type UserSummary {
    id        : String;
    userName  : String;
    email     : String;
    firstName : String;
    lastName  : String;
    active    : Boolean;
    created   : String;
    groups    : Integer;
  }

  type UserList {
    items        : many UserSummary;
    totalResults : Integer;
    startIndex   : Integer;
    itemsPerPage : Integer;
  }

  function listUsers(
    filter     : String,
    startIndex : Integer,
    count      : Integer
  ) returns UserList;

  function getUserDetails(id : String) returns String; // raw JSON, parsed client-side

  action inviteUser(
    userName        : String,
    email           : String,
    firstName       : String,
    lastName        : String,
    active          : Boolean,
    roleCollections : many String  // optional — BTP role-collection names to assign immediately
  ) returns String;

  action updateUserProfile(
    id        : String,
    firstName : String,
    lastName  : String,
    email     : String
  ) returns String;

  action setUserActive(id : String, active : Boolean) returns String;
  action deleteUser   (id : String) returns Boolean;

  action addUserToGroup     (userId : String, groupId : String) returns Boolean;
  action removeUserFromGroup(userId : String, groupId : String) returns Boolean;

  // ─── Groups (a.k.a. Roles in the UI) ────────────────────────────────────

  type GroupSummary {
    id          : String;
    displayName : String;
    description : String;
    memberCount : Integer;
  }

  type GroupList {
    items        : many GroupSummary;
    totalResults : Integer;
  }

  function listGroups(
    filter     : String,
    startIndex : Integer,
    count      : Integer
  ) returns GroupList;

  function getGroupDetails(id : String) returns String;

  action createGroup(displayName : String, description : String) returns String;
  action updateGroup(id : String, displayName : String, description : String) returns String;
  action deleteGroup(id : String) returns Boolean;

  // ─── Applications ───────────────────────────────────────────────────────

  type AppSummary {
    id          : String;
    displayName : String;
    appType     : String;
    homeUrl     : String;
    active      : Boolean;
  }

  type AppList {
    items        : many AppSummary;
    totalResults : Integer;
  }

  function listApplications() returns AppList;
  function getApplicationDetails(id : String) returns String;

  // ─── BTP Role Collections (subaccount-scoped, via XSUAA apiaccess) ───────

  type RoleCollectionSummary {
    name        : String;
    description : String;
    isReadOnly  : Boolean;
    roleCount   : Integer;
  }

  type RoleCollectionList {
    items        : many RoleCollectionSummary;
    totalResults : Integer;
  }

  function listRoleCollections() returns RoleCollectionList;
  function getRoleCollectionDetails(name : String) returns String; // raw JSON
  function getUserRoleCollections(userName : String) returns RoleCollectionList;

  action createRoleCollection(name : String, description : String) returns String;
  action deleteRoleCollection(name : String) returns Boolean;

  action assignRoleCollection  (userName : String, roleCollectionName : String) returns Boolean;
  action unassignRoleCollection(userName : String, roleCollectionName : String) returns Boolean;
}
