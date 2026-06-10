import asyncio
import imaplib
import logging
import os
import re
import shutil
import tempfile
import time
import uuid
from contextlib import suppress
from dataclasses import dataclass
from email import message_from_bytes
from email.header import decode_header
from email.message import EmailMessage
from email.mime.application import MIMEApplication
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from pathlib import Path
from smtplib import SMTP, SMTP_SSL
from typing import Optional

from fastapi import FastAPI, File, Form, Header, HTTPException, UploadFile
from pydantic import BaseModel, EmailStr, Field

from github_receipts import (
    GitHubReceiptError,
    load_github_settings,
    push_receipt_to_github,
)
from playwright.async_api import (
    Browser,
    BrowserContext,
    Error as PlaywrightError,
    Page,
    TimeoutError as PlaywrightTimeoutError,
    async_playwright,
)


logging.basicConfig(
    level=os.getenv("LOG_LEVEL", "INFO").upper(),
    format="%(asctime)s %(levelname)s [%(name)s] %(message)s",
)
LOGGER = logging.getLogger("receipt_automation")


class AutomationError(RuntimeError):
    """Raised for expected automation failures that should be returned as 4xx/5xx."""


@dataclass(frozen=True)
class InventorySelectors:
    """Update these selectors with values from `playwright codegen` before deployment."""

    login_form: str = os.getenv("SELECTOR_LOGIN_FORM", "TODO_LOGIN_FORM_SELECTOR")
    username_input: str = os.getenv("SELECTOR_USERNAME_INPUT", "TODO_USERNAME_INPUT_SELECTOR")
    password_input: str = os.getenv("SELECTOR_PASSWORD_INPUT", "TODO_PASSWORD_INPUT_SELECTOR")
    login_button: str = os.getenv("SELECTOR_LOGIN_BUTTON", "TODO_LOGIN_BUTTON_SELECTOR")
    two_factor_input: str = os.getenv("SELECTOR_2FA_INPUT", "TODO_2FA_INPUT_SELECTOR")
    two_factor_submit_button: str = os.getenv(
        "SELECTOR_2FA_SUBMIT_BUTTON", "TODO_2FA_SUBMIT_BUTTON_SELECTOR"
    )
    logged_in_marker: str = os.getenv("SELECTOR_LOGGED_IN_MARKER", "TODO_LOGGED_IN_MARKER_SELECTOR")
    sale_search_input: str = os.getenv("SELECTOR_SALE_SEARCH_INPUT", "TODO_SALE_SEARCH_INPUT_SELECTOR")
    sale_search_button: str = os.getenv("SELECTOR_SALE_SEARCH_BUTTON", "TODO_SALE_SEARCH_BUTTON_SELECTOR")
    sale_result_row: str = os.getenv("SELECTOR_SALE_RESULT_ROW", "TODO_SALE_RESULT_ROW_SELECTOR")
    receipt_download_button: str = os.getenv(
        "SELECTOR_RECEIPT_DOWNLOAD_BUTTON", "TODO_RECEIPT_DOWNLOAD_BUTTON_SELECTOR"
    )


@dataclass(frozen=True)
class Settings:
    target_base_url: str
    target_username: str
    target_password: str

    auth_state_path: Path
    download_root: Path
    browser_timeout_ms: int
    post_login_timeout_ms: int

    imap_host: str
    imap_port: int
    imap_username: str
    imap_password: str
    imap_mailbox: str
    two_factor_from: str
    two_factor_subject_contains: str
    two_factor_timeout_seconds: int
    two_factor_poll_seconds: int
    two_factor_code_regex: str

    smtp_host: str
    smtp_port: int
    smtp_username: str
    smtp_password: str
    smtp_from_email: str
    smtp_use_ssl: bool
    smtp_use_tls: bool


class ReceiptRequest(BaseModel):
    sale_id: str = Field(..., min_length=1, examples=["SALE-12345"])
    customer_email: EmailStr = Field(..., examples=["customer@example.com"])


class ReceiptResponse(BaseModel):
    status: str
    sale_id: str
    customer_email: EmailStr


class ReceiptWebhookResponse(BaseModel):
    status: str
    sale_id: str
    github_path: str
    commit_sha: str
    html_url: str


