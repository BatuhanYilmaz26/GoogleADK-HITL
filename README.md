# HITL Payment Automation — FastAPI + Google Sheets

![Python 3.11+](https://img.shields.io/badge/Python-3.11%2B-blue?logo=python&logoColor=white) ![FastAPI](https://img.shields.io/badge/FastAPI-0.115%2B-009688?logo=fastapi&logoColor=white) ![License: MIT](https://img.shields.io/badge/License-MIT-green)

## Overview

![HITL Automation](hitl_automation.png)

This system delivers **end-to-end withdrawal automation** - from the moment a player requests a withdrawal in the chatbot, to the final decision being relayed back, with zero manual data entry in between.

- **Fast & Durable**: The request path persists session and job state immediately, then dedicated workers process Google Sheets writes without blocking ADA.
- **Chatbot-native**: Players initiate withdrawals directly through the ADA chatbot — no context switching for the player or the agent.
- **Instant dashboard logging**: Every request is appended to the HITL Google Sheet with player details, backend timestamps, and session tracking.
- **Human-only decisions**: The system routes the requests and **never** approves or rejects a payment automatically — that authority stays with the human reviewer.
- **Real-time feedback loop**: The moment a reviewer types their decision, the chatbot is updated within seconds via an automated webhook pipeline.
- **Full audit trail**: Every request, decision, and note is captured with timestamps — ready for compliance and reporting.
- **Concurrent & resilient**: Session state survives restarts, review jobs are recoverable, and writes avoid full-sheet scans as volume grows.

---

## Architecture

### Sequence Flow

```mermaid
sequenceDiagram
    participant Player
    participant ADA as ADA Chatbot
    participant API as FastAPI Server
    participant Sheet as Google Sheet
    participant Human as Human Reviewer
    participant Script as Apps Script

    Player->>ADA: "I want to withdraw"
    ADA->>API: POST /hitl/v1/request_review
    API-->>ADA: {status: "processing", session_id: "xyz"}
    Note right of API: Session and review job are stored durably
    
    API->>Sheet: Append row (Timestamp, Player ID, Name, Channel)

    loop Every 10-15 seconds
        ADA->>API: GET /hitl/v1/status/session/{session_id}
        API-->>ADA: {status: "pending_human_review"}
    end

    Human->>Sheet: Types "Yes" in Col I, notes in Col J
    Sheet-->>Script: onEdit trigger
    Script->>API: POST /webhook {decision, notes, row_data}
    API->>API: Updates session dictionary
    API-->>Script: 200 OK

    ADA->>API: GET /hitl/v1/status/session/{session_id}
    API-->>ADA: {status: "approved", notes: "Verified", row_data: [...]}
    ADA-->>Player: "Your withdrawal has been approved!"
```

---

## Reliability Features

This system is designed so that **zero withdrawal requests are missed or skipped**, even under high concurrency:

| Feature                           | File                      | Description                                                                                                                     |
| --------------------------------- | ------------------------- | ------------------------------------------------------------------------------------------------------------------------------- |
| **Sheets Retry**            | `sheets_service.py`     | Exponential backoff covers both HTTP failures and transient transport errors such as Windows socket aborts.                    |
| **Fresh Sheets Client**     | `sheets_service.py`     | Builds a new Google Sheets client per operation, avoiding cross-thread reuse of `httplib2.Http()`.                             |
| **Atomic Sheets Append**    | `sheets_service.py`     | Uses a single `values.append` call instead of scanning `A5:K` to find the next row.                                           |
| **Durable Session Store**   | `session_store.py`      | Persists session status in SQLite so polling, webhook updates, and restarts stay in sync.                                      |
| **Durable Review Queue**    | `main.py`               | Review jobs are queued in SQLite and claimed by worker tasks, so requests are recoverable after restarts.                      |
| **Backend Timestamping**    | `sheets_service.py`     | The API writes Column B directly, removing the expensive Apps Script full-sheet `onChange` scan.                               |
| **Webhook Retry**           | `apps_script.js`        | Apps Script retries up to 3x with exponential backoff if the webhook fails.                                                    |
| **Operational Metrics**     | `main.py`               | `/metrics` exposes queue depth, worker counts, session status counts, and review job outcomes.                                |

---

## Security Model

| Layer                            | Implementation                                                                                     |
| -------------------------------- | -------------------------------------------------------------------------------------------------- |
| **Webhook Authentication** | Shared secret (`WEBHOOK_SECRET`) in HTTP header, validated by FastAPI middleware                 |
| **Sheets API Auth**        | Service account with narrow scope (`spreadsheets` only) — no OAuth user consent needed          |
| **Credential Management**  | All secrets in `.env` (gitignored), service account key in `service_account.json` (gitignored) |
| **CORS**                   | Configurable middleware — restrict to specific domains in production                              |
| **Input Validation**       | Pydantic models enforce strict typing on all request payloads                                      |

---

## Quick Start
1. Clone & Setup Venv. Install requirements.
2. Ensure you have the `.env` file populated.
3. Share the Google Sheet with the service account email as an Editor.
4. Run `python main.py`
5. Expose localhost with ngrok: `ngrok http 8000`

## Production Notes
- Keep `REQUIRE_SERVICE_ACCOUNT=true` in production so writes never fall back to API-key mode.
- Put `SESSION_DB_PATH` on a durable disk path that survives restarts and deployments.
- Tune `REVIEW_WORKER_COUNT` carefully against Google Sheets quota rather than CPU core count.
- Set `CORS_ALLOW_ORIGINS` to the exact ADA domains you expect instead of `*`.
- Only the Apps Script `onEdit` trigger is required now; backend writes Column B timestamps directly.

### Testing
```powershell
# 1. Submit a withdrawal with Name and Channel
Invoke-RestMethod -Uri "http://localhost:8000/hitl/v1/request_review" `
  -Method Post `
  -Headers @{"Content-Type"="application/json"} `
  -Body '{"player_id":"P100", "player_name":"Batuhan", "channel":"Chat"}'

# 2. Extract session_id from response

# 3. Go to Google Sheets and simulate human review:
#   - Decision (Column I) = 'Yes'
#   - Notes (Column J) = 'Verified manually'

# 4. Poll status
Invoke-RestMethod -Uri "http://localhost:8000/hitl/v1/status/session/YOUR_SESSION_ID"
```

## Tools Available
Run `python test_concurrent.py` to test the new Asynchronous logic for enterprise queues.
