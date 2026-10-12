"""Deadline Buddy Telegram interface, aiogram 3 long polling."""
import argparse
import asyncio
import io
import logging
import os
import time
from contextlib import suppress
from aiogram import Bot, Dispatcher, F
from aiogram.exceptions import TelegramAPIError, TelegramBadRequest
from aiogram.types import BotCommand, InlineKeyboardButton, InlineKeyboardMarkup
from services import Analyzer, CFG, ServiceError, Store, pdf_text, split_deadline, probe_model

START = ('Привет! Я Deadline Buddy — помогу понять учебное задание и составить план.\n\n'
         'Пришлите условие текстом (200–20 000 символов) или PDF до 5 МБ с текстовым слоем. '
         'Я выделю цель, требования и шаги — выполнять работу будете вы.\n\n'
         'Можно добавить отдельной строкой: Дедлайн: ГГГГ-ММ-ДД. '
         'Для длинного текста используйте /new, отправьте несколько сообщений и нажмите «Разобрать».\n\n'
         '/plan — весь план, /today — сегодня, /deadline — дата сдачи, /help — помощь.\n'
         'Условие передаётся сервису ИИ. Не присылайте пароли и личные данные. /privacy — хранение данных.')
HELP = ('1. Пришлите полное условие текстом или PDF.\n2. Дождитесь разбора (до 30 секунд).\n'
        '3. Отмечайте выполненные шаги кнопками ☐ / ☑.\n4. Укажите срок через /deadline и смотрите /today.\n\n'
        '/new — собрать длинное условие из нескольких сообщений; /analyze — разобрать собранный текст.\n'
        '/cancel — отменить ввод даты или сбор текста.\n/plan — открыть сохранённый план. '
        'Новое задание заменит его только после успешного разбора.\n/delete — удалить сохранённый план. '
        'Часовой пояс: Москва.\nФото, голос и сканы PDF не поддерживаются. Отдельных напоминаний нет.')
PRIVACY = ('В SQLite сохраняются ваш Telegram ID, план, отметки и дата сдачи. '
           'Через 14 дней без активности они удаляются. /delete удаляет их сразу.\n'
           'Исходное условие и PDF в базу и логи не записываются. PDF обрабатывается в памяти. '
           'Черновик /new хранится в памяти до разбора, отмены или 15 минут без ввода.\n'
           'Текст передаётся внешнему сервису ИИ; его правила хранения определяет провайдер. '
           'Сообщения остаются в вашем чате Telegram, пока вы сами их не удалите.')


def ai_keyboard():
    return InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text='Проверить ИИ', callback_data='ai')]])


def navigation():
    return [[InlineKeyboardButton(text='Сегодня', callback_data='today'), InlineKeyboardButton(text='Весь план', callback_data='plan')],
            [InlineKeyboardButton(text='Указать дату сдачи', callback_data='deadline')],
            [InlineKeyboardButton(text='Разобрать заново', callback_data='new')],
            [InlineKeyboardButton(text='Проверить ИИ', callback_data='ai')]]


def keyboard(plan):
    rows = []
    for n, s in enumerate(plan['steps'], 1):
        label = f"{'☑' if s['done'] else '☐'} {n}. {s['title']}"
        if len(label) > 60:
            label = label[:57] + '…'
        rows.append([InlineKeyboardButton(text=label, callback_data=f"step:{s['id']}")])
    return InlineKeyboardMarkup(inline_keyboard=rows + navigation())


def today_text(p):
    if not p['deadline_date']:
        return 'Сегодня\nУкажите дату сдачи, чтобы увидеть план на сегодня.'
    lines = [f"Сегодня · {p['today']['date']}"]
    for s in p['today']['steps']:
        lines.append(f"{'☑' if s['done'] else '☐'} {s['title']}")
    if not p['today']['steps']:
        lines.append('На сегодня шагов нет. Весь список — /plan.')
    overdue = sum(not s['done'] and bool(s['due_date']) and s['due_date'] < p['today']['date'] for s in p['steps'])
    if overdue:
        lines.append(f'Есть незавершённые шаги за прошлые дни: {overdue}. Посмотрите /plan или измените /deadline.')
    return '\n'.join(lines)