app = FastAPI(title="Inventory Receipt Automation", version="1.0.0")
_automation_lock = asyncio.Lock()


def load_settings() -> Settings:
    missing = [
        name
        for name in (
            "TARGET_BASE_URL",
            "TARGET_USERNAME",
            "TARGET_PASSWORD",
            "IMAP_HOST",
            "IMAP_USERNAME",
            "IMAP_PASSWORD",
            "TWO_FACTOR_EMAIL_FROM",
            "SMTP_HOST",
            "SMTP_USERNAME",
            "SMTP_PASSWORD",
        )
        if not os.getenv(name)
    ]
    if missing:
        raise AutomationError(f"Missing required environment variables: {', '.join(missing)}")
    return Settings(
        target_base_url=os.environ["TARGET_BASE_URL"],
        target_username=os.environ["TARGET_USERNAME"],
        target_password=os.environ["TARGET_PASSWORD"],
        auth_state_path=Path(os.getenv("AUTH_STATE_PATH", "auth.json")),
        download_root=Path(os.getenv("DOWNLOAD_ROOT", tempfile.gettempdir())) / "receipt-downloads",
        browser_timeout_ms=int(os.getenv("BROWSER_TIMEOUT_MS", "30000")),
        post_login_timeout_ms=int(os.getenv("POST_LOGIN_TIMEOUT_MS", "60000")),
        imap_host=os.environ["IMAP_HOST"],
        imap_port=int(os.getenv("IMAP_PORT", "993")),
        imap_username=os.environ["IMAP_USERNAME"],
        imap_password=os.environ["IMAP_PASSWORD"],
        imap_mailbox=os.getenv("IMAP_MAILBOX", "INBOX"),
        two_factor_from=os.environ["TWO_FACTOR_EMAIL_FROM"],
        two_factor_subject_contains=os.getenv("TWO_FACTOR_EMAIL_SUBJECT_CONTAINS", ""),
        two_factor_timeout_seconds=int(os.getenv("TWO_FACTOR_TIMEOUT_SECONDS", "180")),
        two_factor_poll_seconds=int(os.getenv("TWO_FACTOR_POLL_SECONDS", "5")),
        two_factor_code_regex=os.getenv("TWO_FACTOR_CODE_REGEX", r"\b(\d{6})\b"),
        smtp_host=os.environ["SMTP_HOST"],
        smtp_port=int(os.getenv("SMTP_PORT", "587")),
        smtp_username=os.environ["SMTP_USERNAME"],
        smtp_password=os.environ["SMTP_PASSWORD"],
        smtp_from_email=os.getenv("SMTP_FROM_EMAIL", os.environ["SMTP_USERNAME"]),
        smtp_use_ssl=os.getenv("SMTP_USE_SSL", "false").lower() == "true",
        smtp_use_tls=os.getenv("SMTP_USE_TLS", "true").lower() == "true",
    )


def _selector_is_placeholder(selector: str) -> bool:
    return selector.startswith("TODO_") or not selector.strip()


def validate_selectors(selectors: InventorySelectors) -> None:
    placeholders = [
        name
        for name, selector in selectors.__dict__.items()
        if _selector_is_placeholder(selector)
    ]
    if placeholders:
        raise AutomationError(
            "Update selector placeholders before running automation: " + ", ".join(placeholders)
        )


async def is_visible(page: Page, selector: str, timeout_ms: int = 2500) -> bool:
    try:
        await page.locator(selector).first.wait_for(state="visible", timeout=timeout_ms)
        return True
    except PlaywrightTimeoutError:
        return False


def decode_header_value(raw_value: Optional[str]) -> str:
    if not raw_value:
        return ""
    decoded_parts = decode_header(raw_value)
    parts: list[str] = []
    for value, encoding in decoded_parts:
        if isinstance(value, bytes):
            parts.append(value.decode(encoding or "utf-8", errors="replace"))
        else:
            parts.append(value)
    return "".join(parts)


