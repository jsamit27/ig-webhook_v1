# main.py — FastAPI Instagram comments webhook (read-only)

import os, asyncio, json
from collections import deque
from typing import Any, Dict, List

import traceback


from fastapi import FastAPI, Request, HTTPException, Query
from fastapi.responses import PlainTextResponse
import httpx

app = FastAPI()

# ===== ENV =====
VERIFY_TOKEN       = os.getenv("VERIFY_TOKEN", "set-a-verify-token")
FB_GRAPH_VERSION   = os.getenv("FB_GRAPH_VERSION", "v24.0")
FB_GRAPH           = f"https://graph.facebook.com/{FB_GRAPH_VERSION}"
FB_LL_USER_TOKEN   = os.getenv("FB_LL_USER_ACCESS_TOKEN")  # 60-day user token
FB_PAGE_ID         = os.getenv("FB_PAGE_ID", "305423772513")  # JCW page id

if not FB_LL_USER_TOKEN:
    print("WARN: FB_LL_USER_ACCESS_TOKEN not set. Mint a long-lived user token and set it in env.")

RECENT = deque(maxlen=20)

# ===== Helpers =====
async def mint_page_token() -> str:
    if not FB_LL_USER_TOKEN:
        raise RuntimeError("FB_LL_USER_ACCESS_TOKEN is missing.")
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

# ===== Health check =====
@app.get("/healthz")
async def healthz():
    return {"ok": True}

# ===== Webhook verify (GET) =====
@app.get("/instagram/webhook")
async def verify(
    hub_mode: str = Query("", alias="hub.mode"),
    hub_verify_token: str = Query("", alias="hub.verify_token"),
    hub_challenge: str = Query("", alias="hub.challenge"),
):
    if hub_mode == "subscribe" and hub_verify_token == VERIFY_TOKEN:
        return PlainTextResponse(hub_challenge)
    raise HTTPException(status_code=403, detail="Invalid verification token")

# ===== Webhook receive (POST) =====
@app.post("/instagram/webhook")
async def receive(request: Request):
    """
    Handles Instagram webhooks:
      - Logs raw payload (so Meta 'Test' shows in logs)
      - For dashboard test payloads (username='test'): logs without enrichment
      - For real events: mints page token and enriches via Graph
    """
    body = await request.json()
    print("WEBHOOK RAW:", json.dumps(body, ensure_ascii=False))

    entries: List[Dict[str, Any]] = body.get("entry", [])
    if not entries:
        return {"status": "ok", "received": False}

    enriched: List[Dict[str, Any]] = []

    async def handle_change(val: Dict[str, Any]):
        try:
            # Dashboard "Send to server" uses fake ids/usernames (username=='test')
            frm = val.get("from") or {}
            if frm.get("username") == "test":
                item = {
                    "test_event": True,
                    "field": "comments",
                    "text": val.get("text"),
                    "from_id": frm.get("id"),
                    "media_id": (val.get("media") or {}).get("id"),
                }
                print("IG COMMENT (TEST):", json.dumps(item, ensure_ascii=False))
                enriched.append(item)
                return

            # Real event → enrich with Graph
            page_token = await mint_page_token()

            comment_id = val.get("id")
            if not comment_id:
                return

            c = await get_comment_detail(comment_id, page_token)
            media_id = (c.get("media") or {}).get("id") or val.get("media_id")
            permalink = await get_media_permalink(media_id, page_token) if media_id else ""

            item = {
                "comment_id": c.get("id"),
                "text": c.get("text"),
                "username": c.get("username"),
                "timestamp": c.get("timestamp"),
                "like_count": c.get("like_count"),
                "media_id": media_id,
                "permalink": permalink,
            }
            print("IG COMMENT:", json.dumps(item, ensure_ascii=False))
            enriched.append(item)
            RECENT.append(item)

        except Exception:
            # make failures visible in Render logs
            print("ERR handle_change:", traceback.format_exc())

    tasks = []
    for e in entries:
        for ch in e.get("changes", []):
            if ch.get("field") == "comments":
                tasks.append(handle_change(ch.get("value", {})))

    if tasks:
        await asyncio.gather(*tasks, return_exceptions=True)

    return {"status": "ok", "count": len(enriched), "comments": enriched}


# ===== Quick viewer for recent events =====
@app.get("/instagram/last")
async def last():
    return {"count": len(RECENT), "comments": list(RECENT)}
