import base64
import os
import re
from dataclasses import dataclass
from typing import Optional

import httpx


class GitHubReceiptError(RuntimeError):
    """Raised when a receipt cannot be pushed to GitHub."""


@dataclass(frozen=True)
class GitHubSettings:
    token: str
    owner: str
    repo: str
    branch: str
    receipts_dir: str
    webhook_secret: Optional[str]


def load_github_settings() -> GitHubSettings:
    missing = [
        name
        for name in ("GITHUB_TOKEN", "GITHUB_OWNER", "GITHUB_REPO")
        if not os.getenv(name)
    ]
    if missing:
        raise GitHubReceiptError(
            f"Missing required environment variables: {', '.join(missing)}"
        )

    return GitHubSettings(
        token=os.environ["GITHUB_TOKEN"],
        owner=os.environ["GITHUB_OWNER"],
        repo=os.environ["GITHUB_REPO"],
        branch=os.getenv("GITHUB_BRANCH", "main"),
        receipts_dir=os.getenv("GITHUB_RECEIPTS_DIR", "receipts"),
        webhook_secret=os.getenv("WEBHOOK_SECRET"),
    )


def sanitize_sale_id(sale_id: str) -> str:
    sanitized = re.sub(r"[^\w\-.]", "_", sale_id.strip())
    if not sanitized:
        raise GitHubReceiptError("sale_id must contain at least one valid character")
    return sanitized


def build_receipt_path(receipts_dir: str, sale_id: str, filename: Optional[str] = None) -> str:
    safe_sale_id = sanitize_sale_id(sale_id)
    file_name = filename or f"{safe_sale_id}.pdf"
    if not file_name.lower().endswith(".pdf"):
        file_name = f"{file_name}.pdf"

    normalized_dir = receipts_dir.strip("/")
    if normalized_dir:
        return f"{normalized_dir}/{file_name}"
    return file_name


async def push_receipt_to_github(
    settings: GitHubSettings,
    *,
    sale_id: str,
    content: bytes,
    filename: Optional[str] = None,
) -> dict[str, str]:
    if not content:
        raise GitHubReceiptError("Receipt file is empty")

    path = build_receipt_path(settings.receipts_dir, sale_id, filename)
    safe_sale_id = sanitize_sale_id(sale_id)
    api_url = f"https://api.github.com/repos/{settings.owner}/{settings.repo}/contents/{path}"
    headers = {
        "Authorization": f"Bearer {settings.token}",
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
    }
    payload: dict[str, str] = {
        "message": f"Add receipt for {safe_sale_id}",
        "content": base64.b64encode(content).decode("ascii"),
        "branch": settings.branch,
    }

    async with httpx.AsyncClient(timeout=30.0) as client:
        existing = await client.get(api_url, headers=headers, params={"ref": settings.branch})
        if existing.status_code == 200:
            payload["sha"] = existing.json()["sha"]
            payload["message"] = f"Update receipt for {safe_sale_id}"
        elif existing.status_code not in {404, 403}:
            existing.raise_for_status()

        response = await client.put(api_url, headers=headers, json=payload)
        if response.status_code >= 400:
            detail = response.text
            try:
                detail = response.json().get("message", detail)
            except ValueError:
                pass
            raise GitHubReceiptError(f"GitHub API error ({response.status_code}): {detail}")

        data = response.json()

    return {
        "github_path": path,
        "commit_sha": data["commit"]["sha"],
        "html_url": data["content"]["html_url"],
    }
