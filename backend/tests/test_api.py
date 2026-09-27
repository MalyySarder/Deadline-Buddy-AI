import io
import json
import time
from datetime import date, timedelta

import httpx
import pytest
from fastapi.testclient import TestClient
from pypdf import PdfWriter
import main

TEXT = 'Подготовить учебный проект по анализу данных. Собрать данные, проверить пропуски, построить графики и написать выводы. Предоставить отчёт на 15 страниц и презентацию на 10 слайдов. Указать источники и описать ограничения выбранного метода.'


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setattr(main, 'DB_PATH', str(tmp_path / 'test.sqlite3'))
    monkeypatch.setenv('DEMO_MODE', 'true')
    with TestClient(main.app) as c:
        yield c


@pytest.mark.parametrize('text,code', [('', 'EMPTY_TEXT'), ('ок','TEXT_TOO_SHORT'), ('я'*20001,'TEXT_TOO_LONG')], ids=['empty','short','long'])
def test_text_validation(client, text, code):
    r = client.post('/api/analyze', json={'text': text})
    assert r.status_code == 400
    assert r.json()['code'] == code


@pytest.mark.parametrize('value', ['2020-01-01','27.09.2026','2026-02-30',123])
def test_deadline_validation(client, value):
    assert client.post('/api/analyze', json={'text':TEXT,'deadline_date':value}).json()['code'] == 'DEADLINE_INVALID'


def test_lifecycle(client):
    end = date.today() + timedelta(days=3)
    r = client.post('/api/analyze', json={'text':TEXT, 'deadline_date':end.isoformat()})
    assert r.status_code == 200
    p = r.json()
    assert p['steps'][0]['due_date'] == date.today().isoformat()
    assert p['steps'][-1]['due_date'] == end.isoformat()
    sid, step = p['session_id'], p['steps'][0]['id']
    assert 'HttpOnly' in r.headers['set-cookie']
    assert client.patch(f'/api/steps/{step}',json={'session_id':sid,'done':True}).status_code == 200
    assert client.patch(f'/api/steps/{step}',json={'session_id':'foreign','done':True}).status_code == 404
    p = client.post('/api/plan/deadline',json={'session_id':sid,'deadline_date':date.today().isoformat()}).json()
    assert p['steps'][0]['done'] is True
    assert all(s['due_date']==date.today().isoformat() for s in p['steps'])
    assert client.get('/api/plan').json()==p
    p2 = client.post('/api/analyze',json={'text':TEXT}).json()
    assert p2['session_id']==sid
    assert all(not s['done'] and s['due_date'] is None for s in p2['steps'])
    assert p2['today']['steps']==[]
    assert client.patch(f'/api/steps/{step}',json={'session_id':sid,'done':False}).status_code==404
    with main.db() as c:
        assert c.execute('SELECT count(*) FROM plans').fetchone()[0]==1
        c.execute('UPDATE sessions SET expires_at=?',(time.time()-1,))
    assert client.get('/api/plan').status_code==404
    main.initialize()
    with main.db() as c:
        assert c.execute('SELECT count(*) FROM steps').fetchone()[0]==0


def test_pdf_errors_and_text_precedence(client):
    writer=PdfWriter();writer.add_blank_page(width=100,height=100)
    buf=io.BytesIO();writer.write(buf)
    assert client.post('/api/analyze',files={'file':('blank.pdf',buf.getvalue(),'application/pdf')}).json()['code']=='PDF_NO_TEXT'
    assert client.post('/api/analyze',files={'file':('x.docx',b'abc')}).json()['code']=='FILE_NOT_PDF'
    assert client.post('/api/analyze',files={'file':('x.pdf',b'%PDF-'+b'x'*main.CFG['max_file_bytes'])}).json()['code']=='FILE_TOO_LARGE'
    assert client.post('/api/analyze',data={'text':TEXT},files={'file':('x.docx',b'abc')}).status_code==200


def test_llm_retry(client, monkeypatch):
    monkeypatch.setenv('DEMO_MODE','false');monkeypatch.setenv('LLM_API_KEY','test-key')
    calls=[]
    valid={'goal':'Цель','requirements':[],'constraints':[],'steps':['Первый','Второй','Третий']}
    def handler(request):
        calls.append(request)
        return httpx.Response(200,json={'choices':[{'message':{'content':'broken' if len(calls)==1 else json.dumps(valid)}}]})
    original=httpx.AsyncClient
    monkeypatch.setattr(main.httpx,'AsyncClient',lambda **kwargs:original(transport=httpx.MockTransport(handler),**kwargs))
    assert client.post('/api/analyze',json={'text':TEXT}).status_code==200
    assert len(calls)==2


@pytest.mark.parametrize('mode,code,status',[('bad','LLM_BAD_RESPONSE',500),('busy','LLM_BUSY',429),('timeout','LLM_TIMEOUT',504)])
def test_llm_failures(client, monkeypatch, mode, code, status):
    monkeypatch.setenv('DEMO_MODE','false');monkeypatch.setenv('LLM_API_KEY','test-key')
    def handler(request):
        if mode=='timeout':raise httpx.ReadTimeout('timeout')
        return httpx.Response(429 if mode=='busy' else 200,json={'choices':[{'message':{'content':'{}'}}]})
    original=httpx.AsyncClient
    monkeypatch.setattr(main.httpx,'AsyncClient',lambda **kwargs:original(transport=httpx.MockTransport(handler),**kwargs))
    r=client.post('/api/analyze',json={'text':TEXT})
    assert r.status_code==status and r.json()['code']==code


def test_busy_and_bad_payload(client):
    main.app.state.active=5
    assert client.post('/api/analyze',json={'text':TEXT}).status_code==429
    main.app.state.active=0
    assert client.post('/api/analyze',content='not json').status_code==400
    assert client.patch('/api/steps/1',json={'done':'yes'}).status_code==400


def test_three_assignments(client):
    for text in [TEXT, 'Написать эссе о развитии городов. '*12, 'Создать программу для учёта книг в библиотеке. '*10]:
        r=client.post('/api/analyze',json={'text':text})
        assert r.status_code==200 and len(r.json()['steps'])>=3
