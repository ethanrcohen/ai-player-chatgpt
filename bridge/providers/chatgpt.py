"""ChatGPT plan requests through the public Responses API; no API-key fallback."""
from openai import OpenAI, APIError
from ..chatgpt_auth import active_account, ChatGPTError, RESOURCE


def complete(messages, config):
    account = active_account()
    model = account.get('model') if config.model in ('auto', 'local-model', '') else config.model
    if not model:
        raise ChatGPTError('Choose a model with python -m bridge.chatgpt_auth select-model.')
    inputs = []
    for message in messages:
        role = 'developer' if message['role'] == 'system' else message['role']
        inputs.append({'role': role, 'content': message['content']})
    try:
        with OpenAI(api_key=account['access_token'], base_url=RESOURCE,
                    timeout=config.timeout, max_retries=0) as client:
            with client.responses.create(model=model, input=inputs, store=False,
                                         stream=True) as stream:
                chunks = []
                for event in stream:
                    if event.type == 'response.output_text.delta':
                        chunks.append(event.delta)
                    if event.type == 'response.completed':
                        text = event.response.output_text or "".join(chunks)
                        if not text:
                            raise ChatGPTError('ChatGPT completed without action text. Bridge stopped.')
                        return text
                    if event.type in ('response.failed', 'response.incomplete', 'error'):
                        raise ChatGPTError('ChatGPT did not complete the request. Bridge stopped; no partial actions were sent.')
        raise ChatGPTError('ChatGPT stream ended before completion. Bridge stopped.')
    except APIError as exc:
        if getattr(exc, 'code', None) == 'subscription_sharing_usage_limit_exceeded':
            raise ChatGPTError(
                'ChatGPT subscription-sharing usage limit reached. Check ChatGPT '
                'Settings → Usage for your allowance or reset time, then restart '
                'the bridge. Signing in again will not reset this limit. '
                'No API billing fallback was used.'
            ) from None
        status = getattr(exc, 'status_code', None)
        raise ChatGPTError(f'ChatGPT request failed (HTTP {status or "unavailable"}). Check sign-in and ChatGPT Settings → Usage. No API billing fallback was used.') from None