def message_text(message: EmailMessage) -> str:
    if message.is_multipart():
        chunks: list[str] = []
        for part in message.walk():
            if part.get_content_maintype() == "multipart":
                continue
            if part.get_content_disposition() == "attachment":
                continue
            if part.get_content_type() not in {"text/plain", "text/html"}:
                continue
            payload = part.get_payload(decode=True)
            if payload:
                charset = part.get_content_charset() or "utf-8"
                chunks.append(payload.decode(charset, errors="replace"))
        return "\n".join(chunks)

    payload = message.get_payload(decode=True)
    if not payload:
        return ""
    return payload.decode(message.get_content_charset() or "utf-8", errors="replace")


def build_imap_search(settings: Settings) -> str:
    criteria = ["UNSEEN", f'FROM "{settings.two_factor_from}"']
    if settings.two_factor_subject_contains:
        criteria.append(f'SUBJECT "{settings.two_factor_subject_contains}"')
    return "(" + " ".join(criteria) + ")"


def poll_two_factor_code(settings: Settings) -> str:
    """Poll the configured mailbox for the newest unread 2FA email and extract a 6-digit code."""

    deadline = time.monotonic() + settings.two_factor_timeout_seconds
    code_pattern = re.compile(settings.two_factor_code_regex)
    last_error: Optional[Exception] = None

    while time.monotonic() < deadline:
        try:
            with imaplib.IMAP4_SSL(settings.imap_host, settings.imap_port) as mailbox:
                mailbox.login(settings.imap_username, settings.imap_password)
                mailbox.select(settings.imap_mailbox)
                status, data = mailbox.search(None, build_imap_search(settings))
                if status != "OK":
                    raise AutomationError(f"IMAP search failed with status {status}")

                message_ids = data[0].split()
                LOGGER.info("Found %s unread candidate 2FA emails", len(message_ids))
                for message_id in reversed(message_ids):
                    status, fetched = mailbox.fetch(message_id, "(BODY.PEEK[])")
                    if status != "OK" or not fetched:
                        LOGGER.warning("Could not fetch 2FA email id %s", message_id.decode())
                        continue

                    raw_message = fetched[0][1]
                    message = message_from_bytes(raw_message)
                    subject = decode_header_value(message.get("Subject"))
                    body = message_text(message)
                    match = code_pattern.search(f"{subject}\n{body}")
                    if not match:
                        LOGGER.debug("Unread 2FA email id %s did not contain a matching code", message_id.decode())
                        continue

                    code = match.group(1)
                    mailbox.store(message_id, "+FLAGS", "\\Seen")
                    LOGGER.info("Extracted 2FA code from email id %s", message_id.decode())
                    return code
        except Exception as exc:  # noqa: BLE001 - log and continue until timeout.
            last_error = exc
            LOGGER.warning("2FA inbox polling attempt failed: %s", exc)

        remaining = max(0, deadline - time.monotonic())
        if remaining:
            time.sleep(min(settings.two_factor_poll_seconds, remaining))

    detail = f"Timed out waiting for a 2FA email after {settings.two_factor_timeout_seconds} seconds"
    if last_error:
        detail = f"{detail}; last error: {last_error}"
    raise AutomationError(detail)


async def wait_for_two_factor_code(settings: Settings) -> str:
    return await asyncio.to_thread(poll_two_factor_code, settings)


async def maybe_click(page: Page, selector: str, timeout_ms: int) -> None:
    try:
        await page.locator(selector).first.click(timeout=timeout_ms)
    except PlaywrightTimeoutError as exc:
        raise AutomationError(f"Required selector was not visible/clickable: {selector}") from exc


