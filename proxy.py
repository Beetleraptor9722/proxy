from fastapi import FastAPI, Request
from fastapi.responses import StreamingResponse
import httpx
import logging

app = FastAPI()
log = logging.getLogger("proxy")

HOP_BY_HOP = {
    "connection",
    "keep-alive",
    "proxy-authenticate",
    "proxy-authorization",
    "te",
    "trailers",
    "transfer-encoding",
    "upgrade",
    # убираем content-length, т.к. при стриминге его нельзя надёжно пробрасывать
    "content-length",
}

@app.api_route("/{path:path}", methods=["GET", "POST", "PUT", "PATCH", "DELETE", "OPTIONS", "HEAD"])
async def proxy(path: str, request: Request):
    body = await request.body()
    # все входящие заголовки пробрасываем вверх к OpenAI
    upstream_request_headers = dict(request.headers)
    # важно: корректный Host для OpenAI (если хотите, можно не менять)
    upstream_request_headers["host"] = "api.openai.com"

    url = f"https://api.openai.com/{path}"

    async with httpx.AsyncClient(timeout=None) as client:
        try:
            async with client.stream(
                request.method,
                url,
                headers=upstream_request_headers,
                content=body if body else None
            ) as upstream:

                # подготовка заголовков ответа: копируем, но удаляем hop-by-hop + content-length
                response_headers = {
                    k: v
                    for k, v in upstream.headers.items()
                    if k.lower() not in HOP_BY_HOP
                }

                # генератор для стриминга. закрываем upstream в finally
                async def gen():
                    try:
                        async for chunk in upstream.aiter_bytes():
                            # chunk может быть пустым в редких случаях — пропускаем
                            if chunk:
                                yield chunk
                    except Exception as exc:
                        # логируем, но не пробрасываем исключение в клиент (короткий и безопасный fail)
                        log.exception("Ошибка при чтении upstream stream: %s", exc)
                    finally:
                        await upstream.aclose()

                return StreamingResponse(
                    gen(),
                    status_code=upstream.status_code,
                    headers=response_headers
                )

        except httpx.RequestError as e:
            log.exception("Ошибка при соединении с upstream: %s", e)
            # отдаём простой 502 с текстом
            return StreamingResponse(iter([b'{"error":"upstream_connection_error"}']), status_code=502, headers={"content-type":"application/json"})
