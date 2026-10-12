"""Telegram transport scaffold. Business flow awaits the updated specification."""
import argparse
import asyncio
import logging
import os
from pathlib import Path

import httpx
from dotenv import load_dotenv

load_dotenv(Path(__file__).with_name('.env'))
# Telegram URLs contain the token. Never enable HTTP request debug logging.
for name in ('httpx', 'httpcore'):
    logging.getLogger(name).disabled = True


class TelegramError(Exception):
    def __init__(self, code):
        self.code = code
        super().__init__(f'Telegram API error ({code}); credentials omitted')


class Telegram:
    def __init__(self, client, token):
        self.client = client
        self._base = f'https://api.telegram.org/bot{token}/'

    async def call(self, method, **payload):
        try:
            response = await self.client.post(self._base + method, json=payload)
            data = response.json()
        except (httpx.RequestError, ValueError):
            raise TelegramError('NETWORK_OR_RESPONSE') from None
        if not isinstance(data, dict) or not data.get('ok'):
            raise TelegramError(data.get('error_code', response.status_code) if isinstance(data, dict) else 'BAD_RESPONSE')
        return data['result']


def reply_for(message):
    if message.get('chat', {}).get('type') != 'private':
        return None
    command = (message.get('text') or '').split(maxsplit=1)
    command = command[0].split('@')[0] if command else ''
    if command == '/start':
        return ('Привет! Это Deadline Buddy — помощник по учебным заданиям. '
                'Бот готовится к запуску. Разбор заданий и планирование пока недоступны. '
                'Не отправляй задания или личные данные до запуска. /help — справка.')
    if command == '/help':
        return ('Доступны /start и /help. Бот пока находится в подготовке. '
                'Разбор текста/PDF, чек-лист и дедлайн появятся после обновления требований.')
    return 'Обработка заданий пока не включена. Статус проекта — /help.'


async def run(check=False):
    token = os.getenv('TELEGRAM_BOT_TOKEN', '').strip()
    expected = os.getenv('TELEGRAM_BOT_USERNAME', 'Deadlinebud_bot').lstrip('@')
    if not token:
        print('Set TELEGRAM_BOT_TOKEN in backend/.env')
        return 1
    async with httpx.AsyncClient(timeout=40, follow_redirects=False) as client:
        api = Telegram(client, token)
        me = await api.call('getMe')
        if not me.get('is_bot') or me.get('username', '').lower() != expected.lower():
            print('Bot identity mismatch; stopped.')
            return 1
        info = await api.call('getWebhookInfo')
        print(f"Bot verified: @{me['username']} (id={me['id']})")
        print(f"Webhook configured: {bool(info.get('url'))}")
        print(f"Pending updates: {info.get('pending_update_count', 0)}")
        if check:
            return 0
        if info.get('url'):
            print('Polling refused: existing webhook preserved.')
            return 1
        print('Preparation-mode polling started. Stop with Ctrl+C.')
        offset = None
        while True:
            try:
                payload = {'timeout': 25, 'allowed_updates': ['message']}
                if offset is not None:
                    payload['offset'] = offset
                updates = await api.call('getUpdates', **payload)
                for update in updates:
                    message = update.get('message', {})
                    reply = reply_for(message)
                    if reply:
                        try:
                            await api.call('sendMessage', chat_id=message['chat']['id'], text=reply)
                        except TelegramError as exc:
                            if exc.code not in (400, 403):
                                raise
                    offset = update['update_id'] + 1
            except TelegramError as exc:
                if exc.code in (401, 409):
                    print(f'Polling stopped: {exc.code}. Check token or another running instance.')
                    return 1
                print('Telegram temporarily unavailable; retrying without logging payloads.')
                await asyncio.sleep(5)


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--check', action='store_true', help='Read-only identity/webhook check; no messages or updates consumed')
    args = parser.parse_args()
    try:
        raise SystemExit(asyncio.run(run(args.check)))
    except TelegramError as exc:
        print(str(exc))
        raise SystemExit(1)
    except KeyboardInterrupt:
        print('Bot stopped.')
