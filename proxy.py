from fastapi import FastAPI, Request
from fastapi.responses import StreamingResponse
import httpx

app = FastAPI()

@app.api_route("/{path:path}", methods=["GET", "POST", "PUT", "PATCH", "DELETE"])
async def proxy(path: str, request: Request):
    # Читаем тело запроса
    body = await request.body()

    # Берём все заголовки от клиента
    headers = dict(request.headers)

    # Формируем полный URL к OpenAI API
    url = f"https://api.openai.com/{path}"

    async with httpx.AsyncClient(timeout=None) as client:
        # stream=True даёт нам поток без закрытия, пока мы его не дочитаем
        upstream = await client.request(
            request.method,
            url,
            headers=headers,
            content=body,
            stream=True
        )

        return StreamingResponse(
            upstream.aiter_raw(),
            status_code=upstream.status_code,
            headers=dict(upstream.headers)
        )
