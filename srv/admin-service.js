// ─────────────────────────────────────────────────────────────────────────────
// AdminService implementation — translates CAP actions/functions into SCIM
// calls against SAP Cloud Identity Services. All handlers are protected by
// the `@requires: 'Admin'` annotation in admin-service.cds.
// ─────────────────────────────────────────────────────────────────────────────

const cds = require('@sap/cds');
const ias = require('./lib/ias-client');
const btp = require('./lib/btp-client');

function _origin() {
  // XSUAA Trust-Configuration `Origin Key` for our IAS tenant.
  return process.env.IAS_ORIGIN || 'sap.custom';
}

/* ─── helpers ─────────────────────────────────────────────────────────────── */

function _mapUserSummary(u) {
  const emails = Array.isArray(u.emails) ? u.emails : [];
  const primary = emails.find(e => e.primary) || emails[0] || {};
  const name = u.name || {};
  return {
    id:        u.id,
    userName:  u.userName || primary.value || '',
    email:     primary.value || u.userName || '',
    firstName: name.givenName  || '',
    lastName:  name.familyName || '',
    active:    u.active !== false,
    created:   (u.meta && u.meta.created) || '',
    groups:    Array.isArray(u.groups) ? u.groups.length : 0
  };
}

function _mapGroupSummary(g) {
  // Description lives in a custom extension namespace in IAS.
  const ext = g['urn:sap:cloud:scim:schemas:extension:custom:2.0:Group'] || {};
  return {
    id:          g.id,
    displayName: g.displayName || '',
    description: ext.description || '',
    memberCount: Array.isArray(g.members) ? g.members.length : 0
  };
}

function _mapAppSummary(a) {
  return {
    id:          a.id,
    displayName: a.name || a.displayName || '',
    appType:     a.applicationType || a.appType || '',
    homeUrl:     (a.urls && (a.urls.homeUrl || a.urls.startUrl)) || a.homeUrl || '',
    active:      a.active !== false
  };
}

function _mapRoleCollectionSummary(rc) {
  return {
    name:        rc.name || '',
    description: rc.description || '',
    isReadOnly:  rc.isReadOnly === true,
    roleCount:   Array.isArray(rc.roleReferences) ? rc.roleReferences.length : 0
  };
}

function _reject(req, e) {
  const code = e.statusCode || 500;
  const msg  = e.message    || 'IAS request failed';
  console.error('[admin-service]', msg, e.iasBody || '');
  return req.reject(code, msg);
}

/* ─── service implementation ──────────────────────────────────────────────── */

