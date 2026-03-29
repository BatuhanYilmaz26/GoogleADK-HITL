# Google ADK HITL Payment Automation: Complete Codebase Guide

This document is a comprehensive, step-by-step guide to understanding and building the **Human-in-the-Loop (HITL) Payment Automation** project from scratch. It explains the core logic, the architectural decisions, and the exact role of each module in the codebase.

By reading this guide, you will understand how to recreate this architecture without relying on an AI copilot.

---

## 1. Project Overview & Architecture

### The Goal
The purpose of this system is to handle automated withdrawal requests from players (typically originating from a chatbot like Ada.cx) but with a mandatory **Human-in-the-Loop** step. No AI agent is allowed to finalize a payment on its own.

### Core Flow
1. **Trigger:** A chatbot (e.g., Ada) sends a withdrawal request to our Python server (`main.py`).
2. **Agent Execution:** The Python server spins up a Google ADK LLM Agent (`agent.py`) instructed to handle withdrawals.
3. **Escalation:** The Agent's system instructions force it to *always* call a specific tool (`tools.py`) to request human review.
4. **Pause & Record:** The tool inserts a new pending row into a Google Spreadsheet (`sheets_service.py`) and returns a "pending" status. The ADK Agent pauses execution.
5. **Human Action:** A human reviewer opens the Google Sheet, looks at the withdrawal request, and types "Yes" or "No" in the Decision column.
6. **Webhook to Backend:** A Google Apps Script (`apps_script.js`) attached to the Spreadsheet detects this edit and immediately fires an HTTP payload (webhook) back to our Python server.
7. **Resumption:** The Python server receives the webhook, finds the paused Agent session, injects the human's "Yes" or "No" decision as the tool's final response, and lets the Agent resume to finish its execution.
8. **Final State:** The Agent crafts a final message (approved/rejected), and the chatbot polls our server to retrieve the final status.

---

## 2. Prerequisite Setup

### Dependencies (`requirements.txt`)
To achieve this, the project relies on a few key libraries:
*   `google-adk`: Google's Agent Development Kit, which manages the LLM agent and pausing/resuming tool calls.
*   `google-genai`: The underlying SDK for communicating with the Gemini model.
*   `google-api-python-client` & `google-auth`: For communicating securely with the Google Sheets API.
*   `fastapi` & `uvicorn`: To create the web server that listens for incoming request APIs and Google Sheet webhooks.
*   `python-dotenv`: To load configuration from a `.env` file securely.
*   `pydantic`: To validate and serialize all request/response payloads.

### Environment Management (`.env` and `config.py`)
Building from scratch, the first file you create after `requirements.txt` is **`config.py`**.

**Why centralized config?** Instead of using `os.getenv` randomly throughout your code, `config.py` parses the `.env` file once at startup and provides strongly-typed, validated constants. This ensures that if a critical key (like `GOOGLE_API_KEY` or `SPREADSHEET_ID`) is missing, the application fails immediately at startup rather than throwing a mysterious error later on.

`config.py` handles:
- Checking for essential API keys.
- Validating the service account file exists (with a warning, not a crash, since API-key fallback is available).
- Setting up a single, structured logging format used across the entire app.
- Defining LLM concurrency limits (`LLM_CONCURRENCY_LIMIT`) to avoid exhausting the Google Gemini API limits.
- Making `MODEL_ID` and `SERVICE_ACCOUNT_PATH` configurable via environment variables for easy deployment changes.

---

## 3. Building the Core Mechanics

### Connecting to Google Sheets (`sheets_service.py`)
Before the Agent can escalate anything, it needs a way to write to a "database". Here, the database is a Google Sheet.

The `sheets_service.py` is written as a **Thread-Safe Singleton**. It initializes the Google Sheets API client exactly once, protected by a `threading.Lock` with double-checked locking to prevent race conditions during initialization.

It exposes a main function: `append_review_row(session_id, player_id, ...)`.

**Key Architectural Logic here:**
*   **Race Conditions:** If two players request a withdrawal at the exact same millisecond, the system might try to overwrite the exact same row in the spreadsheet. To prevent this, `append_review_row` uses a Python `threading.Lock()` (`_append_lock`). This forces simultaneous requests to line up one-by-one locally before executing the Google API call, ensuring no Google Sheet row is overwritten.
*   **Retry Backoff:** Google APIs have rate limits locally and remotely. The `_retry_api_call` helper catches transient `HttpError` 429/5xx status codes and applies an exponential backoff (wait 1s, then 2s, then 4s, etc.) before trying again. It also provides specific, actionable error messages for 403 (permission denied) and 404 (spreadsheet not found) errors.
*   **Finding the Next Row:** Because the Google Sheet might have empty rows or headers, the code securely fetches a large range (`A5:K`), dynamically calculates the last filled row index, and safely writes to `last_filled + 1`. This format supports manual cleanups without breaking.

### Defining the Agent Tool (`tools.py`)
Google ADK allows base agents to interface with real-world functions using tools. We give our agent exactly one tool.

