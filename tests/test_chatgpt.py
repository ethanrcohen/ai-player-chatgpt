import json
from pathlib import Path
import tempfile
import time
from types import SimpleNamespace as NS
import unittest
from unittest.mock import patch, MagicMock
import jwt
from cryptography.hazmat.primitives.asymmetric import rsa
from bridge import chatgpt_auth as auth
from bridge.providers import ProviderConfig, get_provider
from bridge.providers import chatgpt


class AuthTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.store = Path(self.temp.name) / '.chatgpt/accounts.json'
        self.patcher = patch.object(auth, 'STORE', self.store)
        self.patcher.start()
        self.addCleanup(self.patcher.stop)

    def account(self, **changes):
        a = dict(client_id='oaiapp_test', subject='subject', access_token='secret-access',
                 refresh_token='secret-refresh', scopes=['chatgpt.tokens.use.direct'],
                 expires_at=time.time() + 3600, model='account-model')
        a.update(changes)
        with auth.locked():
            db = auth.read_store()
            db['accounts'][a['client_id']] = a
            db['active'] = a['client_id']
            auth.save_store(db)
        return a

    def test_callback_requires_state_and_issued_client(self):
        valid = {'state': ['state'], 'client_id': ['oaiapp_test'], 'code': ['code']}
        self.assertEqual(auth.callback_result(valid, 'state'), ('oaiapp_test', 'code'))
        for change in ({'state': ['wrong']}, {'client_id': ['dynamic_agent_client']},
                       {'code': []}, {'error': ['access_denied']}):
            with self.assertRaises(auth.ChatGPTError):
                auth.callback_result(valid | change, 'state')
        with self.assertRaises(auth.ChatGPTError):
            auth.callback_result(valid, 'state', 'other-client')
        self.assertEqual(auth.callback_result({'state': ['state'], 'code': ['code']},
                                             'state', 'saved'), ('saved', 'code'))

    def test_private_storage_and_stable_host(self):
        self.account()
        self.assertEqual(self.store.stat().st_mode & 0o777, 0o600)
        self.assertEqual(self.store.parent.stat().st_mode & 0o777, 0o700)
        self.assertEqual(auth.read_store()['host_id'], auth.read_store()['host_id'])

    def test_identity_signature_nonce_audience_and_subject(self):
        key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        claims = dict(sub='subject', iss=auth.ISSUER, aud='client', nonce='nonce',
                      iat=int(time.time()), exp=int(time.time())+60)
        token = jwt.encode(claims, key, algorithm='RS256')
        with patch('jwt.PyJWKClient') as jwks:
            jwks.return_value.get_signing_key_from_jwt.return_value = NS(key=key.public_key())
            self.assertEqual(auth.validate_identity(token, 'client', 'nonce')['sub'], 'subject')
            for client, nonce, subject in [('wrong', 'nonce', None), ('client', 'bad', None), ('client', 'nonce', 'other')]:
                with self.assertRaises(auth.ChatGPTError):
                    auth.validate_identity(token, client, nonce, subject)
            bad_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
            with self.assertRaises(auth.ChatGPTError):
                auth.validate_identity(jwt.encode(claims, bad_key, algorithm='RS256'), 'client', 'nonce')

    def test_refresh_rotates_atomically_and_keeps_scope(self):
        self.account(expires_at=0)
        with patch.object(auth, 'request_json', return_value=dict(access_token='new-access', refresh_token='new-refresh', token_type='Bearer', expires_in=3600)) as call:
            a = auth.active_account()
            self.assertEqual(a['refresh_token'], 'new-refresh')
            self.assertEqual(auth.read_store()['accounts']['oaiapp_test']['access_token'], 'new-access')
            self.assertNotIn('scope', call.call_args.args[1])
            self.assertEqual(call.call_args.args[1]['client_id'], 'oaiapp_test')

    def test_missing_or_removed_permission_blocks_inference(self):
        self.account(scopes=[])
        with self.assertRaises(auth.ChatGPTError):
            auth.active_account()
        self.account(expires_at=0)
        with patch.object(auth, 'request_json', return_value=dict(access_token='new', token_type='Bearer', expires_in=3600, scope='openid')):
            with self.assertRaises(auth.ChatGPTError):
                auth.active_account()
        self.assertEqual(auth.read_store()['accounts']['oaiapp_test']['scopes'], ['openid'])

    def test_model_selection_uses_account_catalog(self):
        self.account()
        with patch.object(auth, 'models', return_value=[{'slug':'available'}]):
            with self.assertRaises(auth.ChatGPTError):
                auth.choose_model('unavailable')
            auth.choose_model('available')
        self.assertEqual(auth.active_account()['model'], 'available')

    def test_missing_auth_fails_closed(self):
        with self.assertRaises(auth.ChatGPTError):
            auth.active_account()


