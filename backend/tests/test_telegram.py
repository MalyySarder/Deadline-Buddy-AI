import asyncio
import io
import json
import time
from datetime import timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock

import httpx
import pytest
from pypdf import PdfWriter
import services as s
from telegram_bot import Conversation, LimitedBuffer, chunks, keyboard, plan_texts

TEXT = 'Подготовить материал для студенческой газеты о работе библиотеки университета. Взять интервью у библиотекаря, сравнить электронные и бумажные источники, написать текст объёмом 4000 знаков. Добавить заголовок, лид и ссылки на источники. Проверить цитаты перед сдачей.'
VALID = {'goal':'Подготовить материал', 'requirements':['4000 знаков','Заголовок и лид'], 'constraints':[], 'steps':['Изучить тему','Взять интервью','Написать черновик','Проверить цитаты','Сдать материал']}

@pytest.fixture
def store(tmp_path):
    return s.Store(tmp_path/'bot.sqlite3')

@pytest.mark.parametrize('text,code',[('', 'EMPTY_TEXT'),('ок','TEXT_TOO_SHORT'),('x'*20001,'TEXT_TOO_LONG')])
def test_text_limits(text,code):
    with pytest.raises(s.ServiceError) as e:s.normalize(text)
    assert e.value.code==code

@pytest.mark.parametrize('value',['2020-01-01','01.12.2030','2030-02-30','2030-2-01',''])
def test_dates(value):
    with pytest.raises(s.ServiceError):s.parse_deadline(value)


def test_date_metadata():
    end=(s.today()+timedelta(days=3)).isoformat()
    assert s.split_deadline(TEXT+'\nДедлайн: '+end)==(TEXT,s.parse_deadline(end))
    assert s.split_deadline('Сравнить события 2020-01-01 и 2020-02-01')[1] is None


def test_lifecycle_and_ownership(store):
    p=store.replace(1,VALID)
    sid=p['steps'][0]['id']
    store.replace(2,VALID)
    with pytest.raises(s.ServiceError) as e:store.toggle(2,sid)
    assert e.value.code=='STEP_NOT_FOUND'
    assert store.toggle(1,sid)['done']
    p=store.set_deadline(1,(s.today()+timedelta(days=3)).isoformat())
    assert p['steps'][0]['done']
    assert p['steps'][0]['due_date']==s.today().isoformat()
    assert p['steps'][-1]['due_date']==p['deadline_date']
    p=store.set_deadline(1,s.today().isoformat())
    assert len(p['today']['steps'])==5
    assert s.Store(store.path).plan(1)==p
    store.replace(1,VALID)
    with pytest.raises(s.ServiceError):store.toggle(1,sid)
    assert not any(x['done'] for x in store.plan(1)['steps'])
    store.forget(1)
    with pytest.raises(s.ServiceError):store.plan(1)
    assert store.plan(2)


def test_sliding_expiry(store):
    store.replace(1,VALID)
    with store.db() as c:c.execute('UPDATE sessions SET expires_at=?',(time.time()+20,))
    store.touch(1)
    with store.db() as c:
        assert c.execute('SELECT expires_at-last_seen_at FROM sessions').fetchone()[0]==pytest.approx(s.TTL,abs=.01)
        c.execute('UPDATE sessions SET expires_at=?',(time.time()-1,))
    store.cleanup()
    with store.db() as c:
        for table in ('sessions','plans','requirements','steps'):
            assert c.execute('SELECT count(*) FROM '+table).fetchone()[0]==0


def test_pdf():
    out=io.BytesIO();w=PdfWriter();w.add_blank_page(width=100,height=100);w.write(out)
    for data,code in [(out.getvalue(),'PDF_NO_TEXT'),(b'not pdf','FILE_NOT_PDF'),(b'x'*(s.CFG['max_file_bytes']+1),'FILE_TOO_LARGE')]:
        with pytest.raises(s.ServiceError) as e:s.pdf_text(data)
        assert e.value.code==code
    with LimitedBuffer() as b:
        with pytest.raises(s.ServiceError):b.write(b'x'*(s.CFG['max_file_bytes']+1))


@pytest.mark.parametrize('mode',['retry','invalid','busy','timeout','network'])
def test_model(monkeypatch,mode):
    monkeypatch.setenv('LLM_API_KEY','test-secret')
    monkeypatch.setenv('LLM_BASE_URL','https://example.test/api')
    monkeypatch.setenv('LLM_MODEL','test')
    calls=[]
    def handle(request):
        calls.append(request)
        if mode=='timeout':raise httpx.ReadTimeout('test-secret')
        if mode=='network':raise httpx.ConnectError('test-secret')
        if mode=='busy':return httpx.Response(429)
        content=json.dumps(VALID) if mode=='retry' and len(calls)==2 else '{}'
        return httpx.Response(200,json={'choices':[{'message':{'content':content}}]})
    original=httpx.AsyncClient
    monkeypatch.setattr(s.httpx,'AsyncClient',lambda **kw:original(transport=httpx.MockTransport(handle),**kw))
    if mode=='retry':
        assert asyncio.run(s.analyze_model(TEXT))==VALID
        assert len(calls)==2
    else:
        with pytest.raises(s.ServiceError) as e:asyncio.run(s.analyze_model(TEXT))
        assert 'test-secret' not in str(e.value)
        assert e.value.code=={'invalid':'LLM_BAD_RESPONSE','busy':'LLM_BUSY','timeout':'LLM_TIMEOUT','network':'LLM_BAD_RESPONSE'}[mode]


