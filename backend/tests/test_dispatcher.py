"""Exercise real aiogram routing and Telegram payload serialization offline."""
import asyncio
from datetime import datetime, timezone

from aiogram import Bot, Dispatcher, F
from aiogram.client.session.base import BaseSession
from aiogram.types import Message, Update

from services import Analyzer, Store, today
from telegram_bot import Conversation
from test_telegram import TEXT, VALID


class RecordingSession(BaseSession):
    def __init__(self):
        super().__init__()
        self.calls=[]

    async def close(self):
        pass

    async def stream_content(self,*args,**kwargs):
        yield b''

    async def make_request(self,bot,method,timeout=None):
        payload=method.model_dump(exclude_none=True)
        self.calls.append((method.__api_method__,payload))
        if method.__api_method__ in ('sendMessage','editMessageText'):
            return Message(message_id=len(self.calls),date=datetime.now(timezone.utc),chat={'id':payload['chat_id'],'type':'private'},text=payload['text'],from_user={'id':123456,'is_bot':True,'first_name':'Deadline Buddy'}).as_(bot)
        return True


def test_dispatcher_full_flow(tmp_path):
    async def run():
        session=RecordingSession()
        bot=Bot('123456:offline-test-token',session=session)
        store=Store(tmp_path/'flow.sqlite3')
        async def model(text):return VALID
        c=Conversation(store,Analyzer(store,model))
        dp=Dispatcher()
        dp.message.register(c.message,F.chat.type=='private')
        dp.callback_query.register(c.callback)
        seq=0
        async def send(text):
            nonlocal seq
            seq+=1
            update=Update.model_validate({'update_id':seq,'message':{'message_id':seq,'date':1,'chat':{'id':99,'type':'private'},'from':{'id':99,'is_bot':False,'first_name':'Tester'},'text':text}})
            await dp.feed_update(bot,update)
        async def click(data):
            nonlocal seq
            seq+=1
            update=Update.model_validate({'update_id':seq,'callback_query':{'id':str(seq),'from':{'id':99,'is_bot':False,'first_name':'Tester'},'chat_instance':'test','data':data,'message':{'message_id':42,'date':1,'chat':{'id':99,'type':'private'},'from':{'id':123456,'is_bot':True,'first_name':'Bot'},'text':'Buttons'}}})
            await dp.feed_update(bot,update)
        await send('/start')
        await send(TEXT)
        await click('step:'+str(store.plan(99)['steps'][0]['id']))
        assert store.plan(99)['steps'][0]['done']
        await click('deadline')
        await send(today().isoformat())
        await click('today')
        await click('new')
        await send('/new')
        await send(TEXT[:150]);await send(TEXT[150:])
        await click('analyze')
        assert store.plan(99) and not store.plan(99)['steps'][0]['done']
        with store.db() as db:
            assert db.execute('SELECT count(*) FROM sessions').fetchone()[0]==1
        assert any(name=='editMessageText' for name,_ in session.calls)
        assert all(len(payload.get('text','').encode('utf-16-le'))//2<=4096 for _,payload in session.calls)
        await send('/delete')
        await bot.session.close()
    asyncio.run(run())
