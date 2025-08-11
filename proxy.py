# proxy.py
# pip install fastapi uvicorn httpx

from fastapi import FastAPI, Request, HTTPException
from fastapi.responses import StreamingResponse, JSONResponse
import httpx
import logging

logging.basicConfig(level=logging.INFO)
log = logging.getLogger("proxy")

# Заголовки, которые не следует пробрасывать в ответ клиенту
HOP_BY_HOP = {
    "connection",
    "keep-alive",
    "proxy-authenticate",
    "proxy-authorization",
    "te",
    "trailers",
    "transfer-encoding",
    "upgrade",
    "content-length",
}

app = FastAPI()

@app.api_route("/{path:path}", methods=["GET","POST","PUT","PATCH","DELETE","OPTIONS","HEAD"])
async def proxy(path: str, request: Request):
    """
    Проксирует любой запрос к https://api.openai.com/{path}
    - Пробрасывает все входящие заголовки вверх, но фиксирует Host для OpenAI.
    - Стримит тело ответа напрямую клиенту.
    """
    # читаем тело запроса (может быть пустым)
    body = await request.body()

    # Собираем заголовки, которые отправим к OpenAI
    upstream_request_headers = dict(request.headers)
    # критично: выставляем корректный Host для OpenAI (иначе 421)
    upstream_request_headers["host"] = "api.openai.com"
    # проще получить "вменяемый" ответ — убираем accept-encoding (httpx сам разожмёт если нужно)
    upstream_request_headers.pop("accept-encoding", None)

    url = f"https://api.openai.com/{path}"

    try:
        async with httpx.AsyncClient(timeout=None) as client:
            # корректный способ стриминга в новой версии httpx
            async with client.stream(
                request.method,
                url,
                headers=upstream_request_headers,
                content=body if body else None,
            ) as upstream:

                log.info("Upstream status: %s %s", upstream.status_code, url)

                # Формируем заголовки ответа клиенту, но удаляем hop-by-hop и content-length
                response_headers = {
                    k: v for k, v in upstream.headers.items()
                    if k.lower() not in HOP_BY_HOP
                }

                # Для HEAD — возвращаем только заголовки (без тела)
                if request.method.upper() == "HEAD":
                    return JSONResponse({}, status_code=upstream.status_code, headers=response_headers)

                # Генератор: напрямую стримим сырые байты upstream
                async def stream_generator():
                    try:
                        async for chunk in upstream.aiter_raw():
                            if chunk:
                                yield chunk
                    except Exception as exc:
                        # upstream мог закрыться — логируем и завершаем стрим без выброса ошибки наружу
                        log.debug("Upstream stream error (will terminate): %s", exc)
                        return
                    finally:
                        await upstream.aclose()

                # Возвращаем StreamingResponse — uvicorn сам выберет chunked transfer
                return StreamingResponse(
                    stream_generator(),
                    status_code=upstream.status_code,
                    headers=response_headers
                )

    except httpx.RequestError as e:
        log.exception("Ошибка соединения с OpenAI: %s", e)
        raise HTTPException(status_code=502, detail="upstream_connection_error")
