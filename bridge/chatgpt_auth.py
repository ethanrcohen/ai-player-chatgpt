"""Local ChatGPT plan authorization. Tokens never enter Factorio settings."""
import argparse
import base64
from contextlib import contextmanager
import fcntl
import hashlib
from http.server import BaseHTTPRequestHandler, HTTPServer
import json
import os
from pathlib import Path
import secrets
import tempfile
import time
from urllib.parse import parse_qs, urlencode, urlparse
from urllib.request import Request, urlopen
from urllib.error import HTTPError, URLError
import uuid
import webbrowser

ISSUER = 'https://auth.openai.com'
RESOURCE = 'https://api.openai.com/v1'
SCOPES = 'openid profile email offline_access resource.invoke chatgpt.tokens.use.direct'
# Allow modest clock drift without disabling JWT time validation.
CLOCK_SKEW_SECONDS = 30
STORE = Path(__file__).parent / '.chatgpt' / 'accounts.json'


class ChatGPTError(RuntimeError):
    """Safe, user-facing error that contains no credentials."""


def request_json(url, form=None, token=None):
    headers = {'Accept': 'application/json'}
    if token:
        headers['Authorization'] = 'Bearer ' + token
    data = None if form is None else urlencode(form).encode()
    try:
        with urlopen(Request(url, data=data, headers=headers), timeout=30) as r:
            return json.load(r)
    except HTTPError as exc:
        raise ChatGPTError(f'OpenAI request failed (HTTP {exc.code}). Sign in again or check ChatGPT Settings → Usage.') from None
    except (URLError, TimeoutError, ValueError):
        raise ChatGPTError('Cannot reach OpenAI or received an invalid response. Try again.') from None


@contextmanager
def locked():
    STORE.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    os.chmod(STORE.parent, 0o700)
    fd = os.open(STORE.parent / 'accounts.lock', os.O_CREAT | os.O_RDWR, 0o600)
    with os.fdopen(fd, 'w') as f:
        fcntl.flock(f, fcntl.LOCK_EX)
        yield


def read_store():
    if STORE.exists():
        return json.loads(STORE.read_text())
    return {'host_id': 'urn:uuid:' + str(uuid.uuid4()), 'accounts': {}, 'active': None}


def save_store(data):
    fd, name = tempfile.mkstemp(dir=STORE.parent)
    try:
        with os.fdopen(fd, 'w') as f:
            json.dump(data, f)
            f.flush()
            os.fsync(f.fileno())
        os.replace(name, STORE)
    finally:
        if os.path.exists(name):
            os.unlink(name)


def validate_identity(raw, client_id, nonce=None, subject=None):
    import jwt
    try:
        key = jwt.PyJWKClient(ISSUER + '/.well-known/jwks.json', timeout=30).get_signing_key_from_jwt(raw)
        claims = jwt.decode(raw, key.key, algorithms=['RS256'], audience=client_id,
                            issuer=ISSUER, leeway=CLOCK_SKEW_SECONDS,
                            options={'require': ['sub', 'exp', 'iat']})
        if not isinstance(claims['sub'], str) or not claims['sub']:
            raise ChatGPTError('OpenAI ID token is missing a valid account identity.')
        if nonce is not None and claims.get('nonce') != nonce:
            raise ChatGPTError('OpenAI ID token nonce did not match this sign-in attempt. Retry login.')
        if subject is not None and claims['sub'] != subject:
            raise ChatGPTError('OpenAI returned a different account. Use add-account to sign in separately.')
        return claims
    except ChatGPTError:
        raise
    except Exception as exc:
        # Emit only controlled categories, never JWTs, claims, keys or raw errors.
        reasons = {
            'PyJWKClientConnectionError': 'could not download OpenAI signing keys; check your network',
            'PyJWKClientError': 'no matching OpenAI signing key was found',
            'InvalidAudienceError': 'the ID token audience did not match the issued client ID',
            'InvalidIssuerError': 'the ID token issuer did not match OpenAI',
            'ExpiredSignatureError': 'the ID token expired; retry login and check your system clock',
            'ImmatureSignatureError': 'the ID token is not yet valid; check your system clock',
            'InvalidSignatureError': 'the ID token signature could not be verified',
            'InvalidAlgorithmError': 'the ID token used an unsupported signing algorithm',
            'MissingRequiredClaimError': 'the ID token is missing a required identity claim',
            'DecodeError': 'the ID token is missing or malformed',
        }
        reason = reasons.get(type(exc).__name__, 'unexpected identity verification error')
        raise ChatGPTError(f'OpenAI identity validation failed: {reason}. Existing credentials were preserved.') from None


