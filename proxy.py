# proxy.py
# pip install fastapi uvicorn httpx

from fastapi import FastAPI, Request, HTTPException
from fastapi.responses import Response
import httpx

app = FastAPI()

def extract_key(headers) -> str | None:
    auth = headers.get("authorization")
    if auth and auth.lower().startswith("bearer "):
        return auth[7:].strip()
    if headers.get("x-openai-api-key"):
        return headers.get("x-openai-api-key")
    if headers.get("x-api-key"):
        return headers.get("x-api-key")
    return None

@app.api_route("/proxy/{path:path}", methods=["GET","POST","PUT","PATCH","DELETE","OPTIONS"])
async def proxy(path: str, request: Request):
    key = extract_key(request.headers)
    if not key:
        raise HTTPException(status_code=401, detail="no_api_key_provided")

    url = f"https://api.openai.com/{path}"
    params = dict(request.query_params)
    content = await request.body()
    headers = {
        "Authorization": f"Bearer {key}",
        "Content-Type": request.headers.get("content-type", "application/json"),
    }

    async with httpx.AsyncClient(timeout=120.0) as client:
        try:
            upstream = await client.request(request.method, url, params=params, content=content or None, headers=headers)
        except Exception as e:
            raise HTTPException(status_code=502, detail=str(e))

    # Скопируем заголовки, исключая hop-by-hop
    resp_headers = {k:v for k,v in upstream.headers.items() if k.lower() not in ("connection","keep-alive","transfer-encoding","content-encoding")}
    return Response(content=upstream.content, status_code=upstream.status_code, headers=resp_headers)