def test_concurrency_and_failed_replacement(store,monkeypatch):
    async def run():
        gate=asyncio.Event()
        async def model(text):
            await gate.wait()
            return VALID
        a=s.Analyzer(store,model)
        async def prep():return TEXT,None
        tasks=[asyncio.create_task(a.analyze(uid,prep)) for uid in range(5)]
        await asyncio.sleep(0)
        for uid,code in [(1,'USER_BUSY'),(6,'LLM_BUSY')]:
            with pytest.raises(s.ServiceError) as e:await a.analyze(uid,prep)
            assert e.value.code==code
        gate.set();await asyncio.gather(*tasks)
        old=store.plan(1)
        async def fail(text):raise s.ServiceError('LLM_BAD_RESPONSE')
        a.model=fail
        with pytest.raises(s.ServiceError):await a.analyze(1,prep)
        assert store.plan(1)==old and not a.active
        a.model=model;gate.clear()
        monkeypatch.setitem(s.CFG,'analysis_timeout',.01)
        with pytest.raises(s.ServiceError) as e:await a.analyze(1,prep)
        assert e.value.code=='LLM_TIMEOUT' and not a.active
    asyncio.run(run())


def message(text=None,uid=1,document=None,caption=None):
    return SimpleNamespace(text=text,caption=caption,document=document,chat=SimpleNamespace(type='private'),from_user=SimpleNamespace(id=uid),answer=AsyncMock(),edit_text=AsyncMock(),bot=SimpleNamespace(download=AsyncMock()))


def test_conversation(store):
    async def run():
        c=Conversation(store,s.Analyzer(store,AsyncMock(return_value=VALID)))
        start=message('/start');await c.message(start)
        assert 'Пришлите условие' in start.answer.call_args.args[0]
        m=message(TEXT);await c.message(m)
        assert len(m.answer.call_args_list)>=5
        sid=store.plan(1)['steps'][0]['id']
        cb=SimpleNamespace(message=m,from_user=SimpleNamespace(id=1),data=f'step:{sid}',answer=AsyncMock())
        await c.callback(cb)
        assert store.plan(1)['steps'][0]['done'] and m.edit_text.called
        await c.message(message('/deadline'))
        await c.message(message('bad date'))
        assert 1 in c.waiting
        await c.message(message(s.today().isoformat()))
        assert 1 not in c.waiting and store.plan(1)['deadline_date']
        await c.message(message('/new'))
        await c.message(message(TEXT[:150]));await c.message(message(TEXT[150:]))
        await c.message(message('/analyze'))
        assert 1 not in c.drafts
        await c.message(message('/delete'))
        with pytest.raises(s.ServiceError):store.plan(1)
    asyncio.run(run())


def test_text_wins_over_file(store):
    async def run():
        c=Conversation(store)
        m=message(caption=TEXT,document=SimpleNamespace(file_name='bad.docx',file_size=999999999))
        assert (await c.prepare(m))[0]==TEXT
        m.bot.download.assert_not_called()
        m=message(document=SimpleNamespace(file_name='bad.docx',file_size=4))
        with pytest.raises(s.ServiceError) as e:await c.prepare(m)
        assert e.value.code=='FILE_NOT_PDF'
    asyncio.run(run())


