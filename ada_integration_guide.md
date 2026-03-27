# End-to-End ADA.cx Integration Guide

This guide provides the exact steps to connect your local FastAPI server to the **ADA.cx** chatbot platform using your ngrok tunnel.

> [!IMPORTANT]
> **Base URL**: `https://<your-ngrok-url>.ngrok-free.app`
> All endpoints below must be prefixed with this URL.

---

## 1. Google Apps Script Configuration

Before configuring ADA, ensure your Google Sheet can talk back to your local server.

1. Open your Google Sheet.
2. Go to **Extensions** -> **Apps Script**.
3. In `apps_script.js`, locate **Line 19**:
   ```javascript
   const WEBHOOK_URL = "https://<your-ngrok-url>.ngrok-free.app/webhook";
   ```
4. Click **Save** and ensures you have the `onEdit` and `onChange` triggers set up as described in the README.

---

## 2. ADA Action 1: Trigger Withdrawal Request

This action initializes the process and writes a row to Google Sheets.

### **Endpoint Tab**

- **Method**: `POST`
- **URL**: `https://<your-ngrok-url>.ngrok-free.app/ada/v1/request_review`
- **This API uses**: `JSON`

### **Headers Tab**

- **Parameter**: `Content-Type`
- **Value**: `application/json`

### **Body Tab**

- **Content**:
  ```json
  {
    "player_id": "[player_id]",
    "player_name": "[player_name]",
    "channel": "Chat"
  }
  ```

  *(Ensure `[player_id]` and `[player_name]` are mapped to your ADA variables.)*

### **Response Handling**

ADA needs to capture the `row_number` from our response to use in the next step.

- Map the JSON key `row_number` to a new ADA variable named `meta_row_number`.

---

## 3. ADA Action 2: Poll for Decision

This action retrieves the human decision from the specific row in Google Sheets.

### **Endpoint Tab**

- **Method**: `GET`
- **URL**: `https://<your-ngrok-url>.ngrok-free.app/ada/v1/status/[player_id]/[meta_row_number]`
  - *Note: Replace `[player_id]` and `[meta_row_number]` with the variables in ADA.*

### **Response Handling**

- Map the JSON key `decision` to an ADA variable (e.g., `withdrawal_decision`).
- Map the JSON key `notes` to an ADA variable (e.g., `withdrawal_notes`).

**How these values are retrieved:**

1. **Human Review**: A human payment agent visits the Google Sheet and reviews the request.
2. **Decision Entry**: The agent enters a decision ("Yes" or "No") in **Column I** and adds mandatory notes in **Column J**.
3. **Automated Webhook**: Once both columns are filled, the Google Apps Script `onEdit` trigger automatically sends a webhook to your local server.
4. **State Sync**: The backend receives the webhook, finishes the AI agent's logic, and updates the status for that specific row.
5. **ADA Final Poll**: The next time ADA polls this action, the `decision` will change from `pending` to the human's actual decision, and the `notes` will be populated.

---

## 4. Chat Workflow Logic

To make the automation feel seamless for the player:

1. **Call Action 1**: When the player requests a withdrawal.
2. **Wait Step**: Add a "Wait" block or a small delay.
3. **Call Action 2**: Periodically poll for the status.
4. **Condition**:
   - If `withdrawal_decision` is `pending`, loop back to a wait step.
   - If `withdrawal_decision` is `Yes` or `No`, show the `withdrawal_notes` to the player.

---

## 5. Verification

To ensure everything is working correctly:

1. Run `python main.py` on your local machine.
2. Trigger the flow via the ADA chatbot.
3. Observe the server logs—you should see:
   - `🤖 ADA Request via Chatbot: player=...`
   - `✅ Agent waiting for human review at row X`
4. Edit the Google Sheet (Columns I & J) and verify the chat updates.
