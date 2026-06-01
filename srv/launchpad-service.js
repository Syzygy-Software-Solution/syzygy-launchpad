const cds = require('@sap/cds');

// The 22 fixed roles seeded on first start (idempotent — only when the table
// is empty). Derived from the role/permission matrix: [name, department,
// read, write, delete, approve, execute]. These are created as *common* roles
// (no appId) and flagged isSystem so they cannot be deleted from the UI.
const SEED_ROLES = [
  ['Comp Admin',        'Comp Admin',        1, 1, 0, 0, 1],
  ['Com Admin Manager', 'Comp Admin',        1, 1, 1, 1, 1],
  ['Administrator',     'Comp Admin',        1, 1, 1, 1, 1],
  ['Read Only User',    'Comp Admin',        1, 0, 0, 0, 0],
  ['Comp VP',           'Comp Admin',        1, 0, 0, 1, 0],
  ['Sales Rep',         'Sales',             1, 1, 0, 0, 0],
  ['Sales Managers',    'Sales',             1, 1, 0, 1, 0],
  ['Sales VPs',         'Sales',             1, 0, 0, 1, 0],
  ['Sales CRO',         'Sales',             1, 0, 0, 1, 0],
  ['Finance Manager',   'Finance',           1, 1, 0, 1, 0],
  ['Read Only User',    'Finance',           1, 0, 0, 0, 0],
  ['CFO',               'Finance',           1, 0, 0, 1, 0],
  ['HR Manager',        'HR',                1, 1, 0, 1, 0],
  ['Read Only User',    'HR',                1, 0, 0, 0, 0],
  ['HR VP',             'HR',                1, 0, 0, 1, 0],
  ['IT Support',        'IT',                1, 1, 0, 0, 1],
  ['IT Administrator',  'IT',                1, 1, 1, 1, 1],
  ['Read Only User',    'IT',                1, 0, 0, 0, 0],
  ['IT CIO / VP',       'IT',                1, 0, 0, 1, 0],
  ['Sales Ops Analyst', 'Sales Operations',  1, 1, 0, 0, 1],
  ['Sales Ops Manager', 'Sales Operations',  1, 1, 0, 1, 1],
  ['Sales Ops VP',      'Sales Operations',  1, 0, 0, 1, 0]
];

async function _seedSecurityRoles() {
  const { SecurityRoles } = cds.entities('sz') || {};
  if (!SecurityRoles) return;
  const existing = await SELECT.one`count(*) as n`.from(SecurityRoles);
  if (existing && existing.n > 0) return; // already seeded — never overwrite admin edits
  const rows = SEED_ROLES.map(([roleName, department, r, w, d, a, e]) => ({
    ID:         cds.utils.uuid(),
    roleName,
    department,
    appId:      null,
    isSystem:   true,
    canRead:    !!r,
    canWrite:   !!w,
    canDelete:  !!d,
    canApprove: !!a,
    canExecute: !!e
  }));
  await INSERT.into(SecurityRoles).entries(rows);
  console.log(`[launchpad] seeded ${rows.length} system security roles`);
}

// Warm up the DB connection pool on startup so the first user request doesn't pay
// the cold-start cost (HDI handshake, JWT cache miss). Trivial query against the
// service's own entity is enough to prime connections.
cds.on('served', async () => {
  try {
    const { ConfiguredApps } = cds.entities('LaunchpadService') || {};
    if (ConfiguredApps) {
      await SELECT.one.from(ConfiguredApps);
      console.log('[launchpad] DB pool warmed up');
    }
  } catch (e) {
    console.warn('[launchpad] warm-up failed (non-fatal):', e.message);
  }
  try {
    await _seedSecurityRoles();
  } catch (e) {
    console.warn('[launchpad] security-role seeding failed (non-fatal):', e.message);
  }
});

