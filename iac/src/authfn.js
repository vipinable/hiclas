'use strict';

const https = require('https');
const crypto = require('crypto');

const USER_POOL_ID   = process.env.COGNITO_USER_POOL_ID;
const CLIENT_ID      = process.env.COGNITO_CLIENT_ID;
const COGNITO_DOMAIN = process.env.COGNITO_DOMAIN;
const APP_DOMAIN     = process.env.APP_DOMAIN;
const COGNITO_REGION = process.env.COGNITO_REGION;

const ISSUER   = `https://cognito-idp.${COGNITO_REGION}.amazonaws.com/${USER_POOL_ID}`;
const JWKS_URI = `${ISSUER}/.well-known/jwks.json`;
const CALLBACK = '/auth/callback';
const NO_AUTH  = ['/assets/', '/css/', '/images/', '/uploads/', '/favicon.'];

let _jwksKeys = null;
let _jwksTs   = 0;

function httpsGet(url) {
  return new Promise((resolve, reject) => {
    https.get(url, res => {
      let body = '';
      res.on('data', chunk => { body += chunk; });
      res.on('end', () => resolve(body));
    }).on('error', reject);
  });
}

function httpsPost(hostname, path, formData) {
  return new Promise((resolve, reject) => {
    const body = new URLSearchParams(formData).toString();
    const req = https.request({
      hostname, path, method: 'POST', port: 443,
      headers: {
        'Content-Type': 'application/x-www-form-urlencoded',
        'Content-Length': Buffer.byteLength(body),
      },
    }, res => {
      let data = '';
      res.on('data', c => { data += c; });
      res.on('end', () => resolve(JSON.parse(data)));
    });
    req.on('error', reject);
    req.write(body);
    req.end();
  });
}

async function getJwks() {
  if (_jwksKeys && Date.now() - _jwksTs < 3_600_000) return _jwksKeys;
  const raw = await httpsGet(JWKS_URI);
  _jwksKeys = JSON.parse(raw).keys;
  _jwksTs = Date.now();
  return _jwksKeys;
}

function b64urlDecode(s) {
  return Buffer.from(s.replace(/-/g, '+').replace(/_/g, '/'), 'base64');
}

async function verifyIdToken(token) {
  const parts = token.split('.');
  if (parts.length !== 3) return false;

  let header, payload;
  try {
    header  = JSON.parse(b64urlDecode(parts[0]));
    payload = JSON.parse(b64urlDecode(parts[1]));
  } catch {
    return false;
  }

  const now = Math.floor(Date.now() / 1000);
  if (payload.exp < now)         return false;
  if (payload.iss !== ISSUER)    return false;
  if (payload.aud !== CLIENT_ID) return false;

  const keys = await getJwks();
  const jwk  = keys.find(k => k.kid === header.kid && k.alg === 'RS256');
  if (!jwk) return false;

  try {
    const publicKey = crypto.createPublicKey({ key: jwk, format: 'jwk' });
    const verifier  = crypto.createVerify('RSA-SHA256');
    verifier.update(`${parts[0]}.${parts[1]}`);
    return verifier.verify(publicKey, b64urlDecode(parts[2]));
  } catch (e) {
    console.error('JWT signature verification failed:', e.message);
    return false;
  }
}

function parseCookies(cookieHeader) {
  if (!cookieHeader) return {};
  return Object.fromEntries(
    cookieHeader.split(';').map(part => {
      const [k, ...v] = part.trim().split('=');
      return [k.trim(), v.join('=')];
    })
  );
}

function makeRedirect(location, setCookies) {
  const response = {
    status: '302',
    headers: {
      location:        [{ key: 'Location',      value: location   }],
      'cache-control': [{ key: 'Cache-Control', value: 'no-store' }],
    },
  };
  if (setCookies && setCookies.length > 0) {
    response.headers['set-cookie'] = setCookies.map(v => ({ key: 'Set-Cookie', value: v }));
  }
  return response;
}

function redirectToLogin(returnPath) {
  const params = new URLSearchParams({
    client_id:     CLIENT_ID,
    response_type: 'code',
    scope:         'openid email profile',
    redirect_uri:  `https://${APP_DOMAIN}${CALLBACK}`,
    state:         returnPath || '/',
  });
  return makeRedirect(`https://${COGNITO_DOMAIN}/oauth2/authorize?${params}`);
}

exports.handler = async (event) => {
  const { request } = event.Records[0].cf;
  const uri = request.uri;
  const qs  = request.querystring || '';

  // Static assets bypass auth
  if (NO_AUTH.some(prefix => uri.startsWith(prefix))) return request;

  // Logout — clear cookie and redirect to Cognito logout endpoint
  if (uri === '/logout') {
    const params = new URLSearchParams({
      client_id:  CLIENT_ID,
      logout_uri: `https://${APP_DOMAIN}/`,
    });
    return makeRedirect(`https://${COGNITO_DOMAIN}/logout?${params}`, [
      'id_token=; Path=/; Expires=Thu, 01 Jan 1970 00:00:00 GMT; Secure; HttpOnly; SameSite=Lax',
    ]);
  }

  // OAuth2 callback — exchange authorization code for tokens
  if (uri === CALLBACK) {
    const params = new URLSearchParams(qs);
    const code   = params.get('code');
    const state  = params.get('state') || '/';

    if (!code) return { status: '400', body: 'Missing authorization code' };

    const [cognitoHost, ...pathParts] = `${COGNITO_DOMAIN}/oauth2/token`.split('/');
    try {
      const tokens = await httpsPost(cognitoHost, '/' + pathParts.join('/'), {
        grant_type:   'authorization_code',
        client_id:    CLIENT_ID,
        code,
        redirect_uri: `https://${APP_DOMAIN}${CALLBACK}`,
      });

      if (!tokens.id_token) {
        console.error('Token exchange failed:', JSON.stringify({ error: tokens.error }));
        return redirectToLogin(state);
      }

      const payload = JSON.parse(b64urlDecode(tokens.id_token.split('.')[1]));
      const maxAge  = payload.exp - Math.floor(Date.now() / 1000);

      return makeRedirect(`https://${APP_DOMAIN}${state}`, [
        `id_token=${tokens.id_token}; Path=/; Max-Age=${maxAge}; Secure; HttpOnly; SameSite=Lax`,
      ]);
    } catch (e) {
      console.error('Callback handler error:', e.message);
      return { status: '500', body: 'Authentication error. Please try again.' };
    }
  }

  // All other paths — require a valid id_token cookie
  const cookieHeader = request.headers.cookie?.[0]?.value || '';
  const idToken      = parseCookies(cookieHeader).id_token;

  if (!idToken) return redirectToLogin(uri);

  const isValid = await verifyIdToken(idToken);
  if (!isValid) return redirectToLogin(uri);

  return request;
};
