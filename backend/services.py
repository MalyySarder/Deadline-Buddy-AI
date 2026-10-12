"""Telegram-independent validation, LLM and persistent plan services."""
import asyncio
import io
import json
import logging
import os
import re
import sqlite3
import time
from contextlib import contextmanager
from datetime import date, datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import httpx
import yaml
from dotenv import load_dotenv
from pypdf import PdfReader

ROOT = Path(__file__).parent
load_dotenv(ROOT / '.env')
CFG = yaml.safe_load((ROOT / 'config.yaml').read_text(encoding='utf-8'))
PROMPT = (ROOT / 'prompts/analyze.txt').read_text(encoding='utf-8')
TTL = 14 * 86400
for name in ('httpx', 'httpcore', 'pypdf'):
    logging.getLogger(name).disabled = True

MESSAGES = {
    'EMPTY_TEXT': 'Пришлите текст задания или файл PDF.',
    'TEXT_TOO_SHORT': 'Слишком коротко. Нужно условие задания целиком (от 200 символов).',
    'TEXT_TOO_LONG': 'Текст длиннее 20 000 символов. Сократите условие.',
    'FILE_TOO_LARGE': 'Файл больше 5 МБ.',
    'FILE_NOT_PDF': 'Нужен файл PDF.',
    'PDF_NO_TEXT': 'В PDF нет текста. Пришлите условие вручную.',
    'DEADLINE_INVALID': 'Дата сдачи некорректна или уже прошла. Нужен формат ГГГГ-ММ-ДД.',
    'LLM_TIMEOUT': 'Разбор занял слишком много времени. Попробуйте ещё раз.',
    'LLM_BUSY': 'Сервис ИИ перегружен. Подождите минуту.',
    'LLM_BAD_RESPONSE': 'Не удалось разобрать задание. Попробуйте ещё раз.',
    'SESSION_NOT_FOUND': 'Сессия истекла или плана пока нет. Пришлите задание заново.',
    'STEP_NOT_FOUND': 'Шаг не найден. Откройте план командой /plan.',
    'USER_BUSY': 'Уже разбираю ваше задание. Дождитесь результата.',
    'LLM_NOT_CONFIGURED': 'Сервис ИИ пока не настроен. Сообщите владельцу бота.',
}


class ServiceError(Exception):
    def __init__(self, code):
        self.code = code
        super().__init__(MESSAGES[code])


def today():
    return datetime.now(ZoneInfo(os.getenv('BOT_TIMEZONE', 'Europe/Moscow'))).date()


def parse_deadline(value):
    try:
        if not isinstance(value, str) or not re.fullmatch(r'\d{4}-\d{2}-\d{2}', value):
            raise ValueError
        result = date.fromisoformat(value)
        if result < today():
            raise ValueError
        return result
    except ValueError:
        raise ServiceError('DEADLINE_INVALID') from None


def split_deadline(text):
    """Only an explicit standalone date line is metadata; dates in prose stay intact."""
    lines, end = [], None
    for line in (text or '').splitlines():
        match = re.fullmatch(r'\s*(?:(?:дедлайн|дата сдачи)\s*:\s*)?(\d{4}-\d{2}-\d{2})\s*', line, re.I)
        if match:
            end = parse_deadline(match[1])
        else:
            lines.append(line)
    return '\n'.join(lines).strip(), end


def normalize(text):
    text = re.sub(r'\s+', ' ', text or '').strip()
    if not text:
        raise ServiceError('EMPTY_TEXT')
    if len(text) > CFG['max_text_length']:
        raise ServiceError('TEXT_TOO_LONG')
    if len(text) < CFG['min_text_length']:
        raise ServiceError('TEXT_TOO_SHORT')
    return text


def pdf_text(content):
    if len(content) > CFG['max_file_bytes']:
        raise ServiceError('FILE_TOO_LARGE')
    if not content.startswith(b'%PDF-'):
        raise ServiceError('FILE_NOT_PDF')
    try:
        reader = PdfReader(io.BytesIO(content))
        if reader.is_encrypted:
            raise ServiceError('PDF_NO_TEXT')
        parts, size = [], 0
        for page in reader.pages:
            part = page.extract_text() or ''
            size += len(part)
            if size > CFG['max_text_length']:
                raise ServiceError('TEXT_TOO_LONG')
            parts.append(part)
        text = '\n'.join(parts)
        if not text.strip():
            raise ServiceError('PDF_NO_TEXT')
        return text
    except ServiceError:
        raise
    except Exception:
        raise ServiceError('PDF_NO_TEXT') from None


