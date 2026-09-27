import asyncio
import io
import json
import os
import re
import sqlite3
import time
import uuid
from contextlib import asynccontextmanager, contextmanager
from datetime import date, timedelta
from pathlib import Path

import httpx
import yaml
from dotenv import load_dotenv
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles
from pypdf import PdfReader

ROOT = Path(__file__).parent
load_dotenv(ROOT / '.env')
CFG = yaml.safe_load((ROOT / 'config.yaml').read_text(encoding='utf-8'))
PROMPT = (ROOT / 'prompts/analyze.txt').read_text(encoding='utf-8')
DB_PATH = os.getenv('DATABASE_PATH', str(ROOT / 'deadline_buddy.sqlite3'))
MESSAGES = {
    'EMPTY_TEXT': 'Вставьте текст задания или загрузите PDF.',
    'TEXT_TOO_SHORT': 'Слишком коротко. Нужно условие задания целиком.',
    'TEXT_TOO_LONG': 'Текст длиннее 20 000 символов. Сократите условие.',
    'FILE_TOO_LARGE': 'Файл больше 5 МБ.',
    'FILE_NOT_PDF': 'Нужен файл PDF.',
    'PDF_NO_TEXT': 'В PDF нет текста. Вставьте условие вручную.',
    'DEADLINE_INVALID': 'Дата сдачи некорректна или уже прошла.',
    'LLM_TIMEOUT': 'Разбор занял слишком много времени. Попробуйте ещё раз.',
    'LLM_BUSY': 'Сервис ИИ перегружен. Подождите минуту.',
    'LLM_BAD_RESPONSE': 'Не удалось разобрать задание. Попробуйте ещё раз.',
    'SESSION_NOT_FOUND': 'Сессия истекла. Разберите задание заново.',
    'STEP_NOT_FOUND': 'Шаг не найден. Обновите страницу.',
    'BAD_REQUEST': 'Проверьте данные запроса.',
    'SERVER_ERROR': 'Ошибка сервера. Попробуйте позже.',
    'LLM_NOT_CONFIGURED': 'Для разбора нужно настроить ключ сервиса ИИ.',
}


class ApiError(Exception):
    def __init__(self, code, status=400):
        self.code, self.status = code, status


@contextmanager
def db():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute('PRAGMA foreign_keys=ON')
    try:
        with conn:
            yield conn
    finally:
        conn.close()


def initialize():
    with db() as c:
        c.executescript('''
        CREATE TABLE IF NOT EXISTS sessions(id TEXT PRIMARY KEY, created_at REAL NOT NULL, expires_at REAL NOT NULL);
        CREATE TABLE IF NOT EXISTS plans(id INTEGER PRIMARY KEY, session_id TEXT UNIQUE NOT NULL REFERENCES sessions(id) ON DELETE CASCADE, goal TEXT NOT NULL, deadline_date TEXT);
        CREATE TABLE IF NOT EXISTS requirements(id INTEGER PRIMARY KEY, plan_id INTEGER NOT NULL REFERENCES plans(id) ON DELETE CASCADE, kind TEXT NOT NULL, text TEXT NOT NULL, sort_order INTEGER NOT NULL);
        CREATE TABLE IF NOT EXISTS steps(id INTEGER PRIMARY KEY AUTOINCREMENT, plan_id INTEGER NOT NULL REFERENCES plans(id) ON DELETE CASCADE, title TEXT NOT NULL, sort_order INTEGER NOT NULL, done INTEGER NOT NULL DEFAULT 0, due_date TEXT);
        ''')
        c.execute('DELETE FROM sessions WHERE expires_at <= ?', (time.time(),))


async def cleanup():
    while True:
        with db() as c:
            c.execute('DELETE FROM sessions WHERE expires_at <= ?', (time.time(),))
        await asyncio.sleep(60)


@asynccontextmanager
async def lifespan(app):
    initialize()
    app.state.active = 0
    task = asyncio.create_task(cleanup())
    yield
    task.cancel()
    try:
        await task
    except asyncio.CancelledError:
        pass