module.exports = cds.service.impl(async function () {

  /* Dashboard overview — single round trip from the UI, three calls in
     parallel under the hood. */
  this.on('getOverview', async (req) => {
    if (!ias.isConfigured()) {
      return {
        users: 0, activeUsers: 0, inactiveUsers: 0, groups: 0, applications: 0, roleCollections: 0,
        iasHost:    process.env.IAS_HOST || '',
        iasOrigin:  _origin(),
        healthy:    false,
        btpHealthy: btp.isConfigured(),
        error:      'IAS credentials are not configured. Bind syzygy-launchpad-ias and set clientId/clientSecret.'
      };
    }
    try {
      const tasks = [
        ias.countUsers(),
        ias.countUsers('active eq true'),
        ias.countGroups(),
        ias.listApplications()
      ];
      // BTP role-collection count — add only if apiaccess binding exists.
      const btpReady = btp.isConfigured();
      if (btpReady) tasks.push(btp.listRoleCollections().catch(() => []));
      const [users, activeUsers, groups, apps, rcs] = await Promise.all(tasks);
      const total       = users.totalResults || 0;
      const totalActive = activeUsers.totalResults || 0;
      return {
        users:           total,
        activeUsers:     totalActive,
        inactiveUsers:   Math.max(0, total - totalActive),
        groups:          groups.totalResults || 0,
        applications:    (apps && apps.totalResults) || (apps && apps.applications && apps.applications.length) || 0,
        roleCollections: btpReady ? (Array.isArray(rcs) ? rcs.length : 0) : 0,
        iasHost:         process.env.IAS_HOST || '',
        iasOrigin:       _origin(),
        healthy:         true,
        btpHealthy:      btpReady,
        error:           ''
      };
    } catch (e) {
      return {
        users: 0, activeUsers: 0, inactiveUsers: 0, groups: 0, applications: 0, roleCollections: 0,
        iasHost:    process.env.IAS_HOST || '',
        iasOrigin:  _origin(),
        healthy:    false,
        btpHealthy: btp.isConfigured(),
        error:      e.message
      };
    }
  });

  /* ───── Users ───── */

  this.on('listUsers', async (req) => {
    const { filter, startIndex, count } = req.data;
    try {
      const data = await ias.listUsers({
        filter,
        startIndex: startIndex || 1,
        count:      count != null ? count : 100
      });
      const resources = data.Resources || [];
      return {
        items:        resources.map(_mapUserSummary),
        totalResults: data.totalResults || resources.length,
        startIndex:   data.startIndex   || 1,
        itemsPerPage: data.itemsPerPage || resources.length
      };
    } catch (e) { return _reject(req, e); }
  });

  this.on('getUserDetails', async (req) => {
    try {
      const u = await ias.getUser(req.data.id);
      return JSON.stringify(u);
    } catch (e) { return _reject(req, e); }
  });

  this.on('inviteUser', async (req) => {
    const { userName, email, firstName, lastName, active, roleCollections } = req.data;
    if (!userName && !email) return req.reject(400, 'Either userName or email is required');
    const effectiveUserName = userName || email;
    const payload = {
      schemas:  ['urn:ietf:params:scim:schemas:core:2.0:User'],
      userName: effectiveUserName,
      name:     { givenName: firstName || '', familyName: lastName || '' },
      emails:   [{ value: email || userName, primary: true, type: 'work' }],
      active:   active !== false
    };
    let createdUser;
    try {
      createdUser = await ias.createUser(payload);
    } catch (e) { return _reject(req, e); }

    // Optional: also assign one or more BTP role collections so the user
    // appears in the subaccount user list right away (shadow user is
    // created as a side-effect of the assignment).
    const rcs = Array.isArray(roleCollections) ? roleCollections.filter(Boolean) : [];
    const assignmentErrors = [];
    if (rcs.length) {
      if (!btp.isConfigured()) {
        assignmentErrors.push({ roleCollection: '*', error: 'BTP apiaccess binding is not configured — role collections were not assigned.' });
      } else {
        // Try both common origins so the shadow user lands wherever the
        // subaccount trust accepts it. We succeed if either origin works.
        const origins = Array.from(new Set([_origin(), 'sap.default', 'sap.custom']));
        for (const rc of rcs) {
          let lastErr = null;
          let ok = false;
          for (const origin of origins) {
            try {
              await btp.assignRoleCollection({ userName: effectiveUserName, origin, roleCollectionName: rc });
              ok = true;
              break;
            } catch (e) {
              lastErr = e;
            }
          }
          if (!ok) {
            assignmentErrors.push({ roleCollection: rc, error: (lastErr && lastErr.message) || 'assignment failed' });
          }
        }
      }
    }
    return JSON.stringify({ user: createdUser, assignmentErrors });
  });

  this.on('updateUserProfile', async (req) => {
    const { id, firstName, lastName, email } = req.data;
    if (!id) return req.reject(400, 'id is required');
    const ops = [];
    if (firstName !== undefined) ops.push({ op: 'replace', path: 'name.givenName',  value: firstName });
    if (lastName  !== undefined) ops.push({ op: 'replace', path: 'name.familyName', value: lastName  });
    if (email     !== undefined) ops.push({
      op:    'replace',
      path:  'emails[primary eq true].value',
      value: email
    });
    try {
      const u = await ias.patchUser(id, ops);
      return JSON.stringify(u);
    } catch (e) { return _reject(req, e); }
  });

  this.on('setUserActive', async (req) => {
    const { id, active } = req.data;
    if (!id) return req.reject(400, 'id is required');
    try {
      const u = await ias.patchUser(id, [{ op: 'replace', path: 'active', value: !!active }]);
      return JSON.stringify(u);
    } catch (e) { return _reject(req, e); }
  });

  this.on('deleteUser', async (req) => {
    if (!req.data.id) return req.reject(400, 'id is required');
    try { await ias.deleteUser(req.data.id); return true; }
    catch (e) { return _reject(req, e); }
  });

  this.on('addUserToGroup', async (req) => {
    const { userId, groupId } = req.data;
    if (!userId || !groupId) return req.reject(400, 'userId and groupId are required');
    try {
      await ias.patchGroup(groupId, [{
        op:    'add',
        path:  'members',
        value: [{ value: userId, type: 'User' }]
      }]);
      return true;
    } catch (e) { return _reject(req, e); }
  });

  this.on('removeUserFromGroup', async (req) => {
    const { userId, groupId } = req.data;
    if (!userId || !groupId) return req.reject(400, 'userId and groupId are required');
    try {
      await ias.patchGroup(groupId, [{
        op:   'remove',
        path: `members[value eq "${userId}"]`
      }]);
      return true;
    } catch (e) { return _reject(req, e); }
  });

  /* ───── Groups ───── */

  this.on('listGroups', async (req) => {
    const { filter, startIndex, count } = req.data;
    try {
      const data = await ias.listGroups({
        filter,
        startIndex: startIndex || 1,
        count:      count != null ? count : 200
      });
      const resources = data.Resources || [];
      return {
        items:        resources.map(_mapGroupSummary),
        totalResults: data.totalResults || resources.length
      };
    } catch (e) { return _reject(req, e); }
  });

  this.on('getGroupDetails', async (req) => {
    try {
      const g = await ias.getGroup(req.data.id);
      return JSON.stringify(g);
    } catch (e) { return _reject(req, e); }
  });

  this.on('createGroup', async (req) => {
    const { displayName, description } = req.data;
    if (!displayName) return req.reject(400, 'displayName is required');
    try {
      const g = await ias.createGroup({ displayName, description });
      return JSON.stringify(g);
    } catch (e) { return _reject(req, e); }
  });

  this.on('updateGroup', async (req) => {
    const { id, displayName, description } = req.data;
    if (!id) return req.reject(400, 'id is required');
    const ops = [];
    if (displayName !== undefined) ops.push({ op: 'replace', path: 'displayName', value: displayName });
    if (description !== undefined) ops.push({
      op:    'replace',
      path:  'urn:sap:cloud:scim:schemas:extension:custom:2.0:Group:description',
      value: description
    });
    try {
      const g = await ias.patchGroup(id, ops);
      return JSON.stringify(g);
    } catch (e) { return _reject(req, e); }
  });

  this.on('deleteGroup', async (req) => {
    if (!req.data.id) return req.reject(400, 'id is required');
    try { await ias.deleteGroup(req.data.id); return true; }
    catch (e) { return _reject(req, e); }
  });

  /* ───── Applications ───── */

  this.on('listApplications', async (req) => {
    try {
      const data = await ias.listApplications();
      const resources = data.applications || data.Resources || [];
      return {
        items:        resources.map(_mapAppSummary),
        totalResults: data.totalResults || resources.length
      };
    } catch (e) { return _reject(req, e); }
  });

  this.on('getApplicationDetails', async (req) => {
    try {
      const a = await ias.getApplication(req.data.id);
      return JSON.stringify(a);
    } catch (e) { return _reject(req, e); }
  });

  /* ───── BTP Role Collections ───── */

  this.on('listRoleCollections', async (req) => {
    if (!btp.isConfigured()) {
      return { items: [], totalResults: 0 };
    }
    try {
      const list = await btp.listRoleCollections();
      const items = list.map(_mapRoleCollectionSummary);
      // Stable alphabetical order — BTP returns them in insertion order.
      items.sort((a, b) => a.name.localeCompare(b.name));
      return { items, totalResults: items.length };
    } catch (e) { return _reject(req, e); }
  });

  this.on('getRoleCollectionDetails', async (req) => {
    if (!btp.isConfigured()) return req.reject(503, 'BTP apiaccess binding is not configured');
    try {
      const rc = await btp.getRoleCollection(req.data.name);
      return JSON.stringify(rc);
    } catch (e) { return _reject(req, e); }
  });

  this.on('getUserRoleCollections', async (req) => {
    if (!btp.isConfigured()) return { items: [], totalResults: 0 };
    // Role collections may be assigned under any of several trust origins
    // (Default IDP `sap.default`, custom IAS `sap.custom`, or a custom
    // origin key). Query each known origin and merge the results, since
    // BTP requires `origin` to be specified on this endpoint.
    const origins = Array.from(new Set([_origin(), 'sap.default', 'sap.custom']));
    const seen = new Set();
    const merged = [];
    let anyOk = false;
    let lastErr = null;
    for (const origin of origins) {
      try {
        const list = await btp.listUserRoleCollections({
          userName: req.data.userName,
          origin
        });
        anyOk = true;
        for (const rc of list) {
          const key = rc.name || rc.displayName;
          if (!key || seen.has(key)) continue;
          seen.add(key);
          merged.push(rc);
        }
      } catch (e) {
        // 404 / 403 just mean "no shadow user under this origin".
        if (e.status === 404 || e.status === 403) { anyOk = true; continue; }
        lastErr = e;
      }
    }
    if (!anyOk && lastErr) return _reject(req, lastErr);
    const items = merged.map(_mapRoleCollectionSummary);
    items.sort((a, b) => a.name.localeCompare(b.name));
    return { items, totalResults: items.length };
  });

  this.on('createRoleCollection', async (req) => {
    if (!btp.isConfigured()) return req.reject(503, 'BTP apiaccess binding is not configured');
    const { name, description } = req.data;
    if (!name) return req.reject(400, 'name is required');
    try {
      const rc = await btp.createRoleCollection({ name, description: description || '' });
      return JSON.stringify(rc || { name });
    } catch (e) { return _reject(req, e); }
  });

  this.on('deleteRoleCollection', async (req) => {
    if (!btp.isConfigured()) return req.reject(503, 'BTP apiaccess binding is not configured');
    if (!req.data.name)        return req.reject(400, 'name is required');
    try { await btp.deleteRoleCollection(req.data.name); return true; }
    catch (e) { return _reject(req, e); }
  });

  this.on('assignRoleCollection', async (req) => {
    if (!btp.isConfigured()) return req.reject(503, 'BTP apiaccess binding is not configured');
    const { userName, roleCollectionName } = req.data;
    if (!userName || !roleCollectionName) return req.reject(400, 'userName and roleCollectionName are required');
    // Try the configured origin first; fall back to the other common origins
    // so the assignment succeeds whichever IDP the user authenticates through.
    const origins = Array.from(new Set([_origin(), 'sap.default', 'sap.custom']));
    let lastErr = null;
    for (const origin of origins) {
      try {
        await btp.assignRoleCollection({ userName, origin, roleCollectionName });
        return true;
      } catch (e) { lastErr = e; }
    }
    return _reject(req, lastErr || new Error('assignRoleCollection failed'));
  });

  this.on('unassignRoleCollection', async (req) => {
    if (!btp.isConfigured()) return req.reject(503, 'BTP apiaccess binding is not configured');
    const { userName, roleCollectionName } = req.data;
    if (!userName || !roleCollectionName) return req.reject(400, 'userName and roleCollectionName are required');
    // Attempt removal under every known origin — ignore not-found per origin.
    const origins = Array.from(new Set([_origin(), 'sap.default', 'sap.custom']));
    let anyOk = false;
    let lastErr = null;
    for (const origin of origins) {
      try {
        await btp.unassignRoleCollection({ userName, origin, roleCollectionName });
        anyOk = true;
      } catch (e) {
        if (e.status === 404) { continue; }
        lastErr = e;
      }
    }
    if (!anyOk && lastErr) return _reject(req, lastErr);
    return true;
  });
});