def chunks(text, limit=1750):
    # Safe even when every character occupies two UTF-16 units in Telegram.
    while len(text) > limit:
        cut = text.rfind('\n', 0, limit)
        if cut < limit // 2:
            cut = limit
        yield text[:cut]
        text = text[cut:].lstrip('\n')
    if text:
        yield text


def plan_texts(p):
    yield from chunks('Цель\n' + p['goal'])
    yield from chunks('Требования\n' + ('\n'.join('• '+v for v in p['requirements']) or 'В условии не указаны.') +
                      '\n\nОграничения\n' + ('\n'.join('• '+v for v in p['constraints']) or 'В условии не указаны.'))
    yield from chunks(today_text(p))
    lines = ['Шаги работы' + (f" · до {p['deadline_date']}" if p['deadline_date'] else '')]
    for n,s in enumerate(p['steps'],1):
        due = f" — {s['due_date']}" if s['due_date'] else ''
        lines.append(f"{n}. {s['title']}{due}")
    yield from chunks('\n'.join(lines))


def progress(p):
    return f"Чек-лист · выполнено {sum(s['done'] for s in p['steps'])} из {len(p['steps'])}\nНажмите на шаг, чтобы изменить отметку. Полные названия — выше."


class LimitedBuffer(io.BytesIO):
    def write(self, data):
        if self.tell() + len(data) > CFG['max_file_bytes']:
            raise ServiceError('FILE_TOO_LARGE')
        return super().write(data)