class ProviderTests(unittest.TestCase):
    def run_stream(self, events):
        client = MagicMock()
        client.__enter__.return_value = client
        client.responses.create.return_value.__enter__.return_value = iter(events)
        return client

    def test_completed_stream_and_request_contract(self):
        client = self.run_stream([NS(type='response.output_text.delta', delta='partial'),
                                  NS(type='response.completed', response=NS(output_text='[{"action":"wait"}]'))])
        with patch.object(chatgpt, 'active_account', return_value={'access_token':'secret','model':'account-model'}), patch.object(chatgpt, 'OpenAI', return_value=client) as sdk:
            result = chatgpt.complete([{'role':'system','content':'rules'}, {'role':'user','content':'state'}], ProviderConfig(provider='chatgpt', model='auto'))
            self.assertEqual(json.loads(result), [{'action':'wait'}])
            args = client.responses.create.call_args.kwargs
            self.assertEqual(set(args), {'model', 'input', 'store', 'stream'})
            self.assertEqual(args['input'][0]['role'], 'developer')
            self.assertFalse(args['store'])
            self.assertTrue(args['stream'])
            self.assertEqual(args['model'], 'account-model')
            self.assertEqual(sdk.call_args.kwargs['base_url'], auth.RESOURCE)

    def test_partial_failure_and_incomplete_never_return_actions(self):
        for events in ([NS(type='response.output_text.delta', delta='[]')],
                       [NS(type='response.incomplete')], [NS(type='error')],
                       [NS(type='response.failed')]):
            with patch.object(chatgpt, 'active_account', return_value={'access_token':'secret','model':'m'}), patch.object(chatgpt, 'OpenAI', return_value=self.run_stream(events)):
                with self.assertRaises(auth.ChatGPTError):
                    chatgpt.complete([], ProviderConfig(model='auto'))

    def test_routing_preserves_other_providers(self):
        self.assertIs(get_provider(ProviderConfig(provider='chatgpt')), chatgpt.complete)
        self.assertEqual(get_provider(ProviderConfig(provider='openai')).__module__, 'bridge.providers.openai_compat')


if __name__ == '__main__':
    unittest.main()

class CliRegressionTests(unittest.TestCase):
    def test_module_entrypoint_catches_provider_error(self):
        import contextlib
        import io
        import runpy
        import sys
        error = io.StringIO()
        with patch.object(chatgpt, 'active_account', side_effect=auth.ChatGPTError('Run login first.')), patch.object(sys, 'argv', ['chatgpt_auth', 'test']), contextlib.redirect_stderr(error):
            with self.assertRaises(SystemExit) as result:
                runpy.run_module('bridge.chatgpt_auth', run_name='__main__')
        self.assertEqual(result.exception.code, 1)
        self.assertIn('Run login first.', error.getvalue())
        self.assertNotIn('Traceback', error.getvalue())

    def test_identity_diagnostics_do_not_expose_secrets(self):
        with patch('jwt.PyJWKClient') as client:
            client.return_value.get_signing_key_from_jwt.side_effect = jwt.PyJWKClientConnectionError('secret-token-in-raw-error')
            with self.assertRaisesRegex(auth.ChatGPTError, 'could not download OpenAI signing keys') as result:
                auth.validate_identity('secret-token', 'client')
            self.assertNotIn('secret-token', str(result.exception))


class ClockSkewTests(unittest.TestCase):
    def test_small_drift_allowed_but_future_and_expired_tokens_rejected(self):
        key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        now = int(time.time())
        claims = dict(sub='subject', iss=auth.ISSUER, aud='client', nonce='nonce',
                      iat=now+9, nbf=now+9, exp=now+3600)
        with patch('jwt.PyJWKClient') as jwks:
            jwks.return_value.get_signing_key_from_jwt.return_value = NS(key=key.public_key())
            token = jwt.encode(claims, key, algorithm='RS256')
            self.assertEqual(auth.validate_identity(token, 'client', 'nonce')['sub'], 'subject')
            for changes in ({'iat': now+90}, {'nbf': now+90},
                            {'iat': now-3600, 'nbf': now-3600, 'exp': now-90}):
                with self.subTest(changes=changes), self.assertRaises(auth.ChatGPTError):
                    auth.validate_identity(jwt.encode(claims | changes, key, algorithm='RS256'), 'client', 'nonce')
