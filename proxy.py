from fastapi import FastAPI, Request
from fastapi.responses import StreamingResponse
import httpx

app = FastAPI()

@app.api_route("/{path:path}", methods=["GET", "POST", "PUT", "PATCH", "DELETE"])
async def proxy(path: str, request: Request):
    body = await request.body()
    headers = dict(request.headers)
    url = f"https://api.openai.com/{path}"

    async with httpx.AsyncClient(timeout=None) as client:
        async with client.stream(
            request.method,
            url,
            headers=headers,
            content=body
        ) as upstream:

            async def content_generator():
                try:
                    async for chunk in upstream.aiter_bytes():
                        yield chunk
                except httpx.StreamClosed:
                    # Поток закрыт — безопасно просто завершить генератор
                    return

            return StreamingResponse(
                content_generator(),
                status_code=upstream.status_code,
                headers=dict(upstream.headers)
            )
