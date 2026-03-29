"""
main.py — FastAPI server with webhook endpoint and dev utilities.

Run with:
    python main.py
or:
    uvicorn main:app --reload --port 8000
"""

from __future__ import annotations

import asyncio
import logging
import os
import time
import uuid
from contextlib import asynccontextmanager
from typing import Any, Literal

import uvicorn
from fastapi import FastAPI, HTTPException, Header, BackgroundTasks, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

import config
import agent as agent_module

logger = logging.getLogger(__name__)

# ── Runtime metrics ──────────────────────────────────────────────────
_startup_time: float = 0.0
_metrics = {
    "requests_total": 0,
    "requests_succeeded": 0,
    "requests_failed": 0,
    "webhooks_received": 0,
    "webhooks_corrections": 0,
}


# ── Pydantic models ──────────────────────────────────────────────────


class WebhookPayload(BaseModel):
    """Payload sent by Google Apps Script when a human edits the Sheet."""

    session_id: str = Field(..., description="ADK session ID from Column K")
    decision: str = Field(..., description="'Yes' or 'No' from Column I")
    notes: str = Field("", description="Context/notes from Column J")
    row_number: int | None = Field(None, description="The specific row being edited")
    row_data: list[Any] = Field(default_factory=list, description="Array of columns A to J")


class WithdrawalRequest(BaseModel):
    """Dev/test endpoint payload to trigger a new withdrawal."""

    session_id: str
    player_id: str


class AdaWithdrawalRequest(BaseModel):
    """Payload sent by ADA Chatbot to trigger a withdrawal check."""

    player_id: str
    player_name: str = ""
    channel: Literal["Chat", "Email"] = "Chat"


