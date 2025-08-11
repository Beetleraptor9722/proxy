# proxy.py
# pip install fastapi uvicorn httpx

from fastapi import FastAPI, Request, HTTPException
from fastapi.responses import Response, JSONResponse
import httpx
import logging

logging.basicConfig(level=logging.INFO)
log = logging.getLogger("proxy")

# заголовки hop-by-hop и те, которые не нужно пробрасывать назад
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
    # (опционально) разрешать только v1/* — можно убрать, если нужен универсал
    # if not path.startswith("v1/"):
    #     return JSONResponse({"error":"only /v1/* proxied"}, status_code=404)

    # прочитать тело запроса
    body = await request.body()

    # собрать заголовки для запроса к OpenAI, но корректно выставить Host
    upstream_headers = dict(request.headers)
    upstream_headers["host"] = "api.openai.com"
    # убрать accept-encoding чтобы избежать сложностей с сжатием (httpx обычно декодирует, но на всякий)
    upstream_headers.pop("accept-encoding", None)

    url = f"https://api.openai.com/{path}"

    try:
        async with httpx.AsyncClient(timeout=120.0) as client:
            # Без stream — просто ждём ответа и читаем тело целиком
            resp = await client.request(
                method=request.method,
                url=url,
                headers=upstream_headers,
                content=body if body else None,
            )

            log.info("Upstream %s %s -> %s", request.method, url, resp.status_code)
            log.debug("Upstream headers: %s", dict(resp.headers))

            # Формируем заголовки ответа клиенту: копируем, но удаляем hop-by-hop и content-length
            response_headers = {
                k: v for k, v in resp.headers.items() if k.lower() not in HOP_BY_HOP
            }

            # Если это HEAD — возвращаем только заголовки (без тела)
            if request.method.upper() == "HEAD":
                return Response(content=b"", status_code=resp.status_code, headers=response_headers)

            # Возвращаем тело как есть; httpx уже декодировал gzip/deflate обычно
            return Response(content=resp.content, status_code=resp.status_code, headers=response_headers)

    except httpx.RequestError as e:
        log.exception("Upstream connection error: %s", e)
        raise HTTPException(status_code=502, detail="upstream_connection_error")
