import unittest
from unittest.mock import MagicMock, patch

from openai import APIError
from bridge.chatgpt_auth import ChatGPTError
from bridge.factorio.rcon import RCONGateway
from bridge.providers import ProviderConfig, chatgpt


class RecoveryTests(unittest.TestCase):
    def test_restart_clears_mod_queue_through_mod_command(self):
        gateway = RCONGateway('localhost', 27015, 'test')
        with patch.object(gateway, '_send', return_value='') as send:
            self.assertTrue(gateway.clear_pending_requests())
            send.assert_called_once_with('/ai-clear')
        with patch.object(gateway, '_send', return_value=None):
            self.assertFalse(gateway.clear_pending_requests())

    def test_stream_usage_limit_has_actionable_private_diagnostic(self):
        error = APIError('private server detail', request=MagicMock(),
                         body={'code': 'subscription_sharing_usage_limit_exceeded'})
        def events():
            raise error
            yield
        client = MagicMock()
        client.__enter__.return_value = client
        client.responses.create.return_value.__enter__.return_value = events()
        with patch.object(chatgpt, 'active_account', return_value={'access_token': 'secret', 'model': 'm'}), patch.object(chatgpt, 'OpenAI', return_value=client):
            with self.assertRaisesRegex(ChatGPTError, 'subscription-sharing usage limit reached') as result:
                chatgpt.complete([], ProviderConfig(model='auto'))
        self.assertNotIn('private server detail', str(result.exception))
        self.assertNotIn('secret', str(result.exception))

class GoalPriorityTests(unittest.TestCase):
    def test_explicit_goal_overrides_unrelated_construction(self):
        from bridge.prompt import build_messages
        for memory in ({'long_term_goal': 'Build engines'}, {'user_directive': 'Build engines'}):
            text = build_messages({'memory': memory, 'perception': {'ghosts': 400, 'deconstruction': 76}})[1]['content']
            self.assertIn('Focus on the player', text)
            self.assertNotIn('MUST be exactly', text)

    def test_default_construction_still_works_without_goal(self):
        from bridge.prompt import build_messages
        text = build_messages({'perception': {'ghosts': 2}})[1]['content']
        self.assertIn('MUST be exactly [{"skill":"build_ghosts"}]', text)

class ExecutionHistoryTests(unittest.TestCase):
    def test_history_preserves_completed_work_for_later_turns(self):
        from types import SimpleNamespace
        from bridge.agent import Agent
        from bridge.config import BridgeConfig
        agent = Agent()
        cfg = BridgeConfig()
        with patch('bridge.agent.get_provider', return_value=lambda messages, config: '[{"action":"wait"}]'), patch('bridge.agent.log_exchange'), patch('bridge.agent.record_exchange'), patch('bridge.agent.build_messages', return_value=[]) as build:
            agent.decide(SimpleNamespace(req_id='one', payload={'perception': {'tick': 1}, 'memory': {'last_action_results': [{'ok': True, 'detail': 'placed assembling-machine-1'}]}}), cfg)
            agent.decide(SimpleNamespace(req_id='two', payload={'perception': {'tick': 2}, 'memory': {'last_action_results': [{'ok': True, 'detail': 'connected power'}]}}), cfg)
            history = build.call_args.args[0]['recent_execution_history']
            self.assertEqual(history[0]['results'][0]['detail'], 'placed assembling-machine-1')
            self.assertEqual(history[1]['issued'], [{'action': 'wait'}])
