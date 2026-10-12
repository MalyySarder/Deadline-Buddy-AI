"""A real AI health check. Prints no credentials or user data."""
import asyncio
import sys
import time
from services import ServiceError, analyze_model, CFG

SAMPLES = [
    'Подготовить материал для студенческой газеты о работе библиотеки университета. Взять интервью у библиотекаря, сравнить электронные и бумажные источники, написать текст объёмом 4000 знаков. Добавить заголовок, лид и ссылки на источники. Проверить цитаты перед сдачей.',
    'Подготовить план урока по окружающему миру для второго класса на тему смены времён года. Урок длится 40 минут. Указать цель и ожидаемые результаты, придумать два упражнения для работы в парах и короткое задание для проверки понимания. Подготовить список необходимых материалов для учителя.',
    'Разработать учебную программу для учёта книг в библиотеке на Python. Программа должна добавлять книги, искать их по названию и отмечать выдачу читателю. Данные сохранять в файле JSON. Написать инструкцию запуска и проверить три типовых сценария. Подготовить краткий отчёт о структуре программы.'
]

async def check(all_samples=False):
    for index,text in enumerate(SAMPLES if all_samples else SAMPLES[:1],1):
        start=time.monotonic()
        try:
            async with asyncio.timeout(CFG['analysis_timeout']):
                p=await analyze_model(text)
            print(f'OK sample={index} steps={len(p["steps"])} seconds={time.monotonic()-start:.1f}',flush=True)
        except (ServiceError,TimeoutError) as exc:
            print('FAILED '+(exc.code if isinstance(exc,ServiceError) else 'LLM_TIMEOUT'),flush=True)
            return 1
    return 0

if __name__=='__main__':
    raise SystemExit(asyncio.run(check('--all' in sys.argv)))
