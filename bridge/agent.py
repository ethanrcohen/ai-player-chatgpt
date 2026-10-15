"""
Router agent (v3).

Agent.decide() builds the small router prompt from raw perception, calls the
LLM, parses + validates the response into a list of skill/primitive entries,
and returns it. Skills are validated against api.SKILLS, primitives against
api.ACTIONS (see factorio/api.py). Keeps a short history of execution results.
"""

from collections import deque
import json
import logging
import time

from .config import BridgeConfig
from .factorio.api import parse_response, validate
from .investigation import investigate
from .factorio.watcher import PendingRequest
from .metrics import STATUS_OK, STATUS_PARSE_ERROR, STATUS_TIMEOUT, record_exchange
from .prompt import build_messages
from .providers import get_provider
from .transcript import log_exchange

log = logging.getLogger(__name__)


def _fallback(reason: str) -> list[dict]:
    """Safe response so the mod never hangs; the summary states the real reason."""
    return [
        {"action": "wait"},
        {"action": "summary", "text": f"No action this turn — {reason}."},
    ]


class Agent:
    def __init__(self, query=None, observation_only=False):
        self._query = query
        self._observation_only = observation_only
        self._recent_results = deque(maxlen=8)
        self._previous_entries = None

    def decide(self, req: PendingRequest, config: BridgeConfig) -> list[dict]:
        provider_cfg = config.to_provider_config()
        complete = get_provider(provider_cfg)

        payload = req.payload
        if "perception" not in payload:
            log.error("Request %s: no 'perception' in payload — fallback", req.req_id)
            return _fallback("internal error: request had no perception")

        results = (payload.get("memory") or {}).get("last_action_results")
        if results:
            self._recent_results.append({"issued": self._previous_entries, "results": results})
        # Keep a short record across turns: only the latest result is included
        # by the mod, which otherwise makes completed construction disappear
        # from the model's context as soon as it moves to the next step.
        contextual_payload = dict(payload, recent_execution_history=list(self._recent_results))
        messages = build_messages(contextual_payload, config.system_prefix)

        log.info("Routing request %s via %s (model=%s)",
                 req.req_id, provider_cfg.provider, provider_cfg.model)
        t0 = time.monotonic()
        if self._observation_only:
            messages[0]['content'] += '\nObservation-only session: investigate and explain findings with chat or summary actions. No construction, transfers, research, or movement.'
        content = (investigate(messages, complete, provider_cfg, self._query)
                   if self._query else complete(messages, provider_cfg))
        elapsed = time.monotonic() - t0

        if content is None:
            log.error("Request %s: no response after %.1fs (timeout=%.0fs) — fallback. "
                      "Raise AI_TIMEOUT or use a faster model.",
                      req.req_id, elapsed, provider_cfg.timeout)
            actions = _fallback(f"model did not respond within {provider_cfg.timeout:.0f}s "
                                f"(raise AI_TIMEOUT or use a faster model)")
            log_exchange(config.output_dir, req.req_id, messages, None, elapsed, actions)
            record_exchange(config.output_dir, req.req_id, payload, None, STATUS_TIMEOUT,
                            elapsed, provider_cfg.provider, provider_cfg.model)
            return actions

        log.info("Request %s: response in %.1fs (%d chars)", req.req_id, elapsed, len(content))
        log.debug("Request %s raw: %s", req.req_id, content[:600])

        if self._query:
            # Reject the entire dependent batch if any entry is invalid.
            try:
                raw = json.loads(content)
                entries = validate(raw) if isinstance(raw, list) else None
                if not entries or len(entries) != len(raw):
                    entries = None
            except (ValueError, TypeError):
                entries = None
        else:
            entries = parse_response(content)
        if entries and self._observation_only and any(
            e.get('action') not in {'chat', 'summary', 'wait'} or 'skill' in e
            for e in entries
        ):
            return _fallback('observation-only mode blocked a modifying action')
        if entries is None:
            log.warning("Request %s: could not parse skills/actions — fallback. Raw: %s",
                        req.req_id, content[:600])
            log_exchange(config.output_dir, req.req_id, messages, content, elapsed, None)
            record_exchange(config.output_dir, req.req_id, payload, None, STATUS_PARSE_ERROR,
                            elapsed, provider_cfg.provider, provider_cfg.model)
            return _fallback("could not parse a valid skill/action array from the model's reply")

        self._previous_entries = entries
        entries = self._label_chat(entries, provider_cfg.provider)
        log.info("Request %s: %d entr(ies) [%s]", req.req_id, len(entries),
                 ", ".join(e.get("skill") or e.get("action", "?") for e in entries))
        log_exchange(config.output_dir, req.req_id, messages, content, elapsed, entries)
        record_exchange(config.output_dir, req.req_id, payload, entries, STATUS_OK,
                        elapsed, provider_cfg.provider, provider_cfg.model)
        return entries

    @staticmethod
    def _label_chat(entries: list[dict], provider: str) -> list[dict]:
        """Prefix each chat action's message with the provider name."""
        labels = {"chatgpt": "[ChatGPT]", "anthropic": "[Claude]", "openai": "[GPT]", "lmstudio": "[Local]", "custom": "[Custom]"}
        label = labels.get(provider, "[AI]")
        for e in entries:
            if e.get("action") == "chat" and e.get("message"):
                e["message"] = f"{label} {e['message']}"
        return entries
