# main.py — FastAPI Instagram comments webhook (read-only, fast-path + fallback)

import os, asyncio, json, traceback
from collections import deque
from typing import Any, Dict, List

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
    """Swap long-lived user token -> Page token."""
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
    """Fetch readable comment fields (fallback when payload lacks values)."""
    fields = "id,text,username,timestamp,like_count,media"
    async with httpx.AsyncClient(timeout=15) as client:
        r = await client.get(
            f"{FB_GRAPH}/{comment_id}",
            params={"fields": fields, "access_token": page_token},
        )
        r.raise_for_status()
        return r.json()

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
    # Return plain text challenge for Meta verification
    if hub_mode == "subscribe" and hub_verify_token == VERIFY_TOKEN:
        return PlainTextResponse(hub_challenge)
    raise HTTPException(status_code=403, detail="Invalid verification token")

# ===== Webhook receive (POST) =====
@app.post("/instagram/webhook")
async def receive(request: Request):
    body = await request.json()
    print("WEBHOOK RAW:", json.dumps(body, ensure_ascii=False))

    changes: List[Dict[str, Any]] = []

    # Shape A: {"object":"instagram","entry":[{"changes":[{...}]}]}
    if isinstance(body, dict) and "entry" in body:
        for e in body.get("entry", []):
            for ch in e.get("changes", []):
                changes.append(ch)
    # Shape B: {"field":"comments","value":{...}} (Dashboard "Send Test")
    elif isinstance(body, dict) and "field" in body and "value" in body:
        changes.append({"field": body.get("field"), "value": body.get("value")})

    if not changes:
        return {"status": "ok", "received": False, "reason": "no changes"}

    enriched: List[Dict[str, Any]] = []

    async def handle_change(ch: Dict[str, Any]):
        try:
            if ch.get("field") != "comments":
                return
            val = ch.get("value", {}) or {}

            # 1) Dashboard test payload → use as-is
            frm = val.get("from") or {}
            if frm.get("username") == "test":
                item = {
                    "test_event": True,
                    "comment_id": val.get("id"),
                    "text": val.get("text"),
                    "username": "test",
                    "from_id": frm.get("id"),
                    "media_id": (val.get("media") or {}).get("id"),
                }
                print("IG COMMENT (TEST):", json.dumps(item, ensure_ascii=False))
                RECENT.append(item)
                enriched.append(item)
                return

            # 2) Real event → prefer values already in webhook
            text       = val.get("text")
            username   = (val.get("from") or {}).get("username")
            comment_id = val.get("id")
            media_id   = (val.get("media") or {}).get("id")

            if text and username and comment_id:
                item = {
                    "comment_id": comment_id,
                    "text": text,
                    "username": username,
                    "timestamp": val.get("timestamp"),   # may be absent
                    "like_count": val.get("like_count"), # may be absent
                    "media_id": media_id,
                }
                print("IG COMMENT:", json.dumps(item, ensure_ascii=False))
                RECENT.append(item)
                enriched.append(item)
                return

            # 3) Fallback → enrich via Graph if fields missing
            if comment_id:
                page_token = await mint_page_token()
                c = await get_comment_detail(comment_id, page_token)
                item = {
                    "comment_id": c.get("id"),
                    "text": c.get("text"),
                    "username": c.get("username"),
                    "timestamp": c.get("timestamp"),
                    "like_count": c.get("like_count"),
                    "media_id": (c.get("media") or {}).get("id") or media_id,
                }
                print("IG COMMENT (ENRICHED):", json.dumps(item, ensure_ascii=False))
                RECENT.append(item)
                enriched.append(item)

        except Exception:
            print("ERR handle_change:", traceback.format_exc())

    await asyncio.gather(*(handle_change(ch) for ch in changes), return_exceptions=True)
    return {"status": "ok", "count": len(enriched), "comments": enriched}

# ===== Quick viewer for recent events =====
@app.get("/instagram/last")
async def last():
    return {"count": len(RECENT), "comments": list(RECENT)}