app = FastAPI(title='Deadline Buddy', lifespan=lifespan)


@app.exception_handler(ApiError)
async def api_error(request, exc):
    return JSONResponse({'code': exc.code, 'message': MESSAGES[exc.code]}, status_code=exc.status)


@app.exception_handler(Exception)
async def server_error(request, exc):
    return JSONResponse({'code': 'SERVER_ERROR', 'message': MESSAGES['SERVER_ERROR']}, status_code=500)


@app.middleware('http')
async def guard(request, call_next):
    if request.method in ('POST', 'PATCH'):
        origin = request.headers.get('origin')
        if origin and origin != str(request.base_url).rstrip('/'):
            return JSONResponse({'code': 'BAD_REQUEST', 'message': MESSAGES['BAD_REQUEST']}, status_code=400)
    response = await call_next(request)
    response.headers['Cache-Control'] = 'no-store'
    return response


def deadline(value):
    if value is None or value == '':
        return None
    try:
        if not isinstance(value, str) or not re.fullmatch(r'\d{4}-\d{2}-\d{2}', value):
            raise ValueError()
        result = date.fromisoformat(value)
        if result < date.today():
            raise ValueError()
        return result
    except ValueError:
        raise ApiError('DEADLINE_INVALID')


def normalize(text):
    if not isinstance(text, str):
        raise ApiError('BAD_REQUEST')
    text = re.sub(r'\s+', ' ', text).strip()
    if not text:
        raise ApiError('EMPTY_TEXT')
    if len(text) > CFG['max_text_length']:
        raise ApiError('TEXT_TOO_LONG')
    if len(text) < CFG['min_text_length']:
        raise ApiError('TEXT_TOO_SHORT')
    return text


def pdf_text(content):
    if not content.startswith(b'%PDF-'):
        raise ApiError('FILE_NOT_PDF')
    try:
        reader = PdfReader(io.BytesIO(content))
        if reader.is_encrypted:
            raise ApiError('PDF_NO_TEXT')
        parts = []
        for page in reader.pages:
            parts.append(page.extract_text() or '')
            if sum(map(len, parts)) > CFG['max_text_length']:
                raise ApiError('TEXT_TOO_LONG')
        result = '\n'.join(parts)
        if not result.strip():
            raise ApiError('PDF_NO_TEXT')
        return result
    except ApiError:
        raise
    except Exception:
        raise ApiError('PDF_NO_TEXT')


def validate_model(data):
    if not isinstance(data, dict) or not isinstance(data.get('goal'), str) or not data['goal'].strip():
        raise ValueError()
    for key in ('requirements', 'constraints', 'steps'):
        if not isinstance(data.get(key), list) or any(not isinstance(s, str) or not s.strip() for s in data[key]):
            raise ValueError()
    if not 3 <= len(data['steps']) <= 12:
        raise ValueError()
    return data


async def analyze_model(text):
    if os.getenv('DEMO_MODE', 'false').lower() == 'true':
        return {'goal': 'Демонстрационный план выполнения учебного задания', 'requirements': ['Уточнить требования по исходному условию'], 'constraints': [], 'steps': ['Прочитать условие и выписать результаты работы', 'Собрать материалы и источники', 'Составить структуру решения', 'Выполнить основную часть задания', 'Проверить требования и подготовить работу к сдаче']}
    key = os.getenv('LLM_API_KEY')
    if not key:
        raise ApiError('LLM_NOT_CONFIGURED', 500)
    async with httpx.AsyncClient(timeout=CFG['llm_timeout']) as client:
        for attempt in range(2):
            try:
                response = await client.post(os.getenv('LLM_BASE_URL', 'https://api.openai.com/v1').rstrip('/') + '/chat/completions', headers={'Authorization': f'Bearer {key}'}, json={'model': os.getenv('LLM_MODEL', 'gpt-4.1-mini'), 'messages': [{'role': 'system', 'content': PROMPT}, {'role': 'user', 'content': text}], 'temperature': CFG['temperature'], 'max_tokens': CFG['max_tokens'], 'response_format': {'type': 'json_object'}})
                if response.status_code == 429:
                    raise ApiError('LLM_BUSY', 429)
                if response.is_error:
                    raise ApiError('LLM_BAD_RESPONSE', 500)
                return validate_model(json.loads(response.json()['choices'][0]['message']['content']))
            except httpx.TimeoutException:
                raise ApiError('LLM_TIMEOUT', 504)
            except httpx.RequestError:
                raise ApiError('LLM_BAD_RESPONSE', 500)
            except (ValueError, KeyError, IndexError, TypeError):
                if attempt == 1:
                    raise ApiError('LLM_BAD_RESPONSE', 500)


