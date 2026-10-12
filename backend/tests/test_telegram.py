import asyncio

import httpx
import pytest

from telegram_bot import Telegram, TelegramError, reply_for


def test_private_start_is_honest():
    answer = reply_for({'chat': {'type': 'private'}, 'text': '/start'})
    assert 'пока недоступны' in answer
    assert reply_for({'chat': {'type': 'group'}, 'text': '/start'}) is None


def test_file_is_not_processed():
    assert 'не включена' in reply_for({'chat': {'type': 'private'}, 'document': {'file_id': 'example'}})


def test_error_does_not_expose_token():
    async def check():
        def handler(request):
            raise httpx.ConnectError(str(request.url))
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            with pytest.raises(TelegramError) as exc:
                await Telegram(client, 'secret-test-token').call('getMe')
            assert 'secret-test-token' not in str(exc.value)
    asyncio.run(check())
