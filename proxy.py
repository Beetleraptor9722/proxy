# proxy.py
# pip install fastapi uvicorn httpx

from fastapi import FastAPI, Request, HTTPException
from fastapi.responses import StreamingResponse, JSONResponse
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
    "content-length",  # не пробрасываем content-length назад при стриминге
}

@app.api_route("/{path:path}", methods=["GET", "POST", "PUT", "PATCH", "DELETE", "OPTIONS", "HEAD"])
async def proxy(path: str, request: Request):
    # 1) Защита: проксируем только OpenAI API пути (избегаем проброса "/" и других корневых запросов)
    # Если хотите универсальный прокси — уберите этот check, но тогда ожидайте 421 если Host не скорректирован.
    if not path.startswith("v1/"):
        # Например, GET / -> 404 или 405, HEAD -> 405
        if request.method == "HEAD":
            return JSONResponse({"error": "misdirected_or_not_allowed"}, status_code=405)
        return JSONResponse({"error": "only /v1/* paths are proxied"}, status_code=404)

    body = await request.body()

    # 2) Берём все входящие заголовки, но принудительно ставим корректный Host для OpenAI
    upstream_headers = dict(request.headers)
    upstream_headers["host"] = "api.openai.com"

    # (опционально) Можно удалить Accept-Encoding, чтобы не усложнять декодирование:
    upstream_headers.pop("accept-encoding", None)

    url = f"https://api.openai.com/{path}"

    async with httpx.AsyncClient(timeout=None) as client:
        try:
            async with client.stream(
                request.method,
                url,
                headers=upstream_headers,
                content=body if body else None
            ) as upstream:

                # Подготовка заголовков ответа: удаляем hop-by-hop и content-length
                response_headers = {
                    k: v
                    for k, v in upstream.headers.items()
                    if k.lower() not in HOP_BY_HOP
                }

                # Генератор для стриминга — ловим StreamClosed и завершаем корректно
                async def gen():
                    try:
                        async for chunk in upstream.aiter_bytes():
                            if chunk:
                                yield chunk
                    except httpx.StreamClosed:
                        # upstream закрыл поток — аккуратно завершаем генератор
                        log.info("upstream stream closed early (likely upstream error or client disconnected)")
                        return
                    except Exception as exc:
                        log.exception("Ошибка при чтении upstream stream: %s", exc)
                        return
                    finally:
                        # гарантированно закрываем upstream
                        await upstream.aclose()

                return StreamingResponse(
                    gen(),
                    status_code=upstream.status_code,
                    headers=response_headers
                )

        except httpx.RequestError as e:
            log.exception("Ошибка соединения с OpenAI: %s", e)
            raise HTTPException(status_code=502, detail="upstream_connection_error")
