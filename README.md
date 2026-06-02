# mailing-bot

Headless Playwright service for logging into an inventory system, downloading a sale receipt PDF, and emailing it to a customer. The service is designed to run on Railway through FastAPI and Nixpacks.

## What is included

- `app.py` - FastAPI app with a `/receipts/send` endpoint.
- `requirements.txt` - Python dependencies for FastAPI, Uvicorn, Playwright, and email validation.
- `Nixpacks.toml` - Railway/Nixpacks config that installs Python, Chromium dependencies, Playwright, and starts Uvicorn.
- `.gitignore` - Keeps `.env`, `auth.json`, and generated receipt downloads out of Git.

## Required environment variables

Configure these in Railway before sending receipt requests:

```bash
TARGET_BASE_URL="https://inventory.example.com"
TARGET_USERNAME="your-inventory-username"
TARGET_PASSWORD="your-inventory-password"

IMAP_HOST="imap.example.com"
IMAP_PORT="993"
IMAP_USERNAME="inbox@example.com"
IMAP_PASSWORD="your-email-app-password"
IMAP_MAILBOX="INBOX"
TWO_FACTOR_EMAIL_FROM="no-reply@inventory.example.com"
TWO_FACTOR_EMAIL_SUBJECT_CONTAINS="verification"
TWO_FACTOR_TIMEOUT_SECONDS="180"
TWO_FACTOR_POLL_SECONDS="5"
TWO_FACTOR_CODE_REGEX="\\b(\\d{6})\\b"

SMTP_HOST="smtp.example.com"
SMTP_PORT="587"
SMTP_USERNAME="sender@example.com"
SMTP_PASSWORD="your-smtp-app-password"
SMTP_FROM_EMAIL="sender@example.com"
SMTP_USE_TLS="true"
SMTP_USE_SSL="false"

AUTH_STATE_PATH="auth.json"
LOG_LEVEL="INFO"
```

Update the selector placeholders in `app.py` or set them as Railway variables:

```bash
SELECTOR_LOGIN_FORM=""
SELECTOR_USERNAME_INPUT=""
SELECTOR_PASSWORD_INPUT=""
SELECTOR_LOGIN_BUTTON=""
SELECTOR_2FA_INPUT=""
SELECTOR_2FA_SUBMIT_BUTTON=""
SELECTOR_LOGGED_IN_MARKER=""
SELECTOR_SALE_SEARCH_INPUT=""
SELECTOR_SALE_SEARCH_BUTTON=""
SELECTOR_SALE_RESULT_ROW=""
SELECTOR_RECEIPT_DOWNLOAD_BUTTON=""
```

## Local setup

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
playwright install chromium
uvicorn app:app --host 0.0.0.0 --port 8000
```

Send a receipt request:

```bash
curl -X POST "http://localhost:8000/receipts/send" \
  -H "Content-Type: application/json" \
  -d '{"sale_id":"SALE-12345","customer_email":"customer@example.com"}'
```

The first successful login saves Playwright cookies/tokens to `auth.json`. Later requests reuse that storage state until the target system expires the session. If the login page is detected, the service runs the full username/password/2FA flow again and refreshes `auth.json`.

## Finding selectors with `playwright codegen`

Run codegen locally against the inventory site:

```bash
source .venv/bin/activate
playwright codegen https://inventory.example.com
```

In the browser that opens:

1. Log in manually.
2. Click the username field, password field, login button, 2FA field, and 2FA submit button.
3. After landing inside the app, click a stable element that proves login success, such as a dashboard heading or account menu. Use that as `SELECTOR_LOGGED_IN_MARKER`.
4. Search for a sale and click through the receipt download flow.
5. Copy the generated locator selectors from the Playwright Inspector and paste them into `app.py` or Railway environment variables.

This service passes each selector to `page.locator(...)`, so use selector strings rather than raw Python calls. Prefer stable selectors in this order when available:

1. Playwright selector engines, such as `role=button[name="Download receipt"]` or `text=Download receipt`.
2. Semantic attributes, such as `[data-testid="receipt-download"]`.
3. IDs or stable names, such as `#sale-search`.
4. CSS paths only when no stable attributes exist.

If codegen outputs Python locator calls, convert them to selector strings for this service. For example:

- `page.get_by_label("Username")` is a hint to find a stable input selector, such as `input[name="username"]` or `[aria-label="Username"]`.
- `page.locator("[data-testid='sale-search']")` becomes `[data-testid='sale-search']`.

After deploying to Railway, call `/health` to verify the API is running, then call `/receipts/send` with a real `sale_id` and `customer_email`.