class Conversation:
    def __init__(self, store, analyzer=None):
        self.store = store
        self.analyzer = analyzer or Analyzer(store)
        self.waiting = {}
        self.drafts = {}
        self.probe_active = False
        self.probe_after = 0

    async def check_ai(self, message):
        if self.probe_active:
            await message.answer('Проверка ИИ уже идёт. Дождитесь результата (до 20 секунд).')
            return
        if time.monotonic() < self.probe_after:
            await message.answer('Повторить проверку можно через несколько секунд.', reply_markup=ai_keyboard())
            return
        self.probe_active = True
        try:
            await message.answer('Проверяю ИИ коротким запросом… До 20 секунд.')
            result = await probe_model()
            await message.answer(result, reply_markup=ai_keyboard())
        finally:
            self.probe_active = False
            self.probe_after = time.monotonic() + 10

    def cleanup(self):
        cutoff = time.time()-900
        self.waiting = {u:t for u,t in self.waiting.items() if t>cutoff}
        self.drafts = {u:d for u,d in self.drafts.items() if d[1]>cutoff}
        self.store.cleanup()

    async def show(self, message, plan, only_today=False):
        texts = chunks(today_text(plan)) if only_today else plan_texts(plan)
        for text in texts:
            await message.answer(text)
        await message.answer(progress(plan), reply_markup=keyboard(plan))

    async def prepare(self, message):
        text, end = split_deadline(message.text or message.caption or '')
        if text:
            return text, end
        document = message.document
        if not document:
            raise ServiceError('EMPTY_TEXT')
        if not (document.file_name or '').lower().endswith('.pdf'):
            raise ServiceError('FILE_NOT_PDF')
        if (document.file_size or 0) > CFG['max_file_bytes']:
            raise ServiceError('FILE_TOO_LARGE')
        with LimitedBuffer() as buf:
            await message.bot.download(document, destination=buf, timeout=20)
            content = buf.getvalue()
        text = await asyncio.to_thread(pdf_text, content)
        del content
        return text, end

    async def analyze(self, message, prepare=None):
        uid = message.from_user.id
        if uid in self.analyzer.active:
            raise ServiceError('USER_BUSY')
        status = await message.answer('Разбираем задание…', reply_markup=ai_keyboard())
        try:
            p = await self.analyzer.analyze(uid, prepare or (lambda: self.prepare(message)))
        except ServiceError as exc:
            try:
                await status.edit_text(str(exc), reply_markup=ai_keyboard())
            except TelegramAPIError:
                await message.answer(str(exc), reply_markup=ai_keyboard())
            return
        with suppress(TelegramAPIError):
            await status.edit_text('Разбор готов.')
        self.waiting.pop(uid, None)
        await self.show(message, p)

    async def message(self, message):
        if message.chat.type != 'private' or not message.from_user:
            return
        uid = message.from_user.id
        self.cleanup()
        self.store.touch(uid)
        text = (message.text or '').strip()
        command = text.split(maxsplit=1)[0].split('@')[0] if text.startswith('/') else ''
        try:
            if command in ('/start','/help','/privacy'):
                await message.answer({'/start':START, '/help':HELP, '/privacy':PRIVACY}[command], reply_markup=ai_keyboard())
            elif command == '/ai':
                await self.check_ai(message)
            elif command in ('/plan','/today'):
                await self.show(message,self.store.plan(uid), command=='/today')
            elif uid in self.analyzer.active:
                raise ServiceError('USER_BUSY')
            elif command == '/delete':
                self.store.forget(uid)
                self.waiting.pop(uid,None)
                self.drafts.pop(uid,None)
                await message.answer('Ваш план удалён. Можно прислать новое задание.')
            elif command == '/cancel':
                self.waiting.pop(uid,None)
                self.drafts.pop(uid,None)
                await message.answer('Ввод отменён. Сохранённый план — /plan.')
            elif command == '/new':
                self.waiting.pop(uid,None)
                self.drafts[uid] = ('',time.time())
                await message.answer('Присылайте условие частями, всего до 20 000 символов. Когда закончите, нажмите «Разобрать» или /analyze. Для обычной отправки PDF сначала /cancel.',reply_markup=InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text='Разобрать',callback_data='analyze')]]))
            elif command == '/analyze':
                await self.analyze_draft(message, uid)
            elif command == '/deadline':
                self.store.plan(uid)
                parts = text.split(maxsplit=1)
                if len(parts)>1:
                    await self.show(message,self.store.set_deadline(uid,parts[1].strip()))
                    self.waiting.pop(uid,None)
                else:
                    self.drafts.pop(uid,None)
                    self.waiting[uid]=time.time()
                    await message.answer('Введите дату сдачи в формате ГГГГ-ММ-ДД. Например: 2026-12-20. /cancel — отмена.')
            elif command:
                await message.answer('Не знаю эту команду. Доступные действия — /help.')
            elif uid in self.waiting:
                if message.document or len(text)>30:
                    self.waiting.pop(uid,None)
                    await self.analyze(message)
                else:
                    await self.show(message,self.store.set_deadline(uid,text))
                    self.waiting.pop(uid,None)
            elif uid in self.drafts:
                if not text:
                    await message.answer('В этом режиме отправляйте текст. Для PDF нажмите /cancel и отправьте документ.')
                else:
                    draft = (self.drafts[uid][0]+'\n'+text).strip()
                    if len(draft)>CFG['max_text_length']:
                        raise ServiceError('TEXT_TOO_LONG')
                    self.drafts[uid]=(draft,time.time())
                    await message.answer(f'Собрано символов: {len(draft)}. Ещё часть или /analyze.',reply_markup=InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text='Разобрать',callback_data='analyze')]]))
            else:
                await self.analyze(message)
        except ServiceError as exc:
            await message.answer(str(exc))

    async def analyze_draft(self, message, uid):
        draft = self.drafts.pop(uid, ('',0))[0]
        async def prepare():
            return split_deadline(draft)
        await self.analyze(message, prepare)

    async def callback(self, callback):
        if not callback.message or callback.message.chat.type != 'private':
            await callback.answer()
            return
        uid = callback.from_user.id
        self.cleanup()
        self.store.touch(uid)
        await callback.answer()
        message = callback.message
        action = callback.data or ''
        try:
            if action == 'ai':
                await self.check_ai(message)
            elif action in ('plan','today'):
                await self.show(message,self.store.plan(uid),action=='today')
            elif uid in self.analyzer.active:
                raise ServiceError('USER_BUSY')
            elif action.startswith('step:'):
                try:
                    sid=int(action[5:])
                except ValueError:
                    raise ServiceError('STEP_NOT_FOUND') from None
                self.store.toggle(uid,sid)
                p=self.store.plan(uid)
                try:
                    await message.edit_text(progress(p),reply_markup=keyboard(p))
                except TelegramBadRequest:
                    await message.answer(progress(p),reply_markup=keyboard(p))
            elif action=='deadline':
                self.store.plan(uid)
                self.drafts.pop(uid,None)
                self.waiting[uid]=time.time()
                await message.answer('Введите дату сдачи: ГГГГ-ММ-ДД. /cancel — отмена.')
            elif action=='new':
                self.waiting.pop(uid,None)
                self.drafts.pop(uid,None)
                await message.answer('Пришлите новое условие текстом или PDF. Старый план заменится после успешного разбора. Для нескольких сообщений — /new.')
            elif action=='analyze':
                proxy=message.model_copy(update={'from_user':callback.from_user})
                proxy.as_(message.bot)
                await self.analyze_draft(proxy,uid)
        except ServiceError as exc:
            await message.answer(str(exc))


