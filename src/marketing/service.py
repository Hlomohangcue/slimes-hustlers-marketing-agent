import json
import logging
import os
import re
import sqlite3
import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any
from urllib import error, parse, request
from urllib.parse import urlsplit
from src.marketing.ai_generator import generate_ai_draft

DEFAULT_DB_PATH = Path(__file__).resolve().parents[2] / "data" / "marketing.db"
DEFAULT_FACEBOOK_GRAPH_API = "https://graph.facebook.com/v26.0"
PUBLISH_IN_PROGRESS = "FACEBOOK_PUBLISH_IN_PROGRESS"
PUBLISH_OUTCOME_UNCERTAIN = "FACEBOOK_PUBLISH_OUTCOME_UNCERTAIN"
MAX_FACEBOOK_ERROR_DETAIL = 240

logger = logging.getLogger(__name__)


class FacebookConfigurationError(RuntimeError):
    pass


class FacebookPublishError(RuntimeError):
    pass


class ReviewPersistenceError(RuntimeError):
    pass


class DraftEditConflictError(RuntimeError):
    pass


class DraftEditNotAllowedError(RuntimeError):
    pass


def _clear_facebook_publish_claim(post_id: str) -> None:
    with _managed_connection() as connection:
        connection.execute(
            """
            UPDATE posts SET publication_error = NULL
            WHERE post_id = ? AND publication_error = ?
            """,
            (post_id, PUBLISH_IN_PROGRESS),
        )


def _mark_facebook_publish_uncertain(
    post_id: str, external_post_id: str | None = None
) -> None:
    with _managed_connection() as connection:
        updated = connection.execute(
            """
            UPDATE posts
            SET publication_error = ?, external_post_id = COALESCE(?, external_post_id)
            WHERE post_id = ? AND publication_error = ?
            """,
            (PUBLISH_OUTCOME_UNCERTAIN, external_post_id, post_id, PUBLISH_IN_PROGRESS),
        )
        connection.commit()
        if updated.rowcount != 1:
            raise FacebookPublishError(
                "Facebook publishing outcome could not be safely recorded."
            )


def _sanitize_facebook_error_detail(value: Any, access_token: str) -> str:
    if not isinstance(value, str):
        return ""
    sanitized = " ".join(value.split())
    if access_token:
        sanitized = sanitized.replace(access_token, "[REDACTED]")
    sanitized = re.sub(r"(?i)bearer\s+[A-Za-z0-9._~+/=-]+", "Bearer [REDACTED]", sanitized)
    sanitized = re.sub(
        r"(?i)(access[_ -]?token|authorization)\s*[=:]\s*[^\s,;]+",
        r"\1=[REDACTED]",
        sanitized,
    )
    return sanitized[:MAX_FACEBOOK_ERROR_DETAIL]


def _sanitize_facebook_diagnostic(
    value: Any, access_token: str, limit: int
) -> str:
    if not isinstance(value, (int, str)) or isinstance(value, bool):
        return ""
    sanitized = _sanitize_facebook_error_detail(str(value), access_token)
    sanitized = re.sub(
        r"(?i)\b(app[_ -]?secret|appsecret|client[_ -]?secret|api[_ -]?key|"
        r"password|secret)\s*[:=]\s*(?:bearer\s+)?[^\s,;&]+",
        r"\1=[REDACTED]",
        sanitized,
    )
    sanitized = re.sub(r"\bEA[A-Za-z0-9]{20,}\b", "[REDACTED]", sanitized)
    for name in ("FACEBOOK_APP_SECRET", "META_APP_SECRET", "APP_SECRET"):
        secret = os.getenv(name, "")
        if secret.strip():
            sanitized = sanitized.replace(secret.strip(), "[REDACTED]")
    return sanitized[:limit]


def _facebook_http_error_detail(exc: error.HTTPError, access_token: str) -> str:
    status = exc.code
    diagnostics: dict[str, str] = {}
    try:
        body = exc.read(16_384)
        payload = json.loads(body.decode("utf-8"))
        meta_error = payload.get("error") if isinstance(payload, dict) else None
        if isinstance(meta_error, dict):
            for field, limit in (
                ("message", MAX_FACEBOOK_ERROR_DETAIL),
                ("type", 80),
                ("code", 32),
                ("error_subcode", 32),
                ("fbtrace_id", 96),
            ):
                value = meta_error.get(field)
                if isinstance(value, (int, str)) and not isinstance(value, bool):
                    sanitized = _sanitize_facebook_diagnostic(
                        value, access_token, limit
                    )
                    if sanitized:
                        diagnostics[field] = sanitized
    except (UnicodeDecodeError, json.JSONDecodeError, OSError, ValueError):
        pass
    finally:
        exc.close()

    parts = [f"Facebook rejected the post (HTTP {status}"]
    if "code" in diagnostics:
        parts[0] += f", code {diagnostics['code']}"
    if "error_subcode" in diagnostics:
        parts[0] += f", subcode {diagnostics['error_subcode']}"
    parts[0] += ")"
    for field in ("type", "message", "fbtrace_id"):
        if field in diagnostics:
            parts.append(f"{field}: {diagnostics[field]}")
    return ". ".join(parts)