def token_record(tokens, previous=None):
    record = dict(previous or {})
    if str(tokens.get('token_type', '')).lower() != 'bearer' or not tokens.get('access_token'):
        raise ChatGPTError('OpenAI did not return a usable access token.')
    record.update({k: tokens[k] for k in ('access_token', 'refresh_token', 'id_token') if tokens.get(k)})
    if 'scope' in tokens:
        record['scopes'] = tokens['scope'].split()
    elif not previous:
        record['scopes'] = []
    record['expires_at'] = time.time() + float(tokens.get('expires_in', 0))
    return record


def callback_result(query, state, existing_id=None):
    if query.get('state') != [state]:
        raise ChatGPTError('Sign-in state did not match. Please retry.')
    if 'error' in query:
        raise ChatGPTError('Sign-in was declined or failed. No account was changed.')
    ids = query.get('client_id', [existing_id])
    client_id = ids[0] if len(ids) == 1 else None
    if not client_id or client_id == 'dynamic_agent_client':
        raise ChatGPTError('Registration did not return an issued client ID.')
    if existing_id and existing_id != client_id:
        raise ChatGPTError('Sign-in returned a different account registration.')
    codes = query.get('code', [])
    if len(codes) != 1 or not codes[0]:
        raise ChatGPTError('Sign-in did not return an authorization code.')
    return client_id, codes[0]


def login(new=False):
    with locked():
        db = read_store()
        save_store(db)  # Persist host identity before authorization.
        previous = None if new else db['accounts'].get(db['active'])
        host = db['host_id']
    state, nonce, verifier = (secrets.token_urlsafe(32) for _ in range(3))
    result = {}

    class Callback(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass  # Callback URLs contain authorization codes.

        def do_GET(self):
            parsed = urlparse(self.path)
            if parsed.path != '/auth/callback':
                self.send_error(404)
                return
            query = parse_qs(parsed.query)
            if query.get('state') != [state]:
                self.send_error(400, 'Invalid sign-in state')
                return
            result.update(query)
            self.send_response(200)
            self.send_header('Content-Type', 'text/plain; charset=utf-8')
            self.send_header('Cache-Control', 'no-store')
            self.end_headers()
            self.wfile.write(b'Return to AI Player ChatGPT to finish sign-in. You may close this tab.')

    with HTTPServer(('127.0.0.1', 0), Callback) as server:
        server.timeout = 1
        redirect = f'http://127.0.0.1:{server.server_port}/auth/callback'
        params = dict(client_id=previous['client_id'] if previous else 'dynamic_agent_client',
                      ext_agent_host_id=host, response_type='code', redirect_uri=redirect,
                      scope=SCOPES, resource=RESOURCE, state=state, nonce=nonce,
                      code_challenge_method='S256',
                      code_challenge=base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).decode().rstrip('='))
        if previous:
            if previous.get('id_token'):
                params['id_token_hint'] = previous['id_token']
        else:
            params['agent_name_hint'] = 'AI Player ChatGPT'
        print('Continue with ChatGPT — opening your browser. Approve plan usage there.', flush=True)
        if not webbrowser.open(ISSUER + '/api/accounts/authorize?' + urlencode(params)):
            raise ChatGPTError('Could not open the browser. Run this command in a desktop terminal.')
        deadline = time.monotonic() + 300
        while not result and time.monotonic() < deadline:
            server.handle_request()
        if not result:
            raise ChatGPTError('Sign-in timed out. Run login again.')
    client_id, code = callback_result(result, state, previous['client_id'] if previous else None)
    tokens = request_json(ISSUER + '/api/accounts/oauth/token', dict(
        grant_type='authorization_code', client_id=client_id, code=code,
        code_verifier=verifier, redirect_uri=redirect, resource=RESOURCE))
    claims = validate_identity(tokens.get('id_token', ''), client_id, nonce,
                               previous['subject'] if previous else None)
    record = token_record(tokens)
    record.update(client_id=client_id, subject=claims['sub'], email=claims.get('email', ''),
                  model=(previous or {}).get('model', ''))
    with locked():
        db = read_store()
        db['accounts'][client_id] = record
        db['active'] = client_id
        save_store(db)
    if 'chatgpt.tokens.use.direct' not in record['scopes']:
        raise ChatGPTError('Signed in, but ChatGPT plan usage was not granted. Run login to enable it.')
    print('You’re using your ChatGPT plan. Manage limits and access in ChatGPT Settings → Usage.')
    choose_model()