async def ensure_authenticated(
    page: Page,
    context: BrowserContext,
    settings: Settings,
    selectors: InventorySelectors,
) -> None:
    await page.goto(settings.target_base_url, wait_until="domcontentloaded")
    if await is_visible(page, selectors.logged_in_marker):
        LOGGER.info("Existing session state is valid")
        return

    if not await is_visible(page, selectors.login_form):
        raise AutomationError("Could not confirm logged-in state or find the login form")

    LOGGER.info("Existing session is missing or expired; starting login flow")
    try:
        await page.locator(selectors.username_input).first.fill(settings.target_username)
        await page.locator(selectors.password_input).first.fill(settings.target_password)
        await page.locator(selectors.login_button).first.click()
    except PlaywrightTimeoutError as exc:
        raise AutomationError("Login form selectors were not found or interactable") from exc

    if await is_visible(page, selectors.two_factor_input, timeout_ms=10000):
        code = await wait_for_two_factor_code(settings)
        LOGGER.info("Submitting 2FA code")
        await page.locator(selectors.two_factor_input).first.fill(code)
        await maybe_click(page, selectors.two_factor_submit_button, settings.browser_timeout_ms)
    else:
        LOGGER.info("2FA prompt was not displayed during this login attempt")

    try:
        await page.locator(selectors.logged_in_marker).first.wait_for(
            state="visible",
            timeout=settings.post_login_timeout_ms,
        )
    except PlaywrightTimeoutError as exc:
        raise AutomationError("Login did not complete; logged-in marker was not visible") from exc

    settings.auth_state_path.parent.mkdir(parents=True, exist_ok=True)
    await context.storage_state(path=str(settings.auth_state_path))
    LOGGER.info("Saved fresh Playwright storage state to %s", settings.auth_state_path)


async def fetch_receipt_pdf(
    page: Page,
    settings: Settings,
    selectors: InventorySelectors,
    sale_id: str,
) -> Path:
    settings.download_root.mkdir(parents=True, exist_ok=True)
    request_download_dir = settings.download_root / str(uuid.uuid4())
    request_download_dir.mkdir(parents=True, exist_ok=True)

    LOGGER.info("Searching for sale id %s", sale_id)
    try:
        await page.locator(selectors.sale_search_input).first.fill(sale_id)
        await page.locator(selectors.sale_search_button).first.click()
        await page.locator(selectors.sale_result_row).first.wait_for(
            state="visible",
            timeout=settings.browser_timeout_ms,
        )
    except PlaywrightTimeoutError as exc:
        shutil.rmtree(request_download_dir, ignore_errors=True)
        raise AutomationError(f"Sale search failed or sale was not found: {sale_id}") from exc

    LOGGER.info("Triggering receipt PDF download for sale id %s", sale_id)
    try:
        async with page.expect_download(timeout=settings.browser_timeout_ms) as download_info:
            await page.locator(selectors.receipt_download_button).first.click()
        download = await download_info.value
    except PlaywrightTimeoutError as exc:
        shutil.rmtree(request_download_dir, ignore_errors=True)
        raise AutomationError("Receipt download did not start before the timeout") from exc

    suggested_name = download.suggested_filename or f"receipt-{sale_id}.pdf"
    destination = request_download_dir / suggested_name
    await download.save_as(str(destination))
    LOGGER.info("Downloaded receipt to %s", destination)
    return destination


def send_receipt_email(settings: Settings, sale_id: str, customer_email: str, pdf_path: Path) -> None:
    LOGGER.info("Sending receipt %s to %s", pdf_path.name, customer_email)

    message = MIMEMultipart()
    message["From"] = settings.smtp_from_email
    message["To"] = customer_email
    message["Subject"] = f"Receipt for sale {sale_id}"
    message.attach(
        MIMEText(
            f"Hello,\n\nAttached is the receipt for sale {sale_id}.\n\nThank you.",
            "plain",
            "utf-8",
        )
    )

    with pdf_path.open("rb") as attachment_file:
        attachment = MIMEApplication(attachment_file.read(), _subtype="pdf")
    attachment.add_header("Content-Disposition", "attachment", filename=pdf_path.name)
    message.attach(attachment)

    smtp_class = SMTP_SSL if settings.smtp_use_ssl else SMTP
    with smtp_class(settings.smtp_host, settings.smtp_port, timeout=30) as smtp:
        if settings.smtp_use_tls and not settings.smtp_use_ssl:
            smtp.starttls()
        smtp.login(settings.smtp_username, settings.smtp_password)
        smtp.send_message(message)

    LOGGER.info("Receipt email sent to %s", customer_email)


async def build_context(browser: Browser, settings: Settings) -> BrowserContext:
    context_kwargs = {
        "accept_downloads": True,
    }
    if settings.auth_state_path.exists():
        context_kwargs["storage_state"] = str(settings.auth_state_path)
        LOGGER.info("Loading Playwright storage state from %s", settings.auth_state_path)
    return await browser.new_context(**context_kwargs)


