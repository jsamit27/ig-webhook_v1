import os
import asyncio
from typing import Any, Dict, List
from fastapi import FastAPI, Request, HTTPException
import httpx

app = FastAPI()

# --- ENV ---
VERIFY_TOKEN       = os.getenv("VERIFY_TOKEN", "set-a-verify-token")
FB_GRAPH_VERSION   = os.getenv("FB_GRAPH_VERSION", "v24.0")
FB_GRAPH           = f"https://graph.facebook.com/{FB_GRAPH_VERSION}"
FB_LL_USER_TOKEN   = os.getenv("FB_LL_USER_ACCESS_TOKEN")  # your 60-day token
FB_PAGE_ID         = os.getenv("FB_PAGE_ID", "305423772513")  # JCW page id

if not FB_LL_USER_TOKEN:
    # You can temporarily use a short-lived user token to test, but LL is recommended.
    print("WARN: FB_LL_USER_ACCESS_TOKEN not set. Use a valid user token in env.")

# --- Helpers ---
async def mint_page_token() -> str:
    """Derive a Page token from the (long-lived) user token."""
    async with httpx.AsyncClient(timeout=15) as client:
        r = await client.get(
            f"{FB_GRAPH}/{FB_PAGE_ID}",
            params={"fields": "access_token", "access_token": FB_LL_USER_TOKEN},
        )
        r.raise_for_status()
        js = r.json()
        token = js.get("access_token")
        if not token:
            raise RuntimeError(f"Could not mint page token: {js}")
        return token

async def get_comment_detail(comment_id: str, page_token: str) -> Dict[str, Any]:
    """Pull human-friendly comment details."""
    fields = "id,text,username,timestamp,like_count,media"
    async with httpx.AsyncClient(timeout=15) as client:
        r = await client.get(
            f"{FB_GRAPH}/{comment_id}",
            params={"fields": fields, "access_token": page_token},
        )
        r.raise_for_status()
        return r.json()

async def get_media_permalink(media_id: str, page_token: str) -> str:
    async with httpx.AsyncClient(timeout=15) as client:
        r = await client.get(
            f"{FB_GRAPH}/{media_id}",
            params={"fields": "permalink", "access_token": page_token},
        )
        r.raise_for_status()
        return r.json().get("permalink", "")

# --- Webhook verify (GET) ---
@app.get("/instagram/webhook")
async def verify(hub_mode: str = "", hub_verify_token: str = "", hub_challenge: str = ""):
    # Meta calls this once when you click "Verify and Save"
    if hub_mode == "subscribe" and hub_verify_token == VERIFY_TOKEN:
        return hub_challenge
    raise HTTPException(status_code=403, detail="Invalid verification token")

# --- Webhook receive (POST) ---
@app.post("/instagram/webhook")
async def receive(request: Request):
    """
    Receive Instagram webhooks. We:
      - parse 'comments' changes
      - mint Page token
      - fetch full comment detail + media permalink
      - return them in response (so you can see it immediately in logs/response)
    """
    body = await request.json()
    entries: List[Dict[str, Any]] = body.get("entry", [])
    if not entries:
        return {"status": "ok", "received": False}

    # Page token (once per request)
    page_token = await mint_page_token()

    enriched: List[Dict[str, Any]] = []

    async def handle_change(val: Dict[str, Any]):
        comment_id = val.get("id")
        if not comment_id:
            return
        c = await get_comment_detail(comment_id, page_token)
        media_id = (c.get("media") or {}).get("id") or val.get("media_id")
        permalink = await get_media_permalink(media_id, page_token) if media_id else ""
        enriched.append({
            "comment_id": c.get("id"),
            "text": c.get("text"),
            "username": c.get("username"),
            "timestamp": c.get("timestamp"),
            "like_count": c.get("like_count"),
            "media_id": media_id,
            "permalink": permalink,
        })

    tasks = []
    for e in entries:
        for ch in e.get("changes", []):
            if ch.get("field") == "comments":
                tasks.append(handle_change(ch.get("value", {})))

    if tasks:
        await asyncio.gather(*tasks, return_exceptions=True)

    # Respond quickly with what we enriched (great for testing)
    return {"status": "ok", "count": len(enriched), "comments": enriched}
