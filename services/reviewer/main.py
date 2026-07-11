import time

import httpx
import jwt
from fastapi import FastAPI
from prometheus_fastapi_instrumentator import Instrumentator
from sqlalchemy import update
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine
from sqlalchemy.orm import sessionmaker

from models import Settings, PullRequest, ReviewRequest

settings = Settings()
engine = create_async_engine(settings.database_url)
AsyncSessionLocal = sessionmaker(engine, class_ = AsyncSession, expire_on_commit = False)

app = FastAPI()
Instrumentator().instrument(app).expose(app)

@app.get("/health")
async def health():
    return {"status":"ok"}

def _finding_summary_line(f: dict) -> str:
    severity = (f.get("severity") or "info").upper()
    file = f.get("file") or "unknown"
    line = f.get("line")
    line = line if line is not None else "?"
    agent = f.get("agent") or ""
    message = f.get("message") or ""
    return f"**[{severity}]** `{file}:{line}` ({agent})\n{message}\n"

def _build_summary(findings: list) -> str:
    lines = ["## AI Code Review\n"] + [_finding_summary_line(f) for f in findings]
    return "\n".join(lines)


@app.post("/post-review")
async def post_review(request: ReviewRequest):
    if request.findings:
        token = await get_installation_token(request.installation_id)

        inline_comments = []
        for f in request.findings:
            try:
                line = int(f.get("line") or 0)
            except (ValueError, TypeError):
                line = 0
            if f.get("file") and line > 0:
                inline_comments.append({
                    "path": f.get("file"),
                    "line": line,
                    "side": "RIGHT",
                    "body": f"**[{(f.get('severity') or 'info').upper()}]** ({f.get('agent') or ''})\n{f.get('message') or ''}\n"
                })
        headers = {
            "Authorization": f"Bearer {token}",
            "Accept": "application/vnd.github.v3+json",
        }

        url = f"https://api.github.com/repos/{request.repo_full_name}/pulls/{request.pr_number}/reviews"
        summary = _build_summary(request.findings)

        async with httpx.AsyncClient() as client:
            response = await client.post(
                url,
                json = {"event": "COMMENT", "body": summary,"comments": inline_comments},
                headers = headers,
                timeout = 30
            )
            if response.status_code == 422 and inline_comments:
                response = await client.post(
                    url,
                    json = {"event": "COMMENT", "body": summary},
                    headers = headers,
                    timeout = 30

                )
            response.raise_for_status()

    async with AsyncSessionLocal() as session:
        await session.execute(
            update(PullRequest).where(PullRequest.id == request.pr_id).values(status = "reviewed")
        )
        await session.commit()
    return {"status": "ok"}

async def get_installation_token(installation_id : int) -> str:
    now = int(time.time())
    payload = {"iat": now - 60, "exp": now + 600, "iss": settings.github_app_id}
    private_key = settings.github_app_private_key.replace("\\n","\n")
    encoded_jwt = jwt.encode(payload, private_key, algorithm = "RS256")
    async with httpx.AsyncClient() as client:
        response = await client.post(
            f"https://api.github.com/app/installations/{installation_id}/access_tokens",
            headers = {
                "Authorization": f"Bearer {encoded_jwt}",
                "Accept":  "application/vnd.github.v3+json",
            }
        )
        response.raise_for_status()
        return response.json()["token"]
