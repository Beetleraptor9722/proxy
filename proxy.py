from fastapi import FastAPI, Request, HTTPException
from fastapi.responses import StreamingResponse, Response, JSONResponse
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

# Порог для "малого" ответа — читаем целиком (1MB)
SMALL_RESPONSE_THRESHOLD = 1 * 1024 * 1024

@app.api_route("/{path:path}", methods=["GET", "POST", "PUT", "PATCH", "DELETE", "OPTIONS", "HEAD"])
async def proxy(path: str, request: Request):
    # Проксируем только OpenAI-пути (или убери этот check если нужен универсальный)
    if not path.startswith("v1/"):
        return JSONResponse({"error": "only /v1/* paths are proxied"}, status_code=404)

    body = await request.body()
    upstream_request_headers = dict(request.headers)
    # Принудительно корректный Host для OpenAI
    upstream_request_headers["host"] = "api.openai.com"
    upstream_request_headers.pop("accept-encoding", None)  # просим незжатый ответ

    url = f"https://api.openai.com/{path}"

    async with httpx.AsyncClient(timeout=None) as client:
        try:
            async with client.stream(
                request.method,
                url,
                headers=upstream_request_headers,
                content=body if body else None,
            ) as upstream:

                # Лог upstream статуса и некоторых заголовков (для отладки)
                log.info("upstream status=%s", upstream.status_code)
                log.debug("upstream headers=%s", dict(upstream.headers))

                # Подготовим заголовки для ответа (удаляем hop-by-hop и content-length)
                response_headers = {
                    k: v for k, v in upstream.headers.items() if k.lower() not in HOP_BY_HOP
                }

                # Если upstream указал content-length и он небольшой -> прочитаем целиком и вернём Response
                clen = upstream.headers.get("Content-Length") or upstream.headers.get("content-length")
                try:
                    clen_val = int(clen) if clen is not None else None
                except Exception:
                    clen_val = None

                if clen_val is not None and clen_val <= SMALL_RESPONSE_THRESHOLD:
                    # безопасно прочитать весь ответ
                    content_bytes = await upstream.aread()
                    # если content-type отсутствует — добавим дефолт
                    if "content-type" not in {k.lower() for k in response_headers}:
                        response_headers["content-type"] = "application/octet-stream"
                    return Response(content=content_bytes, status_code=upstream.status_code, headers=response_headers)

                # Если нет явного Content-Length — попробуем прочитать небольшую часть non-blocking:
                # Но в общем случае — стримим
                async def gen():
                    try:
                        async for chunk in upstream.aiter_bytes():
                            # yield даже пустые чанки не нужны — но пропустим None
                            if chunk is None:
                                continue
                            yield chunk
                    except httpx.StreamClosed:
                        # upstream закрыл поток — логируем и заканчиваем генератор
                        log.info("upstream stream closed early")
                        return
                    except Exception as exc:
                        log.exception("Ошибка при чтении upstream stream: %s", exc)
                        return
                    finally:
                        await upstream.aclose()

                return StreamingResponse(gen(), status_code=upstream.status_code, headers=response_headers)

        except httpx.RequestError as e:
            log.exception("Ошибка соединения с OpenAI: %s", e)
            raise HTTPException(status_code=502, detail="upstream_connection_error")
