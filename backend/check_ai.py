"""A real AI health check. Prints no credentials or user data."""
import asyncio
import sys
import time
from services import ServiceError, analyze_model, CFG, normalize

SAMPLES = [
    'Подготовить материал для студенческой газеты о работе библиотеки университета. Взять интервью у библиотекаря, сравнить электронные и бумажные источники, написать текст объёмом 4000 знаков. Добавить заголовок, лид и ссылки на источники. Проверить цитаты перед сдачей.',
    'Подготовить план урока по окружающему миру для второго класса на тему смены времён года. Урок длится 40 минут. Указать цель и ожидаемые результаты, придумать два упражнения для работы в парах и короткое задание для проверки понимания. Подготовить список необходимых материалов для учителя.',
    'Разработать учебную программу для учёта книг в библиотеке на Python. Программа должна добавлять книги, искать их по названию и отмечать выдачу читателю. Данные сохранять в файле JSON. Написать инструкцию запуска и проверить три типовых сценария. Подготовить краткий отчёт о структуре программы.'
]

def long_sample(sections):
    text = 'Подготовить учебный проект: исследование городской среды и доступности библиотеки. Нужно представить отчёт, презентацию и приложения.\n'
    for i in range(1, sections+1):
        text += f'Раздел {i}. Опишите отдельный аспект работы библиотеки: доступность помещения, расписание, электронные каталоги, обслуживание читателей, навигацию и правила выдачи. Приведите наблюдения, сравните с требованиями задания, укажите источники и ограничения. Для каждого вывода приведите конкретный пример; персональные данные участников не публикуйте. Результат включите в отчёт, сгруппировав сходные наблюдения.\n'
    return text


async def check(all_samples=False, long=False):
    samples = SAMPLES if all_samples else SAMPLES[:1]
    if long:
        samples = [SAMPLES[0], long_sample(25), long_sample(48)]
    for index,text in enumerate(samples,1):
        start=time.monotonic()
        try:
            async with asyncio.timeout(CFG['analysis_timeout']):
                text=normalize(text)
                p=await analyze_model(text)
            print(f'OK sample={index} chars={len(text)} steps={len(p["steps"])} seconds={time.monotonic()-start:.1f}',flush=True)
        except (ServiceError,TimeoutError) as exc:
            print('FAILED '+(exc.code if isinstance(exc,ServiceError) else 'LLM_TIMEOUT'),flush=True)
            return 1
    return 0

if __name__=='__main__':
    raise SystemExit(asyncio.run(check('--all' in sys.argv, '--long' in sys.argv)))