module.exports = cds.service.impl(async function () {
  const { ConfiguredApps } = this.entities;

  this.on('registerApp', async (req) => {
    const { appId, displayName, runtimeUrl, iconUri, appType, enabled } = req.data;
    if (!appId || !displayName || !runtimeUrl) {
      return req.reject(400, 'appId, displayName and runtimeUrl are required');
    }
    const existing = await SELECT.one.from(ConfiguredApps).where({ appId });
    if (existing) {
      return req.reject(409, `App "${appId}" is already registered`);
    }
    const max = await SELECT.one`max(sortOrder) as m`.from(ConfiguredApps);
    await INSERT.into(ConfiguredApps).entries({
      appId,
      displayName,
      runtimeUrl,
      iconUri: iconUri || 'sap-icon://grid',
      appType: appType || 'btp',
      enabled: enabled !== false,
      sortOrder: (max?.m || 0) + 10
    });
    return await SELECT.one.from(ConfiguredApps).where({ appId });
  });

  this.on('updateApp', async (req) => {
    const { appId, displayName, runtimeUrl, iconUri, enabled } = req.data;
    if (!appId) return req.reject(400, 'appId is required');
    const set = {};
    if (displayName !== undefined) set.displayName = displayName;
    if (runtimeUrl !== undefined)  set.runtimeUrl  = runtimeUrl;
    if (iconUri !== undefined)     set.iconUri     = iconUri;
    if (enabled !== undefined)     set.enabled     = enabled;
    await UPDATE(ConfiguredApps).set(set).where({ appId });
    return await SELECT.one.from(ConfiguredApps).where({ appId });
  });

  this.on('unregisterApp', async (req) => {
    const { appId } = req.data;
    const result = await DELETE.from(ConfiguredApps).where({ appId });
    return result > 0;
  });

  this.on('toggleApp', async (req) => {
    const { appId, enabled } = req.data;
    await UPDATE(ConfiguredApps).set({ enabled: !!enabled }).where({ appId });
    return true;
  });

  this.on('currentUser', async (req) => {
    const u = req.user || {};
    const attr = u.attr || {};

    // Base values from the XSUAA JWT
    let firstname = attr.given_name || attr.firstname || '';
    let lastname  = attr.family_name || attr.lastname  || '';

    // Override with any DB-persisted profile (user may have customised their name)
    try {
      const profile = await SELECT.one.from('sz.UserProfile').where({ userId: u.id });
      if (profile) {
        if (profile.firstname !== undefined) firstname = profile.firstname;
        if (profile.lastname  !== undefined) lastname  = profile.lastname;
      }
    } catch (e) { /* fall back to XSUAA values on any DB error */ }

    return {
      id:        u.id || '',
      email:     attr.email || u.id || '',
      firstname,
      lastname,
      name: [firstname, lastname].filter(Boolean).join(' ') || u.id || ''
    };
  });

  this.on('updateUserProfile', async (req) => {
    const { firstname, lastname } = req.data;
    const userId = req.user?.id;
    if (!userId) return req.reject(401, 'Not authenticated');

    await UPSERT.into('sz.UserProfile').entries({ userId, firstname, lastname });

    // Return the updated user so the UI can refresh in one round-trip
    const u    = req.user;
    const attr = u.attr || {};
    return {
      id:        u.id,
      email:     attr.email || u.id,
      firstname,
      lastname,
      name: [firstname, lastname].filter(Boolean).join(' ') || u.id
    };
  });

  /* ───── Security · Role & permission management ───── */

  const { SecurityRoles } = this.entities;

  // Normalises an empty/whitespace appId to null so "common role" stays
  // consistent (null) rather than mixing '' and null.
  const _normAppId = (v) => (v && String(v).trim()) ? String(v).trim() : null;

  this.on('createSecurityRole', async (req) => {
    const d = req.data;
    if (!d.roleName || !d.roleName.trim()) {
      return req.reject(400, 'roleName is required');
    }
    const ID = cds.utils.uuid();
    await INSERT.into(SecurityRoles).entries({
      ID,
      roleName:    d.roleName.trim(),
      department:  d.department || null,
      appId:       _normAppId(d.appId),
      description: d.description || null,
      isSystem:    false,
      canRead:     !!d.canRead,
      canWrite:    !!d.canWrite,
      canDelete:   !!d.canDelete,
      canApprove:  !!d.canApprove,
      canExecute:  !!d.canExecute
    });
    return await SELECT.one.from(SecurityRoles).where({ ID });
  });

  this.on('updateSecurityRole', async (req) => {
    const d = req.data;
    if (!d.ID) return req.reject(400, 'ID is required');
    const existing = await SELECT.one.from(SecurityRoles).where({ ID: d.ID });
    if (!existing) return req.reject(404, 'Role not found');
    const set = {};
    // System roles keep their seeded name/department; only permissions and
    // app-scope may be changed. Custom roles are fully editable.
    if (!existing.isSystem) {
      if (d.roleName !== undefined)   set.roleName   = d.roleName;
      if (d.department !== undefined) set.department = d.department || null;
    }
    if (d.appId !== undefined)       set.appId       = _normAppId(d.appId);
    if (d.description !== undefined) set.description = d.description || null;
    if (d.canRead !== undefined)     set.canRead     = !!d.canRead;
    if (d.canWrite !== undefined)    set.canWrite    = !!d.canWrite;
    if (d.canDelete !== undefined)   set.canDelete   = !!d.canDelete;
    if (d.canApprove !== undefined)  set.canApprove  = !!d.canApprove;
    if (d.canExecute !== undefined)  set.canExecute  = !!d.canExecute;
    await UPDATE(SecurityRoles).set(set).where({ ID: d.ID });
    return await SELECT.one.from(SecurityRoles).where({ ID: d.ID });
  });

  this.on('deleteSecurityRole', async (req) => {
    const { ID } = req.data;
    if (!ID) return req.reject(400, 'ID is required');
    const existing = await SELECT.one.from(SecurityRoles).where({ ID });
    if (!existing) return false;
    if (existing.isSystem) {
      return req.reject(403, 'System roles cannot be deleted');
    }
    const result = await DELETE.from(SecurityRoles).where({ ID });
    return result > 0;
  });
});
