import json
import os
import re
import sqlite3
import uuid
from pathlib import Path
from typing import Any
from urllib import parse, request
from urllib.parse import urlsplit
from src.marketing.ai_generator import generate_ai_draft

DEFAULT_DB_PATH = Path(__file__).resolve().parents[2] / "data" / "marketing.db"
DEFAULT_FACEBOOK_GRAPH_API = "https://graph.facebook.com/v26.0"
PUBLISH_IN_PROGRESS = "FACEBOOK_PUBLISH_IN_PROGRESS"


class FacebookConfigurationError(RuntimeError):
    pass


class FacebookPublishError(RuntimeError):
    pass


def _clear_facebook_publish_claim(post_id: str) -> None:
    with _connect() as connection:
        connection.execute(
            """
            UPDATE posts SET publication_error = NULL
            WHERE post_id = ? AND publication_error = ?
            """,
            (post_id, PUBLISH_IN_PROGRESS),
        )


def _connect() -> sqlite3.Connection:
    db_path = Path(os.getenv("MARKETING_DB_PATH") or DEFAULT_DB_PATH)
    db_path.parent.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(db_path)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA foreign_keys = ON")
    connection.executescript(
        """
        CREATE TABLE IF NOT EXISTS campaigns (
            campaign_id TEXT PRIMARY KEY,
            name TEXT NOT NULL,
            objective TEXT NOT NULL,
            audience TEXT NOT NULL,
            offer TEXT NOT NULL,
            call_to_action TEXT NOT NULL,
            created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
        );

        CREATE TABLE IF NOT EXISTS posts (
            post_id TEXT PRIMARY KEY,
            campaign_id TEXT NOT NULL REFERENCES campaigns(campaign_id),
            platform TEXT NOT NULL CHECK(platform IN ('linkedin', 'facebook')),
            content TEXT NOT NULL,
            generation_source TEXT NOT NULL DEFAULT 'template'
                CHECK(generation_source IN ('gemini', 'template')),
            status TEXT NOT NULL CHECK(
                status IN ('PENDING_APPROVAL', 'APPROVED', 'REJECTED')
            ) DEFAULT 'PENDING_APPROVAL',
            external_post_id TEXT,
            published_at TEXT,
            publication_error TEXT,
            created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
            reviewed_at TEXT
        );
        """
    )
    columns = {
        row["name"]
        for row in connection.execute("PRAGMA table_info(posts)").fetchall()
    }

    if "generation_source" not in columns:
        connection.execute(
            """
            ALTER TABLE posts
            ADD COLUMN generation_source TEXT NOT NULL DEFAULT 'template'
            """
        )
    for column in ("external_post_id", "published_at", "publication_error"):
        if column not in columns:
            connection.execute(f"ALTER TABLE posts ADD COLUMN {column} TEXT")

    connection.commit()
    return connection


def _dict(row: sqlite3.Row | None) -> dict[str, Any] | None:
    return dict(row) if row is not None else None