def validate_model(data):
    if not isinstance(data, dict) or not isinstance(data.get('goal'), str) or not data['goal'].strip():
        raise ValueError('goal')
    if len(data['goal']) > 1000:
        raise ValueError('goal length')
    for key in ('requirements', 'constraints', 'steps'):
        values = data.get(key)
        if not isinstance(values, list) or len(values) > 30:
            raise ValueError(key)
        if any(not isinstance(v, str) or not v.strip() or len(v) > 1000 for v in values):
            raise ValueError(key)
    if not 3 <= len(data['steps']) <= 12:
        raise ValueError('steps')
    return {k: data[k] for k in ('goal', 'requirements', 'constraints', 'steps')}


async def analyze_model(text):
    key, base, model = (os.getenv(k, '').strip() for k in ('LLM_API_KEY', 'LLM_BASE_URL', 'LLM_MODEL'))
    if not all((key, base, model)):
        raise ServiceError('LLM_NOT_CONFIGURED')
    body = {'model': model, 'messages': [{'role': 'system', 'content': PROMPT}, {'role': 'user', 'content': text}],
            'temperature': CFG['temperature'], 'max_tokens': CFG['max_tokens'], 'stream': False,
            'response_format': {'type': 'json_object'}}
    async with httpx.AsyncClient(timeout=CFG['llm_timeout'], follow_redirects=False) as client:
        for attempt in range(2):
            try:
                r = await client.post(base.rstrip('/') + '/chat/completions', headers={'Authorization': 'Bearer ' + key}, json=body)
                if r.status_code == 429:
                    raise ServiceError('LLM_BUSY')
                if r.status_code != 200:
                    raise ServiceError('LLM_BAD_RESPONSE')
                content = r.json()['choices'][0]['message']['content']
                return validate_model(json.loads(content))
            except httpx.TimeoutException:
                raise ServiceError('LLM_TIMEOUT') from None
            except httpx.RequestError:
                raise ServiceError('LLM_BAD_RESPONSE') from None
            except (ValueError, KeyError, IndexError, TypeError):
                if attempt:
                    raise ServiceError('LLM_BAD_RESPONSE') from None