def schedule(c, plan_id, end):
    steps = c.execute('SELECT id FROM steps WHERE plan_id=? ORDER BY sort_order', (plan_id,)).fetchall()
    start = date.today()
    span = (end - start).days
    for i, step in enumerate(steps):
        offset = (i * span // (len(steps) - 1)) if len(steps) > 1 else span
        c.execute('UPDATE steps SET due_date=? WHERE id=?', ((start + timedelta(days=offset)).isoformat(), step['id']))
    c.execute('UPDATE plans SET deadline_date=? WHERE id=?', (end.isoformat(), plan_id))


def session(c, sid):
    row = c.execute('SELECT * FROM sessions WHERE id=? AND expires_at>?', (sid, time.time())).fetchone()
    if not row:
        raise ApiError('SESSION_NOT_FOUND', 404)
    return row


def get_plan(c, sid):
    session(c, sid)
    plan = c.execute('SELECT * FROM plans WHERE session_id=?', (sid,)).fetchone()
    if not plan:
        raise ApiError('SESSION_NOT_FOUND', 404)
    reqs = c.execute('SELECT * FROM requirements WHERE plan_id=? ORDER BY sort_order', (plan['id'],)).fetchall()
    steps = [dict(r) for r in c.execute('SELECT id,title,due_date,done FROM steps WHERE plan_id=? ORDER BY sort_order', (plan['id'],))]
    for step in steps:
        step['done'] = bool(step['done'])
    today = date.today().isoformat()
    return {'session_id': sid, 'goal': plan['goal'], 'deadline_date': plan['deadline_date'], 'requirements': [r['text'] for r in reqs if r['kind'] == 'requirement'], 'constraints': [r['text'] for r in reqs if r['kind'] == 'constraint'], 'steps': steps, 'today': {'date': today, 'steps': [{k: s[k] for k in ('id', 'title', 'done')} for s in steps if s['due_date'] == today]}}


async def body_json(request):
    try:
        raw = await limited_body(request, 150000)
        data = json.loads(raw)
        if not isinstance(data, dict):
            raise ValueError()
        return data
    except (ValueError, UnicodeError):
        raise ApiError('BAD_REQUEST')


async def limited_body(request, limit):
    result = bytearray()
    async for chunk in request.stream():
        result.extend(chunk)
        if len(result) > limit:
            raise ApiError('FILE_TOO_LARGE')
    return bytes(result)


@app.post('/api/analyze')
async def analyze(request: Request):
    if app.state.active >= CFG['max_concurrent']:
        raise ApiError('LLM_BUSY', 429)
    app.state.active += 1
    try:
        async with asyncio.timeout(CFG['analysis_timeout']):
            if request.headers.get('content-type', '').startswith('multipart/form-data'):
                request._body = await limited_body(request, CFG['max_file_bytes'] + 150000)
                async with request.form(max_files=1, max_fields=3) as form:
                    text = form.get('text', '')
                    end = deadline(form.get('deadline_date'))
                    if not text:
                        file = form.get('file')
                        if not file:
                            raise ApiError('EMPTY_TEXT')
                        if not getattr(file, 'filename', '').lower().endswith('.pdf'):
                            raise ApiError('FILE_NOT_PDF')
                        content = await file.read(CFG['max_file_bytes'] + 1)
                        if len(content) > CFG['max_file_bytes']:
                            raise ApiError('FILE_TOO_LARGE')
                        await file.close()
                        text = await asyncio.to_thread(pdf_text, content)
                        del content
            else:
                data = await body_json(request)
                text, end = data.get('text', ''), deadline(data.get('deadline_date'))
            text = normalize(text)
            parsed = await analyze_model(text)
            del text
            sid = request.cookies.get('session_id')
            with db() as c:
                existing = c.execute('SELECT id FROM sessions WHERE id=? AND expires_at>?', (sid, time.time())).fetchone()
                if not existing:
                    sid = str(uuid.uuid4())
                    c.execute('INSERT INTO sessions VALUES(?,?,?)', (sid, time.time(), time.time() + 14 * 86400))
                c.execute('DELETE FROM plans WHERE session_id=?', (sid,))
                pid = c.execute('INSERT INTO plans(session_id,goal) VALUES(?,?)', (sid, parsed['goal'])).lastrowid
                for key, kind in [('requirements', 'requirement'), ('constraints', 'constraint')]:
                    c.executemany('INSERT INTO requirements(plan_id,kind,text,sort_order) VALUES(?,?,?,?)', [(pid, kind, value, i) for i, value in enumerate(parsed[key], 1)])
                c.executemany('INSERT INTO steps(plan_id,title,sort_order) VALUES(?,?,?)', [(pid, title, i) for i, title in enumerate(parsed['steps'], 1)])
                if end:
                    schedule(c, pid, end)
                result = get_plan(c, sid)
                expires = session(c, sid)['expires_at']
            response = JSONResponse(result)
            response.set_cookie('session_id', sid, max_age=max(0, int(expires-time.time())), httponly=True, secure=os.getenv('COOKIE_SECURE', 'false').lower() == 'true', samesite='lax')
            return response
    except TimeoutError:
        raise ApiError('LLM_TIMEOUT', 504)
    finally:
        app.state.active -= 1


@app.get('/api/plan')
async def restore(request: Request):
    with db() as c:
        return get_plan(c, request.cookies.get('session_id'))


@app.get('/api/config')
async def public_config():
    return {'demo_mode': os.getenv('DEMO_MODE', 'false').lower() == 'true'}


@app.post('/api/plan/deadline')
async def change_deadline(request: Request):
    data = await body_json(request)
    end = deadline(data.get('deadline_date'))
    if end is None:
        raise ApiError('DEADLINE_INVALID')
    sid = data.get('session_id')
    with db() as c:
        session(c, sid)
        plan = c.execute('SELECT id FROM plans WHERE session_id=?', (sid,)).fetchone()
        if not plan:
            raise ApiError('SESSION_NOT_FOUND', 404)
        schedule(c, plan['id'], end)
        return get_plan(c, sid)


@app.patch('/api/steps/{step_id}')
async def toggle(step_id: int, request: Request):
    data = await body_json(request)
    if type(data.get('done')) is not bool:
        raise ApiError('BAD_REQUEST')
    with db() as c:
        found = c.execute('SELECT s.id FROM steps s JOIN plans p ON p.id=s.plan_id JOIN sessions x ON x.id=p.session_id WHERE s.id=? AND x.id=? AND x.expires_at>?', (step_id, data.get('session_id'), time.time())).fetchone()
        if not found:
            raise ApiError('STEP_NOT_FOUND', 404)
        c.execute('UPDATE steps SET done=? WHERE id=?', (data['done'], step_id))
    return {'id': step_id, 'done': data['done']}


DIST = ROOT.parent / 'frontend' / 'dist'
if DIST.exists():
    app.mount('/', StaticFiles(directory=DIST, html=True), name='frontend')
