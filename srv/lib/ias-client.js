// ─────────────────────────────────────────────────────────────────────────────
// IAS SCIM / Applications API HTTP client.
//
// Reads the IAS tenant host + admin client credentials from the bound
// `syzygy-launchpad-ias` user-provided service (see mta.yaml). Exposes thin
// JSON helpers used by srv/admin-service.js.
//
// Local dev: when VCAP_SERVICES is missing, falls back to env vars
//   IAS_HOST, IAS_CLIENT_ID, IAS_CLIENT_SECRET
// so `cds watch` can hit a real tenant if those are exported in the shell.
// ─────────────────────────────────────────────────────────────────────────────

let _config = null;

function _loadConfig() {
  if (_config) return _config;

  let clientId, clientSecret;
  const host = process.env.IAS_HOST || '';

  // Production / staged: credentials live in VCAP_SERVICES under the
  // user-provided service.
  try {
    const vcap = JSON.parse(process.env.VCAP_SERVICES || '{}');
    const ups  = vcap['user-provided'] || [];
    const ias  = ups.find(s => s.name === 'syzygy-launchpad-ias');
    if (ias && ias.credentials) {
      clientId     = ias.credentials.clientId     || ias.credentials.IAS_CLIENT_ID;
      clientSecret = ias.credentials.clientSecret || ias.credentials.IAS_CLIENT_SECRET;
    }
  } catch (e) { /* ignore — fall through to env */ }

  // Local dev fallback
  if (!clientId)     clientId     = process.env.IAS_CLIENT_ID     || '';
  if (!clientSecret) clientSecret = process.env.IAS_CLIENT_SECRET || '';

  _config = { host, clientId, clientSecret };
  return _config;
}

function _basicAuthHeader() {
  const { clientId, clientSecret } = _loadConfig();
  if (!clientId || !clientSecret) {
    const err = new Error('IAS credentials are not configured (syzygy-launchpad-ias user-provided service is missing clientId/clientSecret)');
    err.statusCode = 500;
    throw err;
  }
  const token = Buffer.from(`${clientId}:${clientSecret}`).toString('base64');
  return `Basic ${token}`;
}

function _baseUrl() {
  const { host } = _loadConfig();
  if (!host) {
    const err = new Error('IAS_HOST is not configured');
    err.statusCode = 500;
    throw err;
  }
  return `https://${host}`;
}

async function _request(method, path, body, accept = 'application/scim+json') {
  const url = _baseUrl() + path;
  const headers = {
    Authorization: _basicAuthHeader(),
    Accept:        accept
  };
  if (body !== undefined) {
    headers['Content-Type'] = accept.includes('scim') ? 'application/scim+json' : 'application/json';
  }

  let res;
  try {
    res = await fetch(url, {
      method,
      headers,
      body: body !== undefined ? JSON.stringify(body) : undefined
    });
  } catch (e) {
    const err = new Error(`IAS request failed: ${e.message}`);
    err.statusCode = 502;
    throw err;
  }

  // 204 No Content — typical for DELETE
  if (res.status === 204) return null;

  const text = await res.text();
  let json = null;
  if (text) {
    try { json = JSON.parse(text); } catch (e) { /* keep as text */ }
  }

  if (!res.ok) {
    const detail = (json && (json.detail || json.message || json.error_description)) || text || res.statusText;
    const err = new Error(`IAS ${method} ${path} → ${res.status}: ${detail}`);
    err.statusCode = res.status;
    err.iasBody = json || text;
    throw err;
  }
  return json;
}

// ─────────────────────────────────────────────────────────────────────────────
// Public surface — keep methods small & explicit so handlers stay readable.
// ─────────────────────────────────────────────────────────────────────────────

module.exports = {
  isConfigured() {
    const c = _loadConfig();
    return !!(c.host && c.clientId && c.clientSecret);
  },

  /* ---------- SCIM /Users ---------- */

  listUsers({ filter, startIndex = 1, count = 100, attributes } = {}) {
    const qs = new URLSearchParams();
    if (filter)     qs.set('filter',     filter);
    if (startIndex) qs.set('startIndex', String(startIndex));
    if (count != null) qs.set('count',   String(count));
    if (attributes) qs.set('attributes', attributes);
    return _request('GET', `/scim/Users?${qs.toString()}`);
  },
  countUsers(filter) {
    const qs = new URLSearchParams({ count: '0' });
    if (filter) qs.set('filter', filter);
    return _request('GET', `/scim/Users?${qs.toString()}`);
  },
  getUser(id) {
    return _request('GET', `/scim/Users/${encodeURIComponent(id)}`);
  },
  createUser(payload) {
    return _request('POST', '/scim/Users', payload);
  },
  patchUser(id, operations) {
    return _request('PATCH', `/scim/Users/${encodeURIComponent(id)}`, {
      schemas:    ['urn:ietf:params:scim:api:messages:2.0:PatchOp'],
      Operations: operations
    });
  },
  deleteUser(id) {
    return _request('DELETE', `/scim/Users/${encodeURIComponent(id)}`);
  },

  /* ---------- SCIM /Groups ---------- */

  listGroups({ filter, startIndex = 1, count = 200, attributes } = {}) {
    const qs = new URLSearchParams();
    if (filter)     qs.set('filter',     filter);
    if (startIndex) qs.set('startIndex', String(startIndex));
    if (count != null) qs.set('count',   String(count));
    if (attributes) qs.set('attributes', attributes);
    return _request('GET', `/scim/Groups?${qs.toString()}`);
  },
  countGroups() {
    return _request('GET', '/scim/Groups?count=0');
  },
  getGroup(id) {
    return _request('GET', `/scim/Groups/${encodeURIComponent(id)}`);
  },
  createGroup({ displayName, description }) {
    return _request('POST', '/scim/Groups', {
      schemas:     ['urn:ietf:params:scim:schemas:core:2.0:Group'],
      displayName,
      ...(description ? { 'urn:sap:cloud:scim:schemas:extension:custom:2.0:Group': { description } } : {})
    });
  },
  patchGroup(id, operations) {
    return _request('PATCH', `/scim/Groups/${encodeURIComponent(id)}`, {
      schemas:    ['urn:ietf:params:scim:api:messages:2.0:PatchOp'],
      Operations: operations
    });
  },
  deleteGroup(id) {
    return _request('DELETE', `/scim/Groups/${encodeURIComponent(id)}`);
  },

  /* ---------- Applications API ---------- */

  listApplications() {
    return _request('GET', '/Applications/v1/', undefined, 'application/json');
  },
  getApplication(id) {
    return _request('GET', `/Applications/v1/${encodeURIComponent(id)}`, undefined, 'application/json');
  }
};