def create_campaign(
    name: str,
    objective: str,
    audience: str,
    offer: str = "",
    call_to_action: str = "Contact us to learn more",
) -> dict[str, Any]:
    values = [name.strip(), objective.strip(), audience.strip()]
    if not all(values):
        raise ValueError("Name, objective, and audience are required.")

    campaign_id = f"CMP-{uuid.uuid4().hex[:10].upper()}"
    with _connect() as connection:
        connection.execute(
            """
            INSERT INTO campaigns
                (campaign_id, name, objective, audience, offer, call_to_action)
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            (
                campaign_id,
                values[0],
                values[1],
                values[2],
                offer.strip(),
                call_to_action.strip() or "Contact us to learn more",
            ),
        )
        row = connection.execute(
            "SELECT * FROM campaigns WHERE campaign_id = ?", (campaign_id,)
        ).fetchone()
    return _dict(row) or {}


def get_campaign(campaign_id: str) -> dict[str, Any] | None:
    with _connect() as connection:
        row = connection.execute(
            "SELECT * FROM campaigns WHERE campaign_id = ?", (campaign_id,)
        ).fetchone()
    return _dict(row)


def list_campaigns() -> list[dict[str, Any]]:
    with _connect() as connection:
        rows = connection.execute(
            "SELECT * FROM campaigns ORDER BY created_at DESC, campaign_id DESC"
        ).fetchall()
    return [dict(row) for row in rows]


def _draft_content(campaign: dict[str, Any], platform: str) -> str:
    offer_line = f"\n\nWhat we're offering: {campaign['offer']}" if campaign["offer"] else ""
    common = (
        f"{campaign['objective']}\n\n"
        f"Created for: {campaign['audience']}.{offer_line}\n\n"
        f"{campaign['call_to_action']}\n\n"
        "#SlimesHustlers #BusinessGrowth #AI"
    )
    if platform == "linkedin":
        return (
            f"Looking for smarter ways to run your business?\n\n"
            f"At Slimes.Hustlers AI Solutions, our focus is: "
            f"{campaign['objective'].rstrip('.')}.\n\n"
            f"We're speaking to {campaign['audience']} who want practical "
            f"technology and automation ideas.\n\n"
            f"{campaign['offer'] + chr(10) + chr(10) if campaign['offer'] else ''}"
            f"{campaign['call_to_action']}\n\n"
            "#SlimesHustlers #BusinessAutomation #ArtificialIntelligence"
        )
    return (
        f"Business owners and teams — let's make work smarter! 🚀\n\n"
        f"{campaign['objective']}\n\n"
        f"Made with {campaign['audience']} in mind."
        f"{offer_line}\n\n"
        f"{campaign['call_to_action']}\n\n"
        "#SlimesHustlers #SmartBusiness #AI"
    )


def generate_drafts(campaign_id: str) -> list[dict[str, Any]]:
    campaign = get_campaign(campaign_id)
    if campaign is None:
        raise LookupError("Campaign not found.")

    with _connect() as connection:
        existing = connection.execute(
            "SELECT * FROM posts WHERE campaign_id = ? ORDER BY platform",
            (campaign_id,),
        ).fetchall()
        if existing:
            return [dict(row) for row in existing]

        created = []
        for platform in ("linkedin", "facebook"):
            post_id = f"PST-{uuid.uuid4().hex[:10].upper()}"

            content = generate_ai_draft(campaign, platform)
            source = "gemini" if content else "template"

            if not content:
                content = _draft_content(campaign, platform)

            connection.execute(
                """
                INSERT INTO posts (
                    post_id,
                    campaign_id,
                    platform,
                    content,
                    generation_source
                )
                VALUES (?, ?, ?, ?, ?)
                """,
                (
                    post_id,
                    campaign_id,
                    platform,
                    content,
                    source,
                ),
            )

            created.append(
                connection.execute(
                    "SELECT * FROM posts WHERE post_id = ?",
                    (post_id,),
                ).fetchone()
            )
        return [dict(row) for row in created]    


def list_posts(campaign_id: str | None = None) -> list[dict[str, Any]]:
    with _connect() as connection:
        if campaign_id:
            rows = connection.execute(
                "SELECT * FROM posts WHERE campaign_id = ? "
                "ORDER BY created_at DESC, platform",
                (campaign_id,),
            ).fetchall()
        else:
            rows = connection.execute(
                "SELECT * FROM posts ORDER BY created_at DESC, post_id DESC"
            ).fetchall()
    return [dict(row) for row in rows]


def _review_post(post_id: str, new_status: str) -> dict[str, Any]:
    with _connect() as connection:
        row = connection.execute(
            "SELECT * FROM posts WHERE post_id = ?", (post_id,)
        ).fetchone()
        if row is None:
            raise LookupError("Post not found.")
        if row["status"] != "PENDING_APPROVAL":
            raise ValueError("Only posts pending approval can be reviewed.")

        connection.execute(
            """
            UPDATE posts SET status = ?, reviewed_at = CURRENT_TIMESTAMP
            WHERE post_id = ? AND status = 'PENDING_APPROVAL'
            """,
            (new_status, post_id),
        )
        updated = connection.execute(
            "SELECT * FROM posts WHERE post_id = ?", (post_id,)
        ).fetchone()
    return _dict(updated) or {}


def approve_post(post_id: str) -> dict[str, Any]:
    return _review_post(post_id, "APPROVED")


def reject_post(post_id: str) -> dict[str, Any]:
    return _review_post(post_id, "REJECTED")


def _facebook_api_endpoint(api_base: str, page_id: str) -> str:
    parsed = urlsplit(api_base)
    if (
        parsed.scheme != "https"
        or parsed.hostname != "graph.facebook.com"
        or parsed.port not in (None, 443)
        or parsed.username is not None
        or parsed.password is not None
        or parsed.query
        or parsed.fragment
        or not re.fullmatch(r"/v[0-9]+(?:\.[0-9]+)?", parsed.path)
    ):
        raise FacebookConfigurationError("FACEBOOK_GRAPH_API_BASE is invalid.")
    return f"{api_base.rstrip('/')}/{parse.quote(page_id, safe='')}/feed"


def _facebook_publishing_config() -> tuple[str, str, str, int]:
    page_id = os.getenv("FACEBOOK_PAGE_ID", "").strip()
    access_token = os.getenv("FACEBOOK_PAGE_ACCESS_TOKEN", "").strip()
    api_base = os.getenv("FACEBOOK_GRAPH_API_BASE", DEFAULT_FACEBOOK_GRAPH_API)
    try:
        timeout_ms = int(os.getenv("FACEBOOK_PUBLISH_TIMEOUT_MS", "20000") or "20000")
    except ValueError as exc:
        raise FacebookConfigurationError("FACEBOOK_PUBLISH_TIMEOUT_MS must be an integer.") from exc
    if not page_id or not access_token:
        raise FacebookConfigurationError("Facebook publishing is not configured.")
    if not page_id.isdecimal():
        raise FacebookConfigurationError("FACEBOOK_PAGE_ID must be numeric.")
    if not 1_000 <= timeout_ms <= 60_000:
        raise FacebookConfigurationError(
            "FACEBOOK_PUBLISH_TIMEOUT_MS must be between 1000 and 60000."
        )
    return page_id, access_token, _facebook_api_endpoint(api_base, page_id), timeout_ms


def publish_facebook_post(post_id: str) -> dict[str, Any]:
    with _connect() as connection:
        connection.execute("BEGIN IMMEDIATE")
        row = connection.execute(
            "SELECT * FROM posts WHERE post_id = ?", (post_id,)
        ).fetchone()
        if row is None:
            raise LookupError("Post not found.")
        if row["platform"] != "facebook":
            raise ValueError("Only Facebook posts can be published through this endpoint.")
        if row["status"] != "APPROVED":
            raise ValueError("Only approved Facebook posts can be published.")
        if row["external_post_id"]:
            raise ValueError("This post has already been published to Facebook.")
        if row["publication_error"] == PUBLISH_IN_PROGRESS:
            raise ValueError("This post is already being published to Facebook.")

    _page_id, access_token, endpoint, timeout_ms = _facebook_publishing_config()

    with _connect() as connection:
        connection.execute("BEGIN IMMEDIATE")
        current = connection.execute(
            """
            SELECT * FROM posts WHERE post_id = ? AND status = 'APPROVED'
              AND platform = 'facebook' AND external_post_id IS NULL
              AND publication_error IS NOT ?
            """,
            (post_id, PUBLISH_IN_PROGRESS),
        ).fetchone()
        if current is None:
            raise ValueError("This post is no longer eligible for Facebook publishing.")
        connection.execute(
            """
            UPDATE posts SET publication_error = ?
            WHERE post_id = ? AND status = 'APPROVED'
              AND platform = 'facebook' AND external_post_id IS NULL
              AND publication_error IS NOT ?
            """,
            (PUBLISH_IN_PROGRESS, post_id, PUBLISH_IN_PROGRESS),
        )
        connection.commit()

    try:
        payload = json.dumps({"message": row["content"]}).encode("utf-8")
        http_request = request.Request(
            endpoint,
            data=payload,
            headers={
                "Authorization": f"Bearer {access_token}",
                "Content-Type": "application/json",
            },
            method="POST",
        )
        with request.urlopen(http_request, timeout=timeout_ms / 1000) as response:
            status = response.getcode()
            if status is not None and not 200 <= status < 300:
                raise FacebookPublishError("Facebook rejected the post.")
            result = json.loads(response.read().decode("utf-8"))
    except FacebookPublishError:
        _clear_facebook_publish_claim(post_id)
        raise
    except (request.URLError, TimeoutError, OSError, ValueError, json.JSONDecodeError) as exc:
        _clear_facebook_publish_claim(post_id)
        raise FacebookPublishError("Facebook rejected or could not accept the post.") from exc

    if not isinstance(result, dict):
        _clear_facebook_publish_claim(post_id)
        raise FacebookPublishError("Facebook returned an invalid publish response.")
    external_post_id = str(result.get("id", "")).strip()
    if not re.fullmatch(r"[A-Za-z0-9_]+", external_post_id):
        _clear_facebook_publish_claim(post_id)
        raise FacebookPublishError("Facebook did not return a valid published post identifier.")

    with _connect() as connection:
        connection.execute("BEGIN IMMEDIATE")
        updated = connection.execute(
            """
            UPDATE posts
            SET external_post_id = ?, published_at = CURRENT_TIMESTAMP,
                publication_error = NULL
            WHERE post_id = ? AND status = 'APPROVED'
              AND platform = 'facebook' AND external_post_id IS NULL
              AND publication_error = ?
            """,
            (external_post_id, post_id, PUBLISH_IN_PROGRESS),
        )
        if updated.rowcount != 1:
            raise FacebookPublishError("Facebook publishing state changed unexpectedly.")
        connection.commit()
        result_row = connection.execute(
            "SELECT * FROM posts WHERE post_id = ?", (post_id,)
        ).fetchone()
    return _dict(result_row) or {}