def test_long_render(store):
    p=store.replace(1,{**VALID,'goal':'😀'*1000,'steps':['шаг'*300]*12})
    assert all(len(t.encode('utf-16-le'))//2<=4096 for t in plan_texts(p))
    assert len(keyboard(p).inline_keyboard)==16
    assert ''.join(chunks('😀'*6000))=='😀'*6000


def test_pdf_with_text_and_no_source_storage(store):
    from pypdf.generic import DictionaryObject, NameObject, DecodedStreamObject
    writer=PdfWriter()
    page=writer.add_blank_page(width=600,height=800)
    font=DictionaryObject({NameObject('/Type'):NameObject('/Font'),NameObject('/Subtype'):NameObject('/Type1'),NameObject('/BaseFont'):NameObject('/Helvetica')})
    page[NameObject('/Resources')]=DictionaryObject({NameObject('/Font'):DictionaryObject({NameObject('/F1'):writer._add_object(font)})})
    stream=DecodedStreamObject()
    source='Prepare a report about the university library and compare paper and electronic sources. '*4
    stream.set_data(('BT /F1 12 Tf 10 700 Td ('+source+') Tj ET').encode('ascii'))
    page[NameObject('/Contents')]=writer._add_object(stream)
    buf=io.BytesIO();writer.write(buf)
    assert s.normalize(s.pdf_text(buf.getvalue()))==source.strip()
    async def prepare():return source,None
    asyncio.run(s.Analyzer(store,AsyncMock(return_value=VALID)).analyze(1,prepare))
    with store.db() as c:
        dump='\n'.join(c.iterdump())
    assert source.strip() not in dump


def test_draft_expiry_and_group_ignored(store):
    async def run():
        c=Conversation(store)
        c.drafts[1]=('private draft',time.time()-901)
        c.waiting[1]=time.time()-901
        c.cleanup()
        assert not c.drafts and not c.waiting
        m=message('/start');m.chat.type='group'
        await c.message(m)
        m.answer.assert_not_called()
    asyncio.run(run())

@pytest.mark.parametrize('mode,expected',[
    ('ok','ИИ ответил'),('timeout','не ответил'),('network','Нет соединения'),
    ('auth','ключ отклонён'),('busy','превышен лимит'),('server','HTTP 503'),
    ('bad','некорректные данные'),('empty','пустой ответ')])
def test_ai_probe(monkeypatch,mode,expected):
    monkeypatch.setenv('LLM_API_KEY','test-secret')
    monkeypatch.setenv('LLM_BASE_URL','https://example.test/api')
    monkeypatch.setenv('LLM_MODEL','test-model')
    def handle(request):
        body=json.loads(request.content)
        assert body['messages']==[{'role':'user','content':'Reply with exactly: API works'}]
        assert body['max_tokens']==256 and not body['stream']
        if mode=='timeout':raise httpx.ReadTimeout('test-secret')
        if mode=='network':raise httpx.ConnectError('test-secret')
        if mode in ('auth','busy','server'):return httpx.Response({'auth':401,'busy':429,'server':503}[mode])
        if mode=='bad':return httpx.Response(200,text='<html>test-secret</html>')
        return httpx.Response(200,json={'choices':[{'message':{'content':'' if mode=='empty' else 'API works'}}]})
    original=httpx.AsyncClient
    monkeypatch.setattr(s.httpx,'AsyncClient',lambda **kw:original(transport=httpx.MockTransport(handle),**kw))
    result=asyncio.run(s.probe_model())
    assert expected in result and 'test-secret' not in result


def test_probe_during_analysis_and_cooldown(store,monkeypatch):
    import telegram_bot
    async def run():
        probe=AsyncMock(return_value='ИИ ответил')
        monkeypatch.setattr(telegram_bot,'probe_model',probe)
        c=Conversation(store)
        c.analyzer.active.add(1)
        c.drafts[1]=('draft',time.time())
        c.waiting[1]=time.time()
        m=message('/ai');await c.message(m)
        assert probe.await_count==1 and 1 in c.waiting and c.drafts[1][0]=='draft'
        cb=SimpleNamespace(message=m,from_user=SimpleNamespace(id=1),data='ai',answer=AsyncMock())
        await c.callback(cb)
        assert probe.await_count==1
        c.probe_after=0
        await c.callback(cb)
        assert probe.await_count==2
        c.probe_active=True
        await c.message(m)
        assert probe.await_count==2
    asyncio.run(run())


def test_analysis_error_replaces_waiting_message(store):
    async def run():
        model=AsyncMock(side_effect=s.ServiceError('LLM_TIMEOUT'))
        c=Conversation(store,s.Analyzer(store,model))
        m=message(TEXT)
        status=m.answer.return_value
        await c.message(m)
        assert 'слишком много времени' in status.edit_text.call_args.args[0]
        assert status.edit_text.call_args.kwargs['reply_markup'].inline_keyboard[0][0].callback_data=='ai'
    asyncio.run(run())


def test_separate_ai_transport(monkeypatch):
    from unittest.mock import Mock
    transport=Mock()
    client=Mock()
    monkeypatch.setattr(s.httpx,'AsyncHTTPTransport',transport)
    monkeypatch.setattr(s.httpx,'AsyncClient',client)
    monkeypatch.setenv('HTTPS_PROXY','http://vpn.example:8080')
    monkeypatch.setenv('LLM_LOCAL_ADDRESS','192.0.2.10')
    s.llm_client(18)
    transport.assert_called_once_with(local_address='192.0.2.10',trust_env=False)
    assert client.call_args.kwargs['trust_env'] is False
    assert client.call_args.kwargs['transport'] is transport.return_value
    monkeypatch.delenv('LLM_LOCAL_ADDRESS')
    s.llm_client(25)
    assert 'transport' not in client.call_args.kwargs
    assert client.call_args.kwargs['trust_env'] is False