async def run(check=False):
    token = os.getenv('BOT_TOKEN') or os.getenv('TELEGRAM_BOT_TOKEN','')
    expected = os.getenv('TELEGRAM_BOT_USERNAME','Deadlinebud_bot').lstrip('@')
    if not token:
        print('BOT_TOKEN is missing in backend/.env',flush=True)
        return 1
    async with Bot(token) as bot:
        me=await bot.get_me()
        if me.username.lower()!=expected.lower():
            print('Bot identity mismatch',flush=True)
            return 1
        info=await bot.get_webhook_info()
        print(f'Bot verified: @{me.username}; webhook={bool(info.url)}',flush=True)
        if check:
            return 0
        if info.url:
            print('Existing webhook preserved; polling not started',flush=True)
            return 1
        conversation=Conversation(Store())
        dp=Dispatcher()
        dp.message.register(conversation.message,F.chat.type=='private')
        dp.callback_query.register(conversation.callback)

        @dp.errors()
        async def safe_error(event):
            print('Handler error: '+type(event.exception).__name__,flush=True)
            msg=event.update.message or (event.update.callback_query.message if event.update.callback_query else None)
            if msg:
                with suppress(TelegramAPIError):
                    await msg.answer('Не удалось выполнить действие. Попробуйте ещё раз или откройте /plan.')
            return True

        await bot.set_my_commands([BotCommand(command=c,description=d) for c,d in [
            ('start','Как начать'),('plan','Весь план и чек-лист'),('today','Задачи на сегодня'),
            ('deadline','Указать дату сдачи'),('new','Собрать длинное условие'),('analyze','Разобрать собранный текст'),
            ('cancel','Отменить ввод'),('help','Помощь'),('ai','Проверить связь с ИИ'),('privacy','Хранение данных'),('delete','Удалить мой план')]])
        async def cleanup():
            while True:
                conversation.cleanup()
                await asyncio.sleep(60)
        task=asyncio.create_task(cleanup())
        print('Polling started; real LLM enabled',flush=True)
        try:
            await dp.start_polling(bot,allowed_updates=['message','callback_query'])
        finally:
            task.cancel()
            with suppress(asyncio.CancelledError):
                await task


if __name__=='__main__':
    parser=argparse.ArgumentParser()
    parser.add_argument('--check',action='store_true')
    args=parser.parse_args()
    logging.disable(logging.CRITICAL)
    try:
        raise SystemExit(asyncio.run(run(args.check)))
    except KeyboardInterrupt:
        print('Bot stopped')
    except Exception as exc:
        print('Startup failed: '+type(exc).__name__+'; check network and local .env',flush=True)
        raise SystemExit(1)