# ── Lifespan ─────────────────────────────────────────────────────────


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Startup / shutdown hooks."""
    global _startup_time
    _startup_time = time.monotonic()

    config.setup_logging()
    config.validate()

    # Set the Gemini API key in the environment for google-genai SDK
    os.environ["GOOGLE_API_KEY"] = config.GOOGLE_API_KEY

    logger.info("🚀 HITL Payment Automation server starting …")
    logger.info("   Model   : %s", config.MODEL_ID)
    logger.info("   Sheet   : %s", config.SPREADSHEET_ID[:12] + "…")
    logger.info("   SA Path : %s", config.SERVICE_ACCOUNT_PATH)
    logger.info("   LLM Pool: %d concurrent", config.LLM_CONCURRENCY_LIMIT)
    logger.info("   Mode    : ALL withdrawals require human approval")
    yield
    logger.info("👋 Server shutting down")


# ── FastAPI app ──────────────────────────────────────────────────────

app = FastAPI(
    title="HITL Payment Automation",
    description=(
        "Human-in-the-Loop payment automation for regulated iGaming platforms. "
        "Uses Google ADK agents with mandatory human review via Google Sheets."
    ),
    version="2.0.0",
    lifespan=lifespan,
    docs_url="/docs",
    redoc_url="/redoc",
)

# ── CORS Middleware ──────────────────────────────────────────────────
# Allows cross-origin requests from ADA chatbot embeds and admin dashboards.
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],  # Restrict in production to specific domains
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


# ── Endpoints ────────────────────────────────────────────────────────


@app.get("/health")
async def health():
    """Health-check endpoint with uptime and pending session count."""
    uptime_seconds = time.monotonic() - _startup_time if _startup_time else 0
    return {
        "status": "ok",
        "uptime_seconds": round(uptime_seconds, 1),
        "pending_sessions": len(agent_module.pending_sessions),
        "model": config.MODEL_ID,
    }


@app.get("/metrics")
async def metrics():
    """Operational metrics for monitoring and observability."""
    uptime_seconds = time.monotonic() - _startup_time if _startup_time else 0
    return {
        "uptime_seconds": round(uptime_seconds, 1),
        "pending_sessions": len(agent_module.pending_sessions),
        "tracked_statuses": len(agent_module.player_status),
        "llm_concurrency_limit": config.LLM_CONCURRENCY_LIMIT,
        **_metrics,
    }


@app.get("/sessions")
async def list_sessions():
    """List all pending HITL sessions (for debugging)."""
    return {
        "pending_count": len(agent_module.pending_sessions),
        "session_ids": list(agent_module.pending_sessions.keys()),
    }


@app.post("/webhook")
async def webhook(
    payload: WebhookPayload,
    x_webhook_secret: str | None = Header(None),
):
    """
    Receive the human decision from Google Apps Script.

    Apps Script sends:
      { "session_id": "...", "decision": "Yes|No", "notes": "..." }

    This endpoint resumes the paused ADK agent with that decision.
    """
    _metrics["webhooks_received"] += 1

    # Optional shared-secret validation
    if config.WEBHOOK_SECRET and x_webhook_secret != config.WEBHOOK_SECRET:
        raise HTTPException(status_code=403, detail="Invalid webhook secret")

    logger.info(
        "📩 Webhook received: session=%s decision=%s notes=%s",
        payload.session_id,
        payload.decision,
        payload.notes,
    )

    if payload.session_id not in agent_module.pending_sessions:
        # If the human agent corrects a typo after it was already approved,
        # we can forcefully update the ADA chatbot polling dictionary directly
        # by extracting the Player ID (Column C, index 2).
        if len(payload.row_data) > 2:
            player_id = payload.row_data[2]
            if player_id:
                # Use row_number in the key if available, else fallback to player_id
                status_key = f"{player_id}:{payload.row_number}" if payload.row_number else player_id
                agent_module.player_status[status_key] = {
                    "decision": payload.decision,
                    "notes": payload.notes,
                    "row_data": payload.row_data,
                }
                _metrics["webhooks_corrections"] += 1
                logger.info("📝 Applied human correction to finalized session %s (key=%s)", payload.session_id, status_key)
                return {"status": "corrected", "message": f"Updated existing record for {status_key}"}

        raise HTTPException(
            status_code=404,
            detail=f"No pending session found for session_id={payload.session_id}",
        )

    try:
        result = await agent_module.resume_withdrawal(
            session_id=payload.session_id,
            decision=payload.decision,
            notes=payload.notes,
            row_data=payload.row_data,
            current_row_number=payload.row_number,
        )
        _metrics["requests_succeeded"] += 1
        return result
    except Exception as exc:
        _metrics["requests_failed"] += 1
        logger.exception("Error resuming session %s", payload.session_id)
        raise HTTPException(status_code=500, detail=str(exc)) from exc


@app.post("/test/withdrawal")
async def test_withdrawal(req: WithdrawalRequest):
    """
    Dev-only endpoint — trigger a new withdrawal flow via HTTP.

    Useful for testing without an external caller.
    """
    _metrics["requests_total"] += 1

    logger.info(
        "🧪 Test withdrawal: session=%s player=%s",
        req.session_id,
        req.player_id,
    )

    try:
        result = await agent_module.start_withdrawal(
            session_id=req.session_id,
            player_id=req.player_id,
            # We add dummy name/channel for the test endpoint
            player_name="Test-User",
            channel="Chat",
        )
        return result
    except Exception as exc:
        _metrics["requests_failed"] += 1
        logger.exception("Error starting withdrawal %s", req.session_id)
        raise HTTPException(status_code=500, detail=str(exc)) from exc


@app.post("/hitl/v1/request_review")
async def ada_request_review(req: AdaWithdrawalRequest):
    """
    ADA Chatbot endpoint. ADA usually supplies only the player ID.
    Now including row_number in the response for multiple requests.
    """
    _metrics["requests_total"] += 1

    logger.info("🤖 ADA Request via Chatbot: player=%s", req.player_id)

    session_id = f"ada-{uuid.uuid4().hex}"
    logger.info("   … Generated new session=%s", session_id)

    try:
        # We wait for the first turn (tool call) to get the row number
        result = await agent_module.start_withdrawal(
            session_id=session_id,
            player_id=req.player_id,
            player_name=req.player_name,
            channel=req.channel,
        )

        # Check if the agent actually succeeded in escalating
        status = result.get("status", "")
        if status == "pending_human_review":
            row_number = result.get("row_number")
            logger.info("✅ Agent waiting for human review at row %s", row_number)
            return {
                "status": "pending_human_review",
                "session_id": session_id,
                "row_number": row_number
            }
        else:
            _metrics["requests_failed"] += 1
            logger.error("❌ Agent did not escalate to HITL. Status: %s", status)
            raise HTTPException(status_code=500, detail=f"Unexpected status: {status}")

    except HTTPException:
        raise  # re-raise FastAPI HTTPException as-is
    except Exception as exc:
        _metrics["requests_failed"] += 1
        logger.exception("🚨 Error starting ADA withdrawal session=%s", session_id)
        raise HTTPException(status_code=500, detail=str(exc))


@app.get("/hitl/v1/status/{player_id}/{row_number}")
async def ada_check_status(player_id: str, row_number: int):
    """
    ADA Chatbot endpoint to poll for the human decision.
    Returns: 'pending', 'Yes', 'No', or 'not_found' and any notes
    """
    status_key = f"{player_id}:{row_number}"
    status_data = agent_module.player_status.get(status_key, {"decision": "not_found", "notes": ""})
    return {
        "player_id": player_id,
        "row_number": row_number,
        "decision": status_data["decision"],
        "notes": status_data["notes"],
        "row_data": status_data.get("row_data", []),
    }


# ── Entry-point ──────────────────────────────────────────────────────

if __name__ == "__main__":
    uvicorn.run(
        "main:app",
        host=config.HOST,
        port=config.PORT,
        reload=False,
        log_level="info",
    )
