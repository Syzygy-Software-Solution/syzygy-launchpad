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
}