In `tools.py`, we define `request_human_approval`.
1. The tool accepts the `session_id`, `player_id`, `player_name`, and `channel`.
2. It calls `sheets_service.append_review_row(...)` to place it in the dashboard.
3. It returns a dictionary map with `{"status": "pending"}`.

**Crucial detail:** We wrap this function using ADK's `LongRunningFunctionTool`. When the ADK encounters a regular tool, it returns the output instantly to the LLM. When it hits a `LongRunningFunctionTool`, the ADK knows *not* to expect an immediate answer. It saves the entire conversation state and completely halts the LLM execution, waiting indefinitely for a webhook mechanism to wake it back up.

---

## 4. The Agent Brain (`agent.py`)

This is the most complex part of the codebase. It glues the standard Google ADK mechanics into our custom web logic.

**1. System Instructions:**
The prompt given to the `LlmAgent` is strictly guarded. It explicitly outlines instructions: *"You are a Payment Withdrawal Agent. You MUST call `request_human_approval` for every withdrawal. No auto-approvals."*

**2. The In-Memory Runner:**
`agent.py` instantiates an `InMemoryRunner`. The runner manages independent "conversations" (sessions) for multiple players concurrently.

**3. State Maps:**
We need to track paused sessions to resume them later. `agent.py` establishes two robust dictionaries in memory:
*   `pending_sessions`: Maps `session_id` → the paused ADK `FunctionResponse` object, row metadata, and a monotonic creation timestamp for TTL expiry.
*   `player_status`: Used by the Chatbot to constantly poll and ask "Is my request done yet?". Since Chatbots drop connections, holding state persistently in a map allows for stateless retrieval.

**4. TTL-Based Cleanup:**
To prevent memory leaks in long-running deployments, a periodic cleanup function (`_cleanup_stale_entries`) evicts pending sessions older than 24 hours. This runs passively on each new withdrawal request rather than requiring a background thread.

**5. `start_withdrawal()`:**
This asynchronous function is called when a new request comes in.
*   It packages the user's request context into a Prompt `types.Content`.
*   It feeds the request into the `runner.run_async()`.
*   **Safety net:** It traps the LLM call inside an `asyncio.Semaphore`. This acts as a traffic light. If the concurrency limit is set to '50', the 51st request will pause here until one of the first 50 finishes. This avoids "Resource Exhausted" API errors from Gemini.
*   Once the LLM outputs the tool call, the function intercepts it using helper extractors (`_extract_long_running_function_call`), saves the paused state to `pending_sessions`, initializes the Chatbot poll entry, and gracefully exits.

**6. `resume_withdrawal()`:**
This function is called *by the webhook payload* after a human says "Yes" or "No".
*   It retrieves the paused session (`FunctionResponse`).
*   It clones it and creates an updated response containing the human's decision.
*   It feeds this `FunctionResponse` right back into the `runner.run_async()`. The LLM wakes up, sees "Decision: Yes", and generates the final output message: "Withdrawal is RELEASED".
*   It uses `pop()` instead of `del` to safely handle double-webhook edge cases without crashing.
*   It saves the final human verdict into the `player_status` poll dictionary.

---

## 5. Exposing the API (`main.py`)

`main.py` is the front door of the application, built with FastAPI. It maps standard HTTP URLs to the complex Python functions inside `agent.py`.

*   **`/hitl/v1/request_review`**: The chatbot hits this. It explicitly calls `agent_module.start_withdrawal()`. If the LLM successfully pauses and escalates to the dashboard, it returns the `row_number` back to the chatbot safely.
*   **`/hitl/v1/status/{player_id}/{row_number}`**: Since webhooks can take minutes or hours to return (the reviewer might be out to lunch), ADA needs to query the status repeatedly. This endpoint safely checks the `player_status` dictionary in memory for the final decision.
*   **`/webhook`**: This is where Google Sheets fires its data backwards. It receives a JSON payload `{"session_id": "...", "decision": "Yes", "notes": "...", "row_data": [...]}`. It then calls `agent_module.resume_withdrawal()`.
*   **`/health`**: Health check with uptime tracking, pending session count, and model info.
*   **`/metrics`**: Operational metrics endpoint with request counters, success/failure rates, and concurrency stats for monitoring.
*   **`/test/withdrawal`**: Developer utility for testing without external webhooks.
*   **`/docs` & `/redoc`**: Auto-generated interactive API documentation via OpenAPI/Swagger.

**Cross-Origin Support:** CORS middleware is configured to allow cross-origin requests from chatbot embeds and admin dashboards.

---

## 6. Error Handling Patterns

The codebase uses a consistent error handling strategy across all modules:

