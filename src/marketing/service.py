import os
import sqlite3
import uuid
from pathlib import Path
from typing import Any
from src.marketing.ai_generator import generate_ai_draft

DEFAULT_DB_PATH = Path(__file__).resolve().parents[2] / "data" / "marketing.db"


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
        connection.commit()

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