def _connect() -> sqlite3.Connection:
    db_path = Path(os.getenv("MARKETING_DB_PATH") or DEFAULT_DB_PATH)
    db_path.parent.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(db_path)
    try:
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
                reviewed_at TEXT,
                version INTEGER NOT NULL DEFAULT 1
            );

            CREATE TABLE IF NOT EXISTS post_review_events (
                review_event_id INTEGER PRIMARY KEY AUTOINCREMENT,
                post_id TEXT NOT NULL REFERENCES posts(post_id),
                action TEXT NOT NULL CHECK(action IN ('APPROVED', 'REJECTED')),
                previous_status TEXT NOT NULL
                    CHECK(previous_status = 'PENDING_APPROVAL'),
                new_status TEXT NOT NULL
                    CHECK(new_status IN ('APPROVED', 'REJECTED')),
                reviewed_at TEXT NOT NULL
            );

            CREATE INDEX IF NOT EXISTS idx_post_review_events_post_id
                ON post_review_events(post_id);
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
        for column in (
            "external_post_id",
            "published_at",
            "publication_error",
            "version",
        ):
            if column not in columns:
                column_definition = (
                    "INTEGER NOT NULL DEFAULT 1"
                    if column == "version"
                    else "TEXT"
                )
                connection.execute(
                    f"ALTER TABLE posts ADD COLUMN {column} {column_definition}"
                )

        connection.commit()
        return connection
    except BaseException:
        connection.close()
        raise


@contextmanager
def _managed_connection() -> Iterator[sqlite3.Connection]:
    connection = _connect()
    try:
        with connection:
            yield connection
    finally:
        connection.close()


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
    with _managed_connection() as connection:
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
    with _managed_connection() as connection:
        row = connection.execute(
            "SELECT * FROM campaigns WHERE campaign_id = ?", (campaign_id,)
        ).fetchone()
    return _dict(row)


def list_campaigns() -> list[dict[str, Any]]:
    with _managed_connection() as connection:
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

    with _managed_connection() as connection:
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
    with _managed_connection() as connection:
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


def update_post_draft(
    post_id: str, content: str, expected_version: int
) -> dict[str, Any]:
    updated_content = content.strip()
    if not updated_content:
        raise ValueError("Draft content cannot be empty.")
    if len(updated_content) > 10_000:
        raise ValueError("Draft content cannot exceed 10000 characters.")
    if expected_version < 1:
        raise ValueError("Draft version must be a positive integer.")

    with _managed_connection() as connection:
        connection.execute("BEGIN IMMEDIATE")
        row = connection.execute(
            """
            SELECT status, external_post_id, publication_error, version
            FROM posts WHERE post_id = ?
            """,
            (post_id,),
        ).fetchone()
        if row is None:
            raise LookupError("Post not found.")
        if (
            row["status"] != "PENDING_APPROVAL"
            or row["external_post_id"] is not None
            or row["publication_error"] is not None
        ):
            raise DraftEditNotAllowedError(
                "Only pending drafts without a publishing outcome can be edited. "
                "The post's review and publishing state was not changed."
            )
        if row["version"] != expected_version:
            raise DraftEditConflictError(
                "This draft changed after you loaded it. Your edits were not saved. "
                "Refresh to load the latest version, then reapply your changes."
            )

        updated = connection.execute(
            """
            UPDATE posts
            SET content = ?, version = version + 1
            WHERE post_id = ? AND status = 'PENDING_APPROVAL' AND version = ?
              AND external_post_id IS NULL AND publication_error IS NULL
            """,
            (updated_content, post_id, expected_version),
        )
        if updated.rowcount != 1:
            raise DraftEditConflictError(
                "This draft changed after you loaded it. Your edits were not saved. "
                "Refresh to load the latest version, then reapply your changes."
            )
        saved = connection.execute(
            "SELECT * FROM posts WHERE post_id = ?", (post_id,)
        ).fetchone()
    return _dict(saved) or {}


