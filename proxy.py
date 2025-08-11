# proxy.py
# pip install fastapi uvicorn httpx

from fastapi import FastAPI, Request, HTTPException
from fastapi.responses import StreamingResponse
import httpx
import logging

log = logging.getLogger("proxy")
app = FastAPI()

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

@app.api_route("/{path:path}", methods=["GET","POST","PUT","PATCH","DELETE","OPTIONS","HEAD"])
async def proxy(path: str, request: Request):
    """
    Проксирует запросы к https://api.openai.com/{path}
    Пробрасывает тело ответа (стримит) напрямую клиенту.
    """
    # Читаем тело запроса (может быть пустым)
    body = await request.body()

    # Копируем все входящие заголовки и фиксируем Host для OpenAI
    upstream_request_headers = dict(request.headers)
    upstream_request_headers["host"] = "api.openai.com"
    # (опционально) убрать accept-encoding, чтобы получить разжатый ответ
    upstream_request_headers.pop("accept-encoding", None)

    url = f"https://api.openai.com/{path}"

    try:
        async with httpx.AsyncClient(timeout=None) as client:
            # stream через контекст-менеджер — корректный способ с современной httpx
            async with client.stream(
                request.method,
                url,
                headers=upstream_request_headers,
                content=body if body else None,
            ) as upstream:

                # Подготовим заголовки для ответа — удаляем hop-by-hop, content-length и transfer-encoding
                response_headers = {
                    k: v for k, v in upstream.headers.items()
                    if k.lower() not in HOP_BY_HOP
                }

                # Генератор, который отдаёт байты upstream напрямую клиенту
                async def stream_generator():
                    try:
                        async for chunk in upstream.aiter_raw():
                            if chunk:
                                yield chunk
                    except Exception as exc:
                        # upstream закрылся или другая ошибка — просто завершиться
                        log.debug("Upstream stream error: %s", exc)
                        return
                    finally:
                        # всегда закрываем upstream
                        await upstream.aclose()

                return StreamingResponse(
                    stream_generator(),
                    status_code=upstream.status_code,
                    headers=response_headers
                )

    except httpx.RequestError as e:
        log.exception("Ошибка соединения с upstream: %s", e)
        raise HTTPException(status_code=502, detail="upstream_connection_error")
