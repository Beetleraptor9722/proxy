import sys,os

if os.system(f"{sys.executable} -m pip install aiohttp") != 0:
    raise Exception('Ошибка установки библиотек')