"""
sheets_service.py - Google Sheets API wrapper.

Builds a fresh Sheets client for each operation and provides a helper
to append a new HITL review row to the configured spreadsheet.
"""

from __future__ import annotations

import logging
import os
import re
import socket
import ssl
import threading
import time
from datetime import datetime, timezone
from typing import Any, Callable

import httplib2
from google.auth.exceptions import TransportError
from google.oauth2.service_account import Credentials
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError

import config

logger = logging.getLogger(__name__)

# ── Sheets client construction ───────────────────────────────────────
_auth_log_lock = threading.Lock()
_auth_mode_logged = False


def get_sheets_service():
    """Build a fresh Google Sheets API v4 service object for the current operation."""
    sa_path = config.SERVICE_ACCOUNT_PATH

    # google-api-python-client uses httplib2 underneath, and its Http transport
    # is not thread-safe. The review workers can run on different threads, so
    # reusing one global service can trigger socket aborts on Windows.
    if os.path.exists(sa_path):
        creds = Credentials.from_service_account_file(
            sa_path,
            scopes=["https://www.googleapis.com/auth/spreadsheets"],
        )
        _log_auth_mode_once(using_service_account=True, sa_path=sa_path)
        return build("sheets", "v4", credentials=creds, cache_discovery=False)

    _log_auth_mode_once(using_service_account=False, sa_path=sa_path)
    return build(
        "sheets",
        "v4",
        developerKey=config.SHEETS_API_KEY or config.GOOGLE_API_KEY,
        cache_discovery=False,
    )


def _log_auth_mode_once(*, using_service_account: bool, sa_path: str) -> None:
    global _auth_mode_logged
    if _auth_mode_logged:
        return

    with _auth_log_lock:
        if _auth_mode_logged:
            return
        if using_service_account:
            logger.info("Using service account '%s' for Sheets API authentication.", sa_path)
        else:
            logger.warning(
                "No service account file at '%s'. Using API Key (may fail on writes).",
                sa_path,
            )
        _auth_mode_logged = True


# ── Retry helper ─────────────────────────────────────────────────────

MAX_RETRIES = 5
INITIAL_BACKOFF = 1.0  # seconds

TRANSIENT_NETWORK_ERRORS = (
    ConnectionError,
    TimeoutError,
    socket.timeout,
    ssl.SSLError,
    httplib2.HttpLib2Error,
    TransportError,
)


def _retry_api_call(fn: Callable[[], Any], *, description: str = "API call"):
    """
    Execute *fn()* with exponential backoff on 429/5xx errors.

    Returns the result of fn() on success, raises on exhaustion.
    """
    backoff = INITIAL_BACKOFF
    for attempt in range(1, MAX_RETRIES + 1):
        try:
            return fn()
        except HttpError as exc:
            status = exc.resp.status if exc.resp else 0
            if status in (429, 500, 502, 503) and attempt < MAX_RETRIES:
                logger.warning(
                    "%s failed (HTTP %d), retrying in %.1fs (attempt %d/%d)",
                    description, status, backoff, attempt, MAX_RETRIES,
                )
                time.sleep(backoff)
                backoff *= 2
            elif status == 403:
                logger.error(
                    "%s returned HTTP 403 - Permission denied. "
                    "Ensure the service account email has Editor access to the spreadsheet.",
                    description,
                )
                raise
            elif status == 404:
                logger.error(
                    "%s returned HTTP 404 - Spreadsheet or sheet tab not found. "
                    "Check SPREADSHEET_ID='%s' and SHEET_NAME='%s' in .env.",
                    description, config.SPREADSHEET_ID, config.SHEET_NAME,
                )
                raise
            else:
                raise
        except TRANSIENT_NETWORK_ERRORS as exc:
            if attempt < MAX_RETRIES:
                logger.warning(
                    "%s failed (%s: %s), retrying in %.1fs (attempt %d/%d)",
                    description,
                    type(exc).__name__,
                    exc,
                    backoff,
                    attempt,
                    MAX_RETRIES,
                )
                time.sleep(backoff)
                backoff *= 2
            else:
                raise
    raise RuntimeError(f"{description} failed after {MAX_RETRIES} retries")


def _sheet_timestamp() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")


def append_review_row(
    session_id: str,
    player_id: str,
    player_name: str = "",
    channel: str = "Chat",
) -> dict[str, Any]:
    """
    Append a new review row with Session ID, Player ID, Name, and Channel.

    Column layout:
        A - Unused helper column owned by the sheet, not the backend
        B - Timestamp (written by backend)
        C - Player ID
        D - Player Name
        E - Channel
        F-J - Reserved (Agent, Decision, Notes, etc.)
        K - Session ID (hidden reference column)

    Returns:
        dict with ``result`` (Sheets API response) and ``row_number`` (int).

    Raises:
        HttpError: On unrecoverable Sheets API failure.
    """
    # The operational contract starts at Column B. Column A stays outside the
    # backend payload so sheet-specific helper data cannot shift the webhook map.
    row_data = [
        _sheet_timestamp(),  # B - Timestamp (written by backend)
        player_id,   # C - Player ID
        player_name, # D - Player Name
        channel,     # E - Channel
        "", "", "", "", "", # F through J (Empty)
        session_id,  # K - Session ID
    ]

    try:
        result = _retry_api_call(
            lambda: get_sheets_service().spreadsheets().values().append(
                spreadsheetId=config.SPREADSHEET_ID,
                range=f"{config.SHEET_NAME}!B:K",
                valueInputOption="USER_ENTERED",
                insertDataOption="OVERWRITE",
                includeValuesInResponse=False,
                body={"values": [row_data]},
            ).execute(),
            description="Append review row",
        )

        row_number = _extract_row_number(result)
        logger.info(
            "Sheet row appended at row %d (session=%s, player=%s)",
            row_number,
            session_id,
            player_id,
        )
        return {"result": result, "row_number": row_number}
    except Exception:
        logger.exception("Failed to append row in Google Sheet (session=%s)", session_id)
        raise


def get_review_row(row_number: int) -> dict[str, Any] | None:
    """Fetch a review row using the canonical Columns B:K contract."""
    result = _retry_api_call(
        lambda: get_sheets_service().spreadsheets().values().get(
            spreadsheetId=config.SPREADSHEET_ID,
            range=f"{config.SHEET_NAME}!B{row_number}:K{row_number}",
        ).execute(),
        description=f"Fetch review row {row_number}",
    )

    values = result.get("values", [])
    if not values:
        return None

    padded_row = list(values[0])
    if len(padded_row) < 10:
        padded_row.extend([""] * (10 - len(padded_row)))

    row_data = padded_row[:9]
    return {
        "row_data": row_data,
        "decision": str(row_data[7]).strip() if row_data[7] is not None else "",
        "notes": str(row_data[8]).strip() if row_data[8] is not None else "",
        "session_id": str(padded_row[9]).strip() if padded_row[9] is not None else "",
    }


def _extract_row_number(result: dict[str, Any]) -> int:
    updates = result.get("updates", {})
    updated_range = updates.get("updatedRange", "")
    match = re.search(r"![A-Z]+(\d+):[A-Z]+\d+$", updated_range)
    if not match:
        raise ValueError(f"Could not determine appended row number from result: {updated_range!r}")
    return int(match.group(1))