class Store:
    def __init__(self, path=None):
        self.path = Path(path or os.getenv('BOT_DATABASE_PATH', 'telegram.sqlite3'))
        if not self.path.is_absolute():
            self.path = ROOT / self.path
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.db() as c:
            c.executescript('''
            PRAGMA journal_mode=WAL;
            CREATE TABLE IF NOT EXISTS sessions(telegram_user_id INTEGER PRIMARY KEY, created_at REAL NOT NULL, last_seen_at REAL NOT NULL, expires_at REAL NOT NULL);
            CREATE TABLE IF NOT EXISTS plans(id INTEGER PRIMARY KEY AUTOINCREMENT, telegram_user_id INTEGER UNIQUE NOT NULL REFERENCES sessions(telegram_user_id) ON DELETE CASCADE, goal TEXT NOT NULL, deadline_date TEXT);
            CREATE TABLE IF NOT EXISTS requirements(id INTEGER PRIMARY KEY, plan_id INTEGER NOT NULL REFERENCES plans(id) ON DELETE CASCADE, kind TEXT NOT NULL, text TEXT NOT NULL, sort_order INTEGER NOT NULL);
            CREATE TABLE IF NOT EXISTS steps(id INTEGER PRIMARY KEY AUTOINCREMENT, plan_id INTEGER NOT NULL REFERENCES plans(id) ON DELETE CASCADE, title TEXT NOT NULL, sort_order INTEGER NOT NULL, done INTEGER NOT NULL DEFAULT 0, due_date TEXT);
            ''')
        self.cleanup()

    @contextmanager
    def db(self):
        c = sqlite3.connect(self.path, timeout=5)
        c.row_factory = sqlite3.Row
        c.execute('PRAGMA foreign_keys=ON')
        try:
            with c:
                yield c
        finally:
            c.close()

    def cleanup(self):
        with self.db() as c:
            c.execute('DELETE FROM sessions WHERE expires_at<=?', (time.time(),))

    def touch(self, uid):
        self.cleanup()
        with self.db() as c:
            c.execute('UPDATE sessions SET last_seen_at=?,expires_at=? WHERE telegram_user_id=?', (time.time(), time.time()+TTL, uid))

    def _session(self, c, uid):
        if not c.execute('SELECT 1 FROM sessions WHERE telegram_user_id=? AND expires_at>?', (uid, time.time())).fetchone():
            raise ServiceError('SESSION_NOT_FOUND')
        c.execute('UPDATE sessions SET last_seen_at=?,expires_at=? WHERE telegram_user_id=?', (time.time(), time.time()+TTL, uid))

    def _plan(self, c, uid):
        self._session(c, uid)
        p = c.execute('SELECT * FROM plans WHERE telegram_user_id=?', (uid,)).fetchone()
        if not p:
            raise ServiceError('SESSION_NOT_FOUND')
        reqs = c.execute('SELECT kind,text FROM requirements WHERE plan_id=? ORDER BY sort_order', (p['id'],)).fetchall()
        steps = [dict(s) for s in c.execute('SELECT id,title,done,due_date FROM steps WHERE plan_id=? ORDER BY sort_order', (p['id'],))]
        for s in steps:
            s['done'] = bool(s['done'])
        return {'telegram_user_id': uid, 'goal': p['goal'], 'deadline_date': p['deadline_date'],
                'requirements': [r['text'] for r in reqs if r['kind']=='requirement'],
                'constraints': [r['text'] for r in reqs if r['kind']=='constraint'], 'steps': steps,
                'today': {'date': today().isoformat(), 'steps': [s for s in steps if s['due_date']==today().isoformat()]}}

    def plan(self, uid):
        with self.db() as c:
            return self._plan(c, uid)

    def replace(self, uid, parsed, end=None):
        with self.db() as c:
            now = time.time()
            c.execute('DELETE FROM sessions WHERE telegram_user_id=? AND expires_at<=?', (uid, now))
            c.execute('INSERT INTO sessions VALUES(?,?,?,?) ON CONFLICT(telegram_user_id) DO UPDATE SET last_seen_at=excluded.last_seen_at,expires_at=excluded.expires_at', (uid, now, now, now+TTL))
            c.execute('DELETE FROM plans WHERE telegram_user_id=?', (uid,))
            pid = c.execute('INSERT INTO plans(telegram_user_id,goal) VALUES(?,?)', (uid, parsed['goal'])).lastrowid
            for key, kind in [('requirements','requirement'), ('constraints','constraint')]:
                c.executemany('INSERT INTO requirements(plan_id,kind,text,sort_order) VALUES(?,?,?,?)', [(pid,kind,v,i) for i,v in enumerate(parsed[key])])
            c.executemany('INSERT INTO steps(plan_id,title,sort_order) VALUES(?,?,?)', [(pid,v,i) for i,v in enumerate(parsed['steps'])])
            if end:
                self._schedule(c, pid, end)
            return self._plan(c, uid)

    def _schedule(self, c, pid, end):
        steps = c.execute('SELECT id FROM steps WHERE plan_id=? ORDER BY sort_order', (pid,)).fetchall()
        start = today()
        span = (end-start).days
        for i, s in enumerate(steps):
            due = start + timedelta(days=i*span//max(len(steps)-1, 1))
            c.execute('UPDATE steps SET due_date=? WHERE id=?', (due.isoformat(),s['id']))
        c.execute('UPDATE plans SET deadline_date=? WHERE id=?', (end.isoformat(),pid))

    def set_deadline(self, uid, value):
        end = parse_deadline(value)
        with self.db() as c:
            self._session(c, uid)
            p = c.execute('SELECT id FROM plans WHERE telegram_user_id=?', (uid,)).fetchone()
            if not p:
                raise ServiceError('SESSION_NOT_FOUND')
            self._schedule(c,p['id'],end)
            return self._plan(c,uid)

    def toggle(self, uid, step_id):
        with self.db() as c:
            self._session(c,uid)
            s = c.execute('SELECT s.id,s.done FROM steps s JOIN plans p ON p.id=s.plan_id WHERE p.telegram_user_id=? AND s.id=?', (uid,step_id)).fetchone()
            if not s:
                raise ServiceError('STEP_NOT_FOUND')
            done = not s['done']
            c.execute('UPDATE steps SET done=? WHERE id=?', (done,step_id))
            return {'id': step_id, 'done': done}

    def forget(self, uid):
        with self.db() as c:
            c.execute('DELETE FROM sessions WHERE telegram_user_id=?', (uid,))


class Analyzer:
    def __init__(self, store, model=analyze_model):
        self.store, self.model = store, model
        self.active = set()

    async def analyze(self, uid, prepare):
        if uid in self.active:
            raise ServiceError('USER_BUSY')
        if len(self.active) >= CFG['max_concurrent']:
            raise ServiceError('LLM_BUSY')
        self.active.add(uid)
        try:
            async with asyncio.timeout(CFG['analysis_timeout']):
                text, end = await prepare()
                text = normalize(text)
                parsed = await self.model(text)
                del text
                return self.store.replace(uid, parsed, end)
        except TimeoutError:
            raise ServiceError('LLM_TIMEOUT') from None
        finally:
            self.active.discard(uid)