async def process_receipt_request(request: ReceiptRequest) -> ReceiptResponse:
    settings = load_settings()
    selectors = InventorySelectors()
    validate_selectors(selectors)

    receipt_path: Optional[Path] = None
    async with async_playwright() as playwright:
        browser = await playwright.chromium.launch(
            headless=True,
            args=["--no-sandbox", "--disable-dev-shm-usage"],
        )
        context = await build_context(browser, settings)
        page = await context.new_page()
        page.set_default_timeout(settings.browser_timeout_ms)

        try:
            await ensure_authenticated(page, context, settings, selectors)
            try:
                receipt_path = await fetch_receipt_pdf(page, settings, selectors, request.sale_id)
            except AutomationError:
                if not await is_visible(page, selectors.login_form):
                    raise
                LOGGER.info("Session expired during receipt fetch; re-authenticating and retrying once")
                await ensure_authenticated(page, context, settings, selectors)
                receipt_path = await fetch_receipt_pdf(page, settings, selectors, request.sale_id)
            await asyncio.to_thread(
                send_receipt_email,
                settings,
                request.sale_id,
                request.customer_email,
                receipt_path,
            )
        finally:
            with suppress(Exception):
                await context.close()
            with suppress(Exception):
                await browser.close()
            if receipt_path:
                shutil.rmtree(receipt_path.parent, ignore_errors=True)
                LOGGER.info("Cleaned up downloaded receipt file %s", receipt_path)

    return ReceiptResponse(
        status="sent",
        sale_id=request.sale_id,
        customer_email=request.customer_email,
    )


@app.get("/health")
async def health() -> dict[str, str]:
    return {"status": "ok"}


@app.post("/webhooks/receipts", response_model=ReceiptWebhookResponse)
async def receive_receipt_webhook(
    sale_id: str = Form(..., min_length=1),
    file: UploadFile = File(...),
    x_webhook_secret: Optional[str] = Header(default=None, alias="X-Webhook-Secret"),
) -> ReceiptWebhookResponse:
    try:
        settings = load_github_settings()
    except GitHubReceiptError as exc:
        raise HTTPException(status_code=500, detail=str(exc)) from exc

    if settings.webhook_secret and x_webhook_secret != settings.webhook_secret:
        raise HTTPException(status_code=401, detail="Invalid webhook secret")

    if file.content_type not in {None, "application/pdf", "application/octet-stream"}:
        raise HTTPException(status_code=400, detail="Receipt file must be a PDF")

    content = await file.read()
    if not content.startswith(b"%PDF"):
        raise HTTPException(status_code=400, detail="Uploaded file is not a valid PDF")

    try:
        github_result = await push_receipt_to_github(
            settings,
            sale_id=sale_id,
            content=content,
            filename=file.filename,
        )
    except GitHubReceiptError as exc:
        LOGGER.exception("Failed to push receipt to GitHub for sale %s", sale_id)
        raise HTTPException(status_code=502, detail=str(exc)) from exc

    LOGGER.info(
        "Pushed receipt for sale %s to GitHub at %s",
        sale_id,
        github_result["github_path"],
    )
    return ReceiptWebhookResponse(
        status="pushed",
        sale_id=sale_id,
        github_path=github_result["github_path"],
        commit_sha=github_result["commit_sha"],
        html_url=github_result["html_url"],
    )


@app.post("/receipts/send", response_model=ReceiptResponse)
async def send_receipt(request: ReceiptRequest) -> ReceiptResponse:
    async with _automation_lock:
        try:
            return await process_receipt_request(request)
        except AutomationError as exc:
            LOGGER.exception("Receipt automation failed")
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        except PlaywrightError as exc:
            LOGGER.exception("Playwright failed while processing receipt")
            raise HTTPException(status_code=502, detail=f"Browser automation failed: {exc}") from exc
        except Exception as exc:  # noqa: BLE001 - return a controlled API error and keep logs detailed.
            LOGGER.exception("Unexpected receipt automation failure")
            raise HTTPException(status_code=500, detail="Unexpected receipt automation failure") from exc


if __name__ == "__main__":
    import uvicorn

    uvicorn.run("app:app", host="0.0.0.0", port=int(os.getenv("PORT", "8000")))
