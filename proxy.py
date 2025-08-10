# proxy.py
# pip install fastapi uvicorn httpx
from fastapi import FastAPI, Request, HTTPException
from fastapi.responses import StreamingResponse
import httpx

app = FastAPI()

@app.api_route("/proxy/{path:path}", methods=["GET","POST","PUT","PATCH","DELETE","OPTIONS","HEAD"])
async def proxy(path: str, request: Request):
    """
    Проксирует запрос на https://api.openai.com/{path}
    Пробрасывает все заголовки и тело без проверок.
    """
    # Целевой URL (с query, если есть)
    qs = f"?{request.url.query}" if request.url.query else ""
    url = f"https://api.openai.com/{path}{qs}"

    # Тело запроса
    body = await request.body()

    # Копируем все заголовки
    upstream_headers = dict(request.headers)
    upstream_headers["host"] = "api.openai.com"  # фиксим Host

    try:
        async with httpx.AsyncClient(timeout=None) as client:
            async with client.stream(
                method=request.method,
                url=url,
                headers=upstream_headers,
                content=body if body else None
            ) as upstream:
                # Потоковая передача ответа клиенту
                async def iter_upstream():
                    async for chunk in upstream.aiter_bytes():
                        yield chunk

                return StreamingResponse(
                    iter_upstream(),
                    status_code=upstream.status_code,
                    headers=dict(upstream.headers)
                )

    except httpx.RequestError as e:
        raise HTTPException(status_code=502, detail=str(e))