| Pattern | Where Used | Behavior |
|---------|-----------|----------|
| **Fail-fast validation** | `config.py` → `validate()` | Missing critical env vars cause immediate `sys.exit()` at startup |
| **Retry with backoff** | `sheets_service.py` → `_retry_api_call()` | 429/5xx errors get exponential backoff (up to 5 retries) |
| **Specific error messages** | `sheets_service.py` | 403 = permission denied (actionable fix), 404 = sheet not found (check config) |
| **Semaphore throttling** | `agent.py` → `_llm_semaphore` | Prevents LLM API exhaustion by queuing excess requests |
| **Rate-limit retry** | `agent.py` → `start/resume_withdrawal()` | 429/ResourceExhausted get 3 attempts with 15s/30s/45s delays |
| **Safe deletion** | `agent.py` → `pending_sessions.pop()` | Guards against double-webhook race conditions |
| **Webhook retry** | `apps_script.js` → `onEdit()` | 3 retries with 2s/4s/8s exponential backoff |
| **Dead-letter logging** | `apps_script.js` → `_logDeadLetter()` | Failed webhooks logged to `ErrorLog` sheet tab |
| **Correction fallback** | `main.py` → `/webhook` | Post-finalization edits update the poll dictionary directly |
| **Config validation** | `apps_script.js` → `_validateConfig()` | Warns if WEBHOOK_URL is still the default placeholder |

---

## 7. Closing the Loop: The Spreadsheet (`apps_script.js`)

Our final puzzle piece lives inside Google Sheets. Without this, the Python server would never know a human wrote "Yes" or "No" in the spreadsheet.

`apps_script.js` contains two trigger functions and two helper functions:

**Triggers:**
1.  **`onChange(e)`**: When the Python backend API writes a new row, this script detects the architectural change and silently stamps the current Date/Time into Column B of that respective row.
2.  **`onEdit(e)`**: The workhorse trigger. Whenever a user types something in the sheet, this function fires.
    *   It validates the webhook URL hasn't been left as the default placeholder.
    *   It verifies if the edit happened in the "Decision" or "Notes" column.
    *   If both are filled out, it grabs the hidden Session ID (Column K) and the full row of context to prevent missing data.
    *   It bundles this into a JSON payload.
    *   It uses Google's `UrlFetchApp` to send an HTTP POST request (the webhook) to your ngrok URL (`main.py` → `/webhook`).
    *   **Resiliency:** Webhook deliveries can fail locally due to network hiccups. This Apps Script handles `URLFetchApp` errors and loops an exponential retry logic. If the local Python API is momentarily down, the script waits and tries sending the webhook again up to 3 times, ensuring human decisions are not lost.

**Helpers:**
1.  **`_validateConfig()`**: Pre-flight check that prevents webhook calls if the URL hasn't been configured.
2.  **`_logDeadLetter(row, sessionId, errorMsg)`**: When all webhook retries fail, this creates (if needed) an `ErrorLog` sheet tab and appends a row with the timestamp, row number, session ID, and error message — ensuring **no human decision is ever silently lost**.

---

## 8. Verifying the Logic (`test_concurrent.py`)

When building complex, multi-layered systems bridging asynchronous webhooks, LLMs, and external Google Sheets, race conditions are inevitable. What happens if 20 people request a withdrawal at exactly the same time? Does the LLM hit rate limits? Does the Google Sheet overwrite overlapping rows?

`test_concurrent.py` is a robust asynchronous testing suite using `aiohttp` meant for validation.

**Features:**
*   **Pre-flight check:** Validates the server is reachable before firing tests, with clear abort message if not.
*   **Two modes:** `burst` (all at once) and `staggered` (batches with configurable delay).
*   **Duplicate testing:** `--duplicate P100` sends multiple requests for the same player to test row isolation.
*   **Three-phase reporting:** Fire requests → Poll statuses → Check sessions.

By firing simultaneous packets via `burst` mode or staged intervals in `staggered` mode, developers can objectively verify that the Semaphore in `agent.py` and the threading Lock in `sheets_service.py` successfully protect data insertion from breaking under synthetic load testing.

---

## 9. Known Limitations

These are deliberate trade-offs for a PoC that should be addressed when transitioning to production:

| Limitation | Impact | Production Solution |
|-----------|--------|-------------------|
| **In-memory state** | Sessions lost on server restart | Replace with Cloud Firestore |
| **Single instance** | Cannot horizontally scale | Cloud Run + Firestore for shared state |
| **No persistent queue** | Webhook failures in Apps Script may need manual replay | Add Cloud Tasks or Pub/Sub |
| **No authentication** | API endpoints are open (relies on network isolation) | Add OAuth2/JWT via Cloud Run IAM |
| **Free-tier LLM limits** | 5 RPM throttles concurrent requests | Upgrade to Vertex AI Enterprise |
| **24h TTL cleanup** | Very long reviews (>24h) may expire | Extend TTL or use persistent storage |

---

## Quick Summary of How to Re-Build From Scratch

1. Scaffold standard server dependencies: `.env`, `requirements.txt`, `config.py`.
2. Map your external API dependencies robustly with concurrency locks and HTTP retries: `sheets_service.py`.
3. Inform your ADK Agent with strict guidelines and the `request_human_approval` tool setup: `tools.py`.
4. Tie the ADK Runner pipeline together to process the incoming prompt, intercept the LLM tool call, pause the session context, and structure waking hooks: `agent.py`.
5. Expose HTTP FastAPI routes so external chatbots and hooks can trigger start/resume actions cleanly: `main.py`.
6. Attach an Apps Script execution trigger to your production Google Sheet to dispatch webhooks on manual edits: `apps_script.js`.
7. Torture test the integration with overlapping traffic via a stress suite: `test_concurrent.py`.
