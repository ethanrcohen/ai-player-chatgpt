import json
import unittest
from types import SimpleNamespace
from unittest.mock import Mock, patch
from bridge.investigation import investigate, validate_query
from bridge.providers import ProviderConfig
from bridge.agent import Agent
from bridge.config import BridgeConfig


class InvestigationTests(unittest.TestCase):
    def messages(self):
        return [{'role':'system','content':'rules'}, {'role':'user','content':'Build engines'}]

    def test_query_evidence_reaches_next_decision(self):
        complete = Mock(side_effect=[json.dumps({'queries':[{'name':'get_recipe','params':{'name':'engine-unit'}}]}), '[{"action":"chat","message":"Need steel"}]'])
        query = Mock(return_value={'ingredients':[{'name':'steel-plate','amount':1}]})
        messages = self.messages()
        answer = investigate(messages, complete, ProviderConfig(), query)
        query.assert_called_once_with('get_recipe', {'name':'engine-unit'})
        self.assertIn('steel-plate', messages[-1]['content'])
        self.assertEqual(json.loads(answer)[0]['message'], 'Need steel')

    def test_mixed_queries_and_actions_do_not_dispatch(self):
        complete = Mock(side_effect=['{"queries":[{"name":"get_character_state","params":{}}],"actions":[{"action":"mine"}]}', '[{"action":"wait"}]'])
        query = Mock()
        investigate(self.messages(), complete, ProviderConfig(), query)
        query.assert_not_called()

    def test_invalid_keys_and_nonfinite_values_rejected(self):
        for params in ({'x':float('nan'),'y':1}, {'x':True,'y':1}, {'x':1,'y':1,'injected);game.print(1)--':0}):
            with self.assertRaises(ValueError):
                validate_query({'name':'inspect_entity','params':params})
        with self.assertRaises(ValueError):
            validate_query({'name':'run_primitive','params':{}})

    def test_query_rounds_are_bounded_and_errors_are_observable(self):
        complete = Mock(return_value='{"queries":[{"name":"get_character_state","params":{}}]}')
        query = Mock(return_value={'error':'RCON unavailable'})
        messages = self.messages()
        self.assertIsNone(investigate(messages, complete, ProviderConfig(), query))
        self.assertEqual(query.call_count, 4)
        self.assertEqual(complete.call_count, 5)
        self.assertIn('RCON unavailable', messages[-1]['content'])

    def test_shared_time_budget_not_reset_for_each_completion(self):
        times = iter([0, 0, 2, 6])
        complete = Mock(return_value='{"queries":[{"name":"get_character_state","params":{}}]}')
        query = Mock(return_value={})
        self.assertIsNone(investigate(self.messages(), complete, ProviderConfig(timeout=5), query, clock=lambda:next(times)))
        self.assertEqual(complete.call_count, 1)

    def decide(self, answer, observe):
        agent = Agent(query=Mock(), observation_only=observe)
        req = SimpleNamespace(req_id='test', payload={'perception':{'tick':1}})
        with patch('bridge.agent.get_provider', return_value=Mock(return_value=answer)), patch('bridge.agent.log_exchange'), patch('bridge.agent.record_exchange'):
            return agent.decide(req, BridgeConfig())

    def test_observation_mode_blocks_whole_mutating_batch(self):
        result = self.decide('[{"action":"chat","message":"Building"},{"action":"place","item":"assembling-machine-1","position":{"x":1,"y":1}}]', True)
        self.assertEqual(result[0], {'action':'wait'})
        self.assertIn('blocked',result[1]['text'])

    def test_invalid_step_cannot_leave_dependent_actions_running(self):
        result = self.decide('[{"action":"goto","position":{"x":1,"y":1}},{"action":"mine"}]', False)
        self.assertEqual(result[0], {'action':'wait'})
        self.assertFalse(any(a.get('action')=='mine' for a in result))

    def test_observation_report_is_allowed(self):
        result = self.decide('[{"action":"chat","message":"Supply not established"}]', True)
        self.assertIn('Supply not established',result[0]['message'])
