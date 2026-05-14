const cds = require('@sap/cds');

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
});
