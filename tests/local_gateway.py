"""Tiny local-only reverse proxy used for browser QA without Docker."""
import httpx
from fastapi import FastAPI, Request
from fastapi.responses import Response

app = FastAPI()


async def forward(request: Request, target: str):
    body = await request.body()
    headers = {k: v for k, v in request.headers.items() if k.lower() not in {"host", "content-length", "connection", "accept-encoding"}}
    async with httpx.AsyncClient(timeout=180, follow_redirects=False) as client:
        result = await client.request(request.method, target, params=request.query_params, content=body, headers=headers)
    response_headers = {k: v for k, v in result.headers.items() if k.lower() not in {"content-encoding", "transfer-encoding", "connection", "content-length"}}
    return Response(result.content, status_code=result.status_code, headers=response_headers, media_type=result.headers.get("content-type"))


@app.api_route("/api/{path:path}", methods=["GET", "POST", "PUT", "PATCH", "DELETE", "OPTIONS"])
async def api(request: Request, path: str):
    return await forward(request, f"http://127.0.0.1:8010/api/{path}")


@app.api_route("/{path:path}", methods=["GET", "POST", "PUT", "PATCH", "DELETE", "OPTIONS"])
async def frontend(request: Request, path: str):
    return await forward(request, f"http://127.0.0.1:3010/{path}")
