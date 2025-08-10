# proxy.py
# pip install fastapi uvicorn httpx
from fastapi import FastAPI, Request, HTTPException
from fastapi.responses import StreamingResponse
import httpx

app = FastAPI()

@app.api_route("/proxy/{path:path}", methods=["GET","POST","PUT","PATCH","DELETE","OPTIONS","HEAD"])
async def proxy(path: str, request: Request):
    """
    Проксирует любой запрос на https://api.openai.com/{path}
    Пробрасывает ВСЕ входящие заголовки (без валидации), кроме:
      - принудительно заменяет Host на api.openai.com (для корректности запроса)
    Возвращает статус, заголовки и тело от OpenAI напрямую клиенту.
    """
    # Собираем целевой URL (включая query string)
    qs = ""
    if request.url.query:
        qs = "?" + request.url.query

    url = f"https://api.openai.com/{path}{qs}"

    # Берём тело запроса (если есть)
    body = await request.body()

    # Копируем все входящие заголовки
    upstream_headers = dict(request.headers)

    # Принудительно корректируем Host для upstream
    upstream_headers["host"] = "api.openai.com"

    # Выполняем запрос к OpenAI (stream=True для проксирования chunked/stream ответов)
    async with httpx.AsyncClient(timeout=None) as client:
        try:
            upstream = await client.request(
                request.method,
                url,
                headers=upstream_headers,
                content=body if body else None,
                stream=True
            )
        except httpx.RequestError as e:
            # Возвращаем 502 при ошибке связи с upstream
            raise HTTPException(status_code=502, detail=str(e))

        # Стримим байты назад клиенту
        async def iter_upstream():
            try:
                async for chunk in upstream.aiter_bytes():
                    yield chunk
            finally:
                await upstream.aclose()

        # Копируем все заголовки от upstream без фильтрации
        response_headers = dict(upstream.headers)

        # Возвращаем StreamingResponse с upstream статусом и заголовками
        return StreamingResponse(iter_upstream(), status_code=upstream.status_code, headers=response_headers)       except Exception as e:
            raise HTTPException(status_code=502, detail=str(e))

    # Скопируем заголовки, исключая hop-by-hop
    resp_headers = {k:v for k,v in upstream.headers.items() if k.lower() not in ("connection","keep-alive","transfer-encoding","content-encoding")}
    return Response(content=upstream.content, status_code=upstream.status_code, headers=resp_headers)
