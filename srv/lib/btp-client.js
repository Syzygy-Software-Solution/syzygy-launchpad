"use strict";

/**
 * srv/lib/btp-client.js
 *
 * Thin REST client around the BTP Authorization & Trust Management Service
 * exposed by the XSUAA `apiaccess` service-plan binding.
 *
 * Binding (VCAP_SERVICES[xsuaa] entry with plan=apiaccess) gives us:
 *   credentials.clientid
 *   credentials.clientsecret
 *   credentials.url              ← token endpoint (https://<subdomain>.authentication.<region>.hana.ondemand.com)
 *   credentials.apiurl           ← REST API base    (https://api.authentication.<region>.hana.ondemand.com)
 *
 * We obtain an OAuth2 bearer via client_credentials grant and cache it
 * until ~30 s before its declared expiry.
 *
 * The REST paths used below are the documented public endpoints of the
 * Authorization & Trust Management API:
 *   https://api.sap.com/api/APIAuthorizationService/overview
 *
 * Scope used by all callers: the `apiaccess` plan binding already grants
 * us the scopes the API requires (xs_authorization.*, xs_user.*) when the
 * binding is created — no extra config needed for the SAME subaccount.
 */

let _cachedToken = null; // { value, expiresAt }

function _readBinding() {
  // VCAP_SERVICES is injected by Cloud Foundry. We look for an xsuaa entry
  // whose plan is `apiaccess`. If multiple are bound we take the first.
  const vcap = process.env.VCAP_SERVICES ? JSON.parse(process.env.VCAP_SERVICES) : {};
  const list = vcap.xsuaa || [];
  const entry = list.find(e => (e.plan || '').toLowerCase() === 'apiaccess');
  if (!entry || !entry.credentials) return null;
  const c = entry.credentials;
  if (!c.clientid || !c.clientsecret || !c.url || !c.apiurl) return null;
  return c;
}

function isConfigured() {
  return !!_readBinding();
}

async function _getToken() {
  if (_cachedToken && _cachedToken.expiresAt > Date.now() + 30_000) {
    return _cachedToken.value;
  }
  const b = _readBinding();
  if (!b) {
    const err = new Error('XSUAA apiaccess binding (syzygy-launchpad-xsuaa-api) is not configured');
    err.code = 'BTP_NOT_CONFIGURED';
    throw err;
  }
  const basic = Buffer.from(`${b.clientid}:${b.clientsecret}`).toString('base64');
  const res = await fetch(`${b.url}/oauth/token`, {
    method: 'POST',
    headers: {
      Authorization: `Basic ${basic}`,
      'Content-Type': 'application/x-www-form-urlencoded',
      Accept: 'application/json'
    },
    body: 'grant_type=client_credentials&response_type=token'
  });
  if (!res.ok) {
    const txt = await res.text();
    throw new Error(`XSUAA token endpoint ${res.status}: ${txt}`);
  }
  const json = await res.json();
  _cachedToken = {
    value:     json.access_token,
    expiresAt: Date.now() + ((json.expires_in || 3600) * 1000)
  };
  return _cachedToken.value;
}

async function _call(method, path, body) {
  const b = _readBinding();
  if (!b) {
    const err = new Error('XSUAA apiaccess binding is not configured');
    err.code = 'BTP_NOT_CONFIGURED';
    throw err;
  }
  const token = await _getToken();
  const url = b.apiurl.replace(/\/+$/, '') + path;
  const res = await fetch(url, {
    method,
    headers: {
      Authorization: `Bearer ${token}`,
      Accept: 'application/json',
      ...(body !== undefined ? { 'Content-Type': 'application/json' } : {})
    },
    body: body !== undefined ? JSON.stringify(body) : undefined
  });
  if (res.status === 204) return null;
  const ct = res.headers.get('content-type') || '';
  const text = await res.text();
  if (!res.ok) {
    let detail = text;
    try { detail = JSON.parse(text).error_description || JSON.parse(text).message || text; } catch {}
    const err = new Error(`BTP ${method} ${path} → ${res.status}: ${detail}`);
    err.status = res.status;
    throw err;
  }
  if (!text) return null;
  return ct.includes('application/json') ? JSON.parse(text) : text;
}

/* =========================================================================
 * Role Collections (subaccount-scoped)
 * ========================================================================= */

// The SAP Authorization & Trust Management REST API is rooted at
// `/sap/rest/authorization/v2/…` on the apiurl host. (The shorter
// `/authorization/v2/…` form returns 404 on the public endpoint.)
const BASE = '/sap/rest/authorization/v2';

async function listRoleCollections() {
  // GET /sap/rest/authorization/v2/rolecollections → array of role collections.
  // Each entry includes: name, description, isReadOnly, roleReferences[]
  const data = await _call('GET', `${BASE}/rolecollections`);
  // Response shape can be either a raw array or { resources: [...] } depending on API version.
  return Array.isArray(data) ? data : (data && data.resources) || [];
}

async function getRoleCollection(name) {
  return _call('GET', `${BASE}/rolecollections/${encodeURIComponent(name)}`);
}

async function createRoleCollection({ name, description }) {
  return _call('POST', `${BASE}/rolecollections`, { name, description: description || '' });
}

async function deleteRoleCollection(name) {
  return _call('DELETE', `${BASE}/rolecollections/${encodeURIComponent(name)}`);
}

/* =========================================================================
 * User ↔ Role Collection assignments (creates shadow user as a side-effect)
 * ========================================================================= */

async function listUserRoleCollections({ userName, origin }) {
  const qs = `?userName=${encodeURIComponent(userName)}&origin=${encodeURIComponent(origin)}`;
  const data = await _call('GET', `${BASE}/userRoleCollections` + qs);
  return Array.isArray(data) ? data : (data && data.resources) || [];
}

async function assignRoleCollection({ userName, origin, roleCollectionName }) {
  // Body shape per Authorization Service docs.
  return _call('PUT', `${BASE}/userRoleCollections`, {
    userName,
    origin,
    roleCollectionName
  });
}

async function unassignRoleCollection({ userName, origin, roleCollectionName }) {
  // The Authorization Service uses DELETE with a JSON body for this endpoint.
  return _call('DELETE', `${BASE}/userRoleCollections`, {
    userName,
    origin,
    roleCollectionName
  });
}

/* =========================================================================
 * Shadow users — lookup / existence test
 * ========================================================================= */

async function findShadowUser({ userName, origin }) {
  // GET /sap/rest/authorization/v2/users?userName=...&origin=...
  // Returns an array (may be empty) of matching shadow-user records.
  const qs = `?userName=${encodeURIComponent(userName)}&origin=${encodeURIComponent(origin)}`;
  const data = await _call('GET', `${BASE}/users` + qs);
  const arr = Array.isArray(data) ? data : (data && data.resources) || [];
  return arr[0] || null;
}

module.exports = {
  isConfigured,
  listRoleCollections,
  getRoleCollection,
  createRoleCollection,
  deleteRoleCollection,
  listUserRoleCollections,
  assignRoleCollection,
  unassignRoleCollection,
  findShadowUser
};