def _review_post(post_id: str, new_status: str) -> dict[str, Any]:
    try:
        with _managed_connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                "SELECT * FROM posts WHERE post_id = ?", (post_id,)
            ).fetchone()
            if row is None:
                raise LookupError("Post not found.")
            if row["status"] != "PENDING_APPROVAL":
                raise ValueError("Only posts pending approval can be reviewed.")

            updated_count = connection.execute(
                """
                UPDATE posts SET status = ?, reviewed_at = CURRENT_TIMESTAMP
                WHERE post_id = ? AND status = 'PENDING_APPROVAL'
                """,
                (new_status, post_id),
            ).rowcount
            if updated_count != 1:
                raise ValueError("Only posts pending approval can be reviewed.")

            updated = connection.execute(
                "SELECT * FROM posts WHERE post_id = ?", (post_id,)
            ).fetchone()
            if updated is None:
                raise ReviewPersistenceError("Review could not be saved.")
            connection.execute(
                """
                INSERT INTO post_review_events (
                    post_id, action, previous_status, new_status, reviewed_at
                )
                VALUES (?, ?, 'PENDING_APPROVAL', ?, ?)
                """,
                (post_id, new_status, new_status, updated["reviewed_at"]),
            )
        return _dict(updated) or {}
    except sqlite3.Error as exc:
        logger.error("Could not persist review transition for post %s.", post_id)
        raise ReviewPersistenceError("Review could not be saved.") from exc


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
    with _managed_connection() as connection:
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
        if row["publication_error"] in (
            PUBLISH_IN_PROGRESS,
            PUBLISH_OUTCOME_UNCERTAIN,
        ):
            raise ValueError(
                "Facebook publishing outcome is uncertain. Verify the Page before "
                "manually resolving this post."
                if row["publication_error"] == PUBLISH_OUTCOME_UNCERTAIN
                else "This post is already being published to Facebook."
            )

    _page_id, access_token, endpoint, timeout_ms = _facebook_publishing_config()

    with _managed_connection() as connection:
        connection.execute("BEGIN IMMEDIATE")
        current = connection.execute(
            """
            SELECT * FROM posts WHERE post_id = ? AND status = 'APPROVED'
              AND platform = 'facebook' AND external_post_id IS NULL
              AND publication_error IS NULL
            """,
            (post_id,),
        ).fetchone()
        if current is None:
            raise ValueError("This post is no longer eligible for Facebook publishing.")
        claimed = connection.execute(
            """
            UPDATE posts SET publication_error = ?
            WHERE post_id = ? AND status = 'APPROVED'
              AND platform = 'facebook' AND external_post_id IS NULL
              AND publication_error IS NULL
            """,
            (PUBLISH_IN_PROGRESS, post_id),
        )
        if claimed.rowcount != 1:
            raise ValueError("This post is no longer eligible for Facebook publishing.")
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
    except error.HTTPError as exc:
        if 400 <= exc.code < 500:
            _clear_facebook_publish_claim(post_id)
            raise FacebookPublishError(
                _facebook_http_error_detail(exc, access_token)
            ) from exc
        exc.close()
        _mark_facebook_publish_uncertain(post_id)
        raise FacebookPublishError(
            f"Facebook publish outcome is uncertain (HTTP {exc.code}). "
            "Verify the Page before retrying."
        ) from exc
    except FacebookPublishError:
        _mark_facebook_publish_uncertain(post_id)
        raise
    except (request.URLError, TimeoutError, OSError, ValueError, json.JSONDecodeError) as exc:
        _mark_facebook_publish_uncertain(post_id)
        raise FacebookPublishError(
            "Facebook publish outcome is uncertain. "
            "Verify the Page before retrying."
        ) from exc

    if not isinstance(result, dict):
        _mark_facebook_publish_uncertain(post_id)
        raise FacebookPublishError(
            "Facebook publish outcome is uncertain because its success response "
            "was invalid. Verify the Page before retrying."
        )
    external_post_id = str(result.get("id", "")).strip()
    if not re.fullmatch(r"[A-Za-z0-9_]+", external_post_id):
        _mark_facebook_publish_uncertain(post_id)
        raise FacebookPublishError(
            "Facebook publish outcome is uncertain because it did not return a "
            "valid post identifier. Verify the Page before retrying."
        )

    try:
        with _managed_connection() as connection:
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
    except (sqlite3.Error, OSError, FacebookPublishError) as exc:
        try:
            _mark_facebook_publish_uncertain(post_id, external_post_id)
        except (sqlite3.Error, OSError, FacebookPublishError) as recovery_exc:
            logger.error(
                "Could not persist Facebook publication uncertainty for post %s; "
                "database recovery is unavailable",
                post_id,
                exc_info=recovery_exc,
            )
            raise FacebookPublishError(
                "Facebook returned a valid post ID, but the local publication "
                "outcome and safe recovery state could not be recorded. "
                "Verify the Page manually and do not retry."
            ) from exc
        logger.error(
            "Could not save confirmed Facebook publication for post %s; "
            "outcome is uncertain and retry is blocked",
            post_id,
            exc_info=exc,
        )
        raise FacebookPublishError(
            "Facebook returned a valid post ID, but the local publication save "
            "failed. Verify the Page before retrying."
        ) from exc
    return _dict(result_row) or {}