def active_account():
    with locked():
        db = read_store()
        account = db['accounts'].get(db['active'])
        if not account or not account.get('access_token'):
            raise ChatGPTError('Run python -m bridge.chatgpt_auth login first.')
        if 'chatgpt.tokens.use.direct' not in account.get('scopes', []):
            raise ChatGPTError('ChatGPT plan permission is missing. Run login again.')
        if account['expires_at'] <= time.time() + 60:
            if not account.get('refresh_token'):
                raise ChatGPTError('ChatGPT session expired. Run login again.')
            tokens = request_json(ISSUER + '/api/accounts/oauth/token', dict(
                grant_type='refresh_token', client_id=account['client_id'],
                refresh_token=account['refresh_token'], resource=RESOURCE))
            if tokens.get('id_token'):
                validate_identity(tokens['id_token'], account['client_id'], subject=account['subject'])
            updated = token_record(tokens, account)
            db['accounts'][db['active']] = updated
            save_store(db)
            account = updated
        if 'chatgpt.tokens.use.direct' not in account.get('scopes', []):
            raise ChatGPTError('ChatGPT plan permission was removed. Run login again.')
        return dict(account)


def models(account):
    payload = request_json(RESOURCE + '/models', token=account['access_token'])
    return [m for m in payload.get('models', []) if m.get('visibility') == 'list' and m.get('slug')]


def choose_model(slug=None):
    account = active_account()
    available = models(account)
    if not available:
        raise ChatGPTError('No models are available for this ChatGPT account.')
    if slug is None:
        for i, model in enumerate(available, 1):
            print(f"{i}. {model.get('display_name', model['slug'])} ({model['slug']})")
        raw = input('Choose model number: ').strip()
        if not raw.isdigit() or not 1 <= int(raw) <= len(available):
            raise ChatGPTError('Invalid model selection. Run select-model again.')
        slug = available[int(raw) - 1]['slug']
    if slug not in [m['slug'] for m in available]:
        raise ChatGPTError('That model is not available to this ChatGPT account.')
    with locked():
        db = read_store()
        db['accounts'][account['client_id']]['model'] = slug
        save_store(db)
    print('Selected model:', slug)


def logout():
    with locked():
        db = read_store()
        account = db['accounts'].get(db['active'])
        if not account:
            return
        revoked = False
        try:
            if account.get('refresh_token'):
                discovery = request_json(ISSUER + '/.well-known/openid-configuration')
                endpoint = discovery['revocation_endpoint']
                if not endpoint.startswith(ISSUER + '/'):
                    raise ChatGPTError('Unexpected revocation endpoint.')
                form = urlencode(dict(token=account['refresh_token'], token_type_hint='refresh_token', client_id=account['client_id'])).encode()
                for attempt in range(3):
                    try:
                        with urlopen(Request(endpoint, data=form), timeout=15) as response:
                            revoked = response.status == 200
                        break
                    except (HTTPError, URLError, TimeoutError):
                        if attempt < 2:
                            time.sleep(2 ** attempt)
        except (ChatGPTError, KeyError):
            pass
        for key in ('access_token', 'refresh_token', 'id_token'):
            account.pop(key, None)
        save_store(db)
    print('Signed out.' if revoked else 'Signed out locally. Remote revocation was not confirmed; disconnect the app in ChatGPT settings.')


def main():
    parser = argparse.ArgumentParser(description='AI Player ChatGPT subscription setup')
    parser.add_argument('command', choices=['login', 'add-account', 'accounts', 'use-account', 'models', 'select-model', 'logout', 'test'])
    parser.add_argument('value', nargs='?')
    args = parser.parse_args()
    try:
        if args.command in ('login', 'add-account'):
            login(args.command == 'add-account')
        elif args.command == 'logout':
            logout()
        elif args.command == 'select-model':
            choose_model(args.value)
        elif args.command == 'models':
            for model in models(active_account()):
                print(model['slug'], model.get('display_name', ''))
        elif args.command in ('accounts', 'use-account'):
            with locked():
                db = read_store()
                if args.command == 'use-account':
                    if args.value not in db['accounts']:
                        raise ChatGPTError('Choose an issued client ID listed by accounts.')
                    db['active'] = args.value
                    save_store(db)
                for client_id, a in db['accounts'].items():
                    print('*' if client_id == db['active'] else ' ', client_id, a.get('email'), a.get('model'))
        elif args.command == 'test':
            from .providers.chatgpt import complete
            from .providers import ProviderConfig
            print(complete([{'role': 'user', 'content': 'Reply with exactly: Connected'}], ProviderConfig(provider='chatgpt', model='auto')))
    except ChatGPTError as exc:
        parser.exit(1, str(exc) + '\n')


if __name__ == '__main__':
    # Use one canonical module so provider and CLI share the same error class.
    from bridge.chatgpt_auth import main as cli_main
    cli_main()
