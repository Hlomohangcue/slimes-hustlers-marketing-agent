import io
import json
import pytest
from contextlib import closing
from fastapi.testclient import TestClient
import asyncio
import os
import sqlite3
import subprocess
import sys
from types import SimpleNamespace
from urllib import error
from concurrent.futures import ThreadPoolExecutor
from threading import Barrier, Event, Lock

os.environ["APP_ENV"] = "development"

from src.marketing import ai_generator, service
from src.api import main as api_main
from src.api.main import (
    RequestBodyLimitMiddleware,
    _production_origins,
    app,
    validate_runtime_config,
)

TEST_SECRET = "test-only-secret-with-at-least-32-bytes-long"
client = TestClient(app, headers={"Authorization": f"Bearer {TEST_SECRET}"})


@pytest.fixture(autouse=True)
def isolated_database(tmp_path, monkeypatch):
    monkeypatch.setenv("APP_ENV", "development")
    monkeypatch.setenv("MARKETING_DB_PATH", str(tmp_path / "marketing-test.db"))
    monkeypatch.setenv("MARKETING_AUTH_SECRET", TEST_SECRET)
    monkeypatch.setenv("MARKETING_AI_ENABLED", "false")
    monkeypatch.setenv("AI_ENABLED", "false")


def create_test_campaign():
    response = client.post(
        "/campaigns",
        json={
            "name": "October AI awareness",
            "objective": "Introduce practical AI automation services",
            "audience": "small and medium businesses in Lesotho",
            "offer": "Book an introductory conversation",
            "call_to_action": "Message Slimes.Hustlers to discuss your workflow",
        },
    )
    assert response.status_code == 201
    return response.json()


def test_health_endpoint():
    response = client.get("/health")
    assert response.status_code == 200
    assert response.json()["status"] == "ok"


@pytest.mark.parametrize("method,path", [
    ("POST", "/campaigns"),
    ("GET", "/campaigns"),
    ("GET", "/campaigns/CMP-UNKNOWN"),
    ("POST", "/campaigns/CMP-UNKNOWN/drafts"),
    ("GET", "/posts"),
    ("POST", "/posts/PST-UNKNOWN/approve"),
    ("PATCH", "/posts/PST-UNKNOWN/draft"),
    ("POST", "/posts/PST-UNKNOWN/publish/facebook"),
    ("POST", "/posts/PST-UNKNOWN/reject"),
])
def test_every_protected_route_rejects_missing_or_invalid_auth(method, path):
    unauthenticated = TestClient(app)
    missing = unauthenticated.request(method, path, json={} if method == "POST" and path == "/campaigns" else None)
    invalid = unauthenticated.request(
        method, path,
        headers={"Authorization": "Bearer invalid"},
        json={} if method == "POST" and path == "/campaigns" else None,
    )
    assert missing.status_code == 401
    assert invalid.status_code == 401


def test_unauthorized_requests_cannot_create_or_review_data():
    campaign = create_test_campaign()
    posts = client.post(f"/campaigns/{campaign['campaign_id']}/drafts").json()
    post_id = posts[0]["post_id"]
    unauthenticated = TestClient(app)

    assert unauthenticated.post("/campaigns", json={
        "name": "Unauthorized", "objective": "Should not persist", "audience": "Nobody"
    }).status_code == 401
    assert unauthenticated.post(f"/posts/{post_id}/approve").status_code == 401
    assert unauthenticated.post(f"/posts/{post_id}/reject").status_code == 401
    assert unauthenticated.get("/campaigns").status_code == 401
    assert unauthenticated.get("/posts").status_code == 401
    assert len(client.get("/campaigns").json()) == 1
    assert client.get("/posts").json()[0]["status"] == "PENDING_APPROVAL"


def test_health_is_public_and_auth_configuration_fails_closed(monkeypatch):
    monkeypatch.delenv("MARKETING_AUTH_SECRET")
    assert client.get("/health").status_code == 200
    assert client.get("/campaigns").status_code == 503


def test_undersized_auth_secret_fails_closed(monkeypatch):
    monkeypatch.setenv("MARKETING_AUTH_SECRET", "too-short")
    assert client.get("/campaigns").status_code == 503


def test_cors_allows_development_origin_and_rejects_unlisted_origin():
    allowed = client.options("/campaigns", headers={
        "Origin": "http://localhost:5173",
        "Access-Control-Request-Method": "GET",
        "Access-Control-Request-Headers": "authorization",
    })
    denied = client.options("/campaigns", headers={
        "Origin": "https://unlisted.example",
        "Access-Control-Request-Method": "GET",
    })
    assert allowed.status_code == 200
    assert allowed.headers.get("access-control-allow-origin") == "http://localhost:5173"
    assert "authorization" in allowed.headers.get("access-control-allow-headers", "").lower()
    assert "origin" in allowed.headers.get("vary", "").lower()
    assert denied.headers.get("access-control-allow-origin") is None
    assert denied.status_code == 400


def test_create_campaign_and_list_it():
    campaign = create_test_campaign()
    assert campaign["name"] == "October AI awareness"
    response = client.get("/campaigns")
    assert response.status_code == 200
    assert any(item["campaign_id"] == campaign["campaign_id"] for item in response.json())


def test_database_path_creates_parent_and_preserves_existing_campaign(tmp_path, monkeypatch):
    database_path = tmp_path / "mounted-volume" / "marketing.db"
    monkeypatch.setenv("MARKETING_DB_PATH", str(database_path))
    original = service.create_campaign("Persistent", "Keep this", "Pilot user")

    # Reopening the configured path initializes idempotently without replacing data.
    with service._connect() as connection:
        assert connection.execute("SELECT COUNT(*) FROM campaigns").fetchone()[0] == 1
    assert database_path.exists()
    assert service.get_campaign(original["campaign_id"])["name"] == "Persistent"


def test_service_operation_commits_and_closes_connection(monkeypatch):
    original_connect = service._connect
    connections = []

    def tracked_connect():
        connection = original_connect()
        connections.append(connection)
        return connection

    monkeypatch.setattr(service, "_connect", tracked_connect)
    campaign = service.create_campaign(
        "Connection lifecycle", "Verify commit", "Test audience"
    )

    assert len(connections) == 1
    with pytest.raises(sqlite3.ProgrammingError, match="closed"):
        connections[0].execute("SELECT 1")
    with closing(sqlite3.connect(os.environ["MARKETING_DB_PATH"])) as verification:
        assert verification.execute(
            "SELECT name FROM campaigns WHERE campaign_id = ?",
            (campaign["campaign_id"],),
        ).fetchone()[0] == "Connection lifecycle"


def test_managed_connection_rolls_back_and_closes_after_exception(monkeypatch):
    original_connect = service._connect
    connections = []

    def tracked_connect():
        connection = original_connect()
        connections.append(connection)
        return connection

    monkeypatch.setattr(service, "_connect", tracked_connect)
    with pytest.raises(RuntimeError, match="injected operation failure"):
        with service._managed_connection() as connection:
            connection.execute(
                """
                INSERT INTO campaigns (
                    campaign_id, name, objective, audience, offer, call_to_action
                )
                VALUES (
                    'CMP-ROLLBACK01', 'Rollback test', 'Verify rollback',
                    'Test audience', '', 'Learn more'
                )
                """
            )
            raise RuntimeError("injected operation failure")

    assert len(connections) == 1
    with pytest.raises(sqlite3.ProgrammingError, match="closed"):
        connections[0].execute("SELECT 1")
    with closing(sqlite3.connect(os.environ["MARKETING_DB_PATH"])) as verification:
        assert verification.execute(
            "SELECT COUNT(*) FROM campaigns WHERE campaign_id = 'CMP-ROLLBACK01'"
        ).fetchone()[0] == 0


def test_connection_initialization_failure_closes_and_reraises_original(
    monkeypatch,
):
    original_connect = sqlite3.connect
    initialization_error = RuntimeError("injected schema initialization failure")
    connections = []

    class FailingInitializationConnection:
        def __init__(self, connection):
            self.connection = connection
            self.closed = False

        @property
        def row_factory(self):
            return self.connection.row_factory

        @row_factory.setter
        def row_factory(self, value):
            self.connection.row_factory = value

        def execute(self, *args, **kwargs):
            return self.connection.execute(*args, **kwargs)

        def executescript(self, *_args, **_kwargs):
            raise initialization_error

        def close(self):
            self.closed = True
            self.connection.close()

    def failing_connect(database_path):
        connection = FailingInitializationConnection(original_connect(database_path))
        connections.append(connection)
        return connection

    monkeypatch.setattr(service.sqlite3, "connect", failing_connect)

    with pytest.raises(RuntimeError) as raised:
        service._connect()

    assert raised.value is initialization_error
    assert len(connections) == 1
    assert connections[0].closed
    with pytest.raises(sqlite3.ProgrammingError, match="closed"):
        connections[0].connection.execute("SELECT 1")


def test_legacy_database_initialization_preserves_data_and_is_idempotent(
    tmp_path, monkeypatch
):
    database_path = tmp_path / "legacy-marketing.db"
    monkeypatch.setenv("MARKETING_DB_PATH", str(database_path))

    with sqlite3.connect(database_path) as connection:
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        connection.executescript(
            """
            CREATE TABLE campaigns (
                campaign_id TEXT PRIMARY KEY,
                name TEXT NOT NULL,
                objective TEXT NOT NULL,
                audience TEXT NOT NULL,
                offer TEXT NOT NULL,
                call_to_action TEXT NOT NULL,
                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
            );

            CREATE TABLE posts (
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

            INSERT INTO campaigns (
                campaign_id, name, objective, audience, offer, call_to_action,
                created_at
            )
            VALUES (
                'CMP-LEGACY001', 'Legacy campaign', 'Preserve existing data',
                'Pilot businesses', 'Consultation', 'Contact the team',
                '2026-10-01 09:00:00'
            );

            INSERT INTO posts (
                post_id, campaign_id, platform, content, generation_source,
                status, external_post_id, published_at, publication_error,
                created_at, reviewed_at
            )
            VALUES (
                'PST-LEGACY001', 'CMP-LEGACY001', 'facebook',
                'Existing approved draft', 'template', 'APPROVED',
                NULL, NULL, NULL, '2026-10-01 09:05:00',
                '2026-10-01 09:10:00'
            );
            """
        )
        campaigns_before = [
            dict(row)
            for row in connection.execute(
                "SELECT * FROM campaigns ORDER BY campaign_id"
            ).fetchall()
        ]
        posts_before = [
            dict(row)
            for row in connection.execute(
                "SELECT * FROM posts ORDER BY post_id"
            ).fetchall()
        ]
        posts_before = [{**post, "version": 1} for post in posts_before]

    initialized = service._connect()
    try:
        campaigns_after = [
            dict(row)
            for row in initialized.execute(
                "SELECT * FROM campaigns ORDER BY campaign_id"
            ).fetchall()
        ]
        posts_after = [
            dict(row)
            for row in initialized.execute(
                "SELECT * FROM posts ORDER BY post_id"
            ).fetchall()
        ]
        assert campaigns_after == campaigns_before
        assert posts_after == posts_before

        migrated_post = initialized.execute(
            "SELECT version FROM posts WHERE post_id = 'PST-LEGACY001'"
        ).fetchone()
        assert migrated_post["version"] == 1
        assert initialized.execute(
            "SELECT COUNT(*) FROM sqlite_master "
            "WHERE type = 'table' AND name = 'post_review_events'"
        ).fetchone()[0] == 1
        assert initialized.execute(
            "SELECT COUNT(*) FROM sqlite_master "
            "WHERE type = 'index' AND name = 'idx_post_review_events_post_id'"
        ).fetchone()[0] == 1
        index_columns = initialized.execute(
            "PRAGMA index_info(idx_post_review_events_post_id)"
        ).fetchall()
        assert [(row["seqno"], row["name"]) for row in index_columns] == [
            (0, "post_id")
        ]

        assert initialized.execute("PRAGMA foreign_keys").fetchone()[0] == 1
        foreign_keys = initialized.execute(
            "PRAGMA foreign_key_list(post_review_events)"
        ).fetchall()
        assert any(
            row["table"] == "posts"
            and row["from"] == "post_id"
            and row["to"] == "post_id"
            for row in foreign_keys
        )
        with pytest.raises(sqlite3.IntegrityError):
            initialized.execute(
                """
                INSERT INTO post_review_events (
                    post_id, action, previous_status, new_status, reviewed_at
                )
                VALUES ('PST-MISSING', 'APPROVED', 'PENDING_APPROVAL',
                        'APPROVED', '2026-10-01 09:10:00')
                """
            )

        invalid_audit_values = (
            ("INVALID", "PENDING_APPROVAL", "APPROVED"),
            ("APPROVED", "APPROVED", "APPROVED"),
            ("APPROVED", "PENDING_APPROVAL", "INVALID"),
        )
        for action, previous_status, new_status in invalid_audit_values:
            with pytest.raises(sqlite3.IntegrityError):
                initialized.execute(
                    """
                    INSERT INTO post_review_events (
                        post_id, action, previous_status, new_status, reviewed_at
                    )
                    VALUES ('PST-LEGACY001', ?, ?, ?, '2026-10-01 09:10:00')
                    """,
                    (action, previous_status, new_status),
                )

        initialized.execute(
            """
            INSERT INTO post_review_events (
                post_id, action, previous_status, new_status, reviewed_at
            )
            VALUES (
                'PST-LEGACY001', 'APPROVED', 'PENDING_APPROVAL', 'APPROVED',
                '2026-10-01 09:10:00'
            )
            """
        )
        initialized.commit()
    finally:
        initialized.close()

    initialized_again = service._connect()
    try:
        assert [
            dict(row)
            for row in initialized_again.execute(
                "SELECT * FROM campaigns ORDER BY campaign_id"
            ).fetchall()
        ] == campaigns_before
        assert [
            dict(row)
            for row in initialized_again.execute(
                "SELECT * FROM posts ORDER BY post_id"
            ).fetchall()
        ] == posts_before
        assert initialized_again.execute(
            "SELECT COUNT(*) FROM post_review_events"
        ).fetchone()[0] == 1
        assert initialized_again.execute(
            "SELECT COUNT(*) FROM sqlite_master "
            "WHERE type = 'table' AND name = 'post_review_events'"
        ).fetchone()[0] == 1
        assert initialized_again.execute(
            "SELECT COUNT(*) FROM sqlite_master "
            "WHERE type = 'index' AND name = 'idx_post_review_events_post_id'"
        ).fetchone()[0] == 1
        index_columns = initialized_again.execute(
            "PRAGMA index_info(idx_post_review_events_post_id)"
        ).fetchall()
        assert [(row["seqno"], row["name"]) for row in index_columns] == [
            (0, "post_id")
        ]
    finally:
        initialized_again.close()


def test_sqlite_backup_restore_preserves_campaigns_posts_and_review_history(
    tmp_path, monkeypatch
):
    source_path = tmp_path / "source.db"
    backup_path = tmp_path / "backup.db"
    restored_path = tmp_path / "restored.db"
    assert len({source_path, backup_path, restored_path}) == 3
    assert not source_path.exists()
    assert not backup_path.exists()
    assert not restored_path.exists()

    def snapshot(connection):
        table_rows = {
            "campaigns": tuple(
                tuple(row)
                for row in connection.execute(
                    "SELECT * FROM campaigns ORDER BY campaign_id"
                ).fetchall()
            ),
            "posts": tuple(
                tuple(row)
                for row in connection.execute(
                    "SELECT * FROM posts ORDER BY post_id"
                ).fetchall()
            ),
            "post_review_events": tuple(
                tuple(row)
                for row in connection.execute(
                    "SELECT * FROM post_review_events ORDER BY review_event_id"
                ).fetchall()
            ),
        }
        schema = tuple(
            tuple(row)
            for row in connection.execute(
                """
                SELECT type, name, tbl_name, sql FROM sqlite_master
                WHERE type IN ('table', 'index') AND name NOT LIKE 'sqlite_%'
                ORDER BY type, name
                """
            ).fetchall()
        )
        indexes = {}
        foreign_keys = {}
        for table in table_rows:
            index_list = connection.execute(
                f'PRAGMA index_list("{table}")'
            ).fetchall()
            indexes[table] = tuple(
                (
                    row[0],
                    row[1],
                    row[2],
                    row[3],
                    row[4],
                    tuple(
                        tuple(index_column)
                        for index_column in connection.execute(
                            f'PRAGMA index_info("{row[1]}")'
                        ).fetchall()
                    ),
                )
                for row in index_list
            )
            foreign_keys[table] = tuple(
                tuple(row)
                for row in connection.execute(
                    f'PRAGMA foreign_key_list("{table}")'
                ).fetchall()
            )
        return table_rows, schema, indexes, foreign_keys

    monkeypatch.setenv("MARKETING_DB_PATH", str(source_path))
    with closing(service._connect()) as source:
        source.executemany(
            """
            INSERT INTO campaigns (
                campaign_id, name, objective, audience, offer, call_to_action,
                created_at
            )
            VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            [
                (
                    "CMP-BACKUP001",
                    "Synthetic backup campaign one",
                    "Preserve approved history",
                    "Test audience one",
                    "Synthetic offer",
                    "Contact the team",
                    "2026-11-01 09:00:00",
                ),
                (
                    "CMP-BACKUP002",
                    "Synthetic backup campaign two",
                    "Preserve pending content",
                    "Test audience two",
                    "",
                    "Learn more",
                    "2026-11-02 10:00:00",
                ),
            ],
        )
        source.executemany(
            """
            INSERT INTO posts (
                post_id, campaign_id, platform, content, generation_source,
                status, external_post_id, published_at, publication_error,
                created_at, reviewed_at
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            [
                (
                    "PST-BACKUP001",
                    "CMP-BACKUP001",
                    "linkedin",
                    "Synthetic approved post",
                    "template",
                    "APPROVED",
                    None,
                    None,
                    None,
                    "2026-11-01 09:05:00",
                    "2026-11-01 09:10:00",
                ),
                (
                    "PST-BACKUP002",
                    "CMP-BACKUP001",
                    "facebook",
                    "Synthetic rejected post",
                    "gemini",
                    "REJECTED",
                    None,
                    None,
                    None,
                    "2026-11-01 09:06:00",
                    "2026-11-01 09:11:00",
                ),
                (
                    "PST-BACKUP003",
                    "CMP-BACKUP002",
                    "linkedin",
                    "Synthetic pending post",
                    "template",
                    "PENDING_APPROVAL",
                    None,
                    None,
                    None,
                    "2026-11-02 10:05:00",
                    None,
                ),
            ],
        )
        source.executemany(
            """
            INSERT INTO post_review_events (
                review_event_id, post_id, action, previous_status, new_status,
                reviewed_at
            )
            VALUES (?, ?, ?, 'PENDING_APPROVAL', ?, ?)
            """,
            [
                (
                    1,
                    "PST-BACKUP001",
                    "APPROVED",
                    "APPROVED",
                    "2026-11-01 09:10:00",
                ),
                (
                    2,
                    "PST-BACKUP002",
                    "REJECTED",
                    "REJECTED",
                    "2026-11-01 09:11:00",
                ),
            ],
        )
        source.commit()
        baseline = snapshot(source)
        baseline_counts = {
            table: len(rows) for table, rows in baseline[0].items()
        }

        assert source.execute("PRAGMA foreign_keys").fetchone()[0] == 1
        campaign_sql = source.execute(
            "SELECT sql FROM sqlite_master WHERE type = 'table' AND name = 'campaigns'"
        ).fetchone()[0]
        posts_sql = source.execute(
            "SELECT sql FROM sqlite_master WHERE type = 'table' AND name = 'posts'"
        ).fetchone()[0]
        audit_sql = source.execute(
            "SELECT sql FROM sqlite_master "
            "WHERE type = 'table' AND name = 'post_review_events'"
        ).fetchone()[0]
        assert "CHECK(platform IN ('linkedin', 'facebook'))" in posts_sql
        assert "CHECK(generation_source IN ('gemini', 'template'))" in posts_sql
        assert (
            "status IN ('PENDING_APPROVAL', 'APPROVED', 'REJECTED')" in posts_sql
        )
        assert "CHECK(action IN ('APPROVED', 'REJECTED'))" in audit_sql
        assert "CHECK(previous_status = 'PENDING_APPROVAL')" in audit_sql
        assert "CHECK(new_status IN ('APPROVED', 'REJECTED'))" in audit_sql
        assert "campaign_id TEXT PRIMARY KEY" in campaign_sql
        assert any(
            index[1] == "idx_post_review_events_post_id"
            and tuple(column[2] for column in index[5]) == ("post_id",)
            for index in baseline[2]["post_review_events"]
        )
        assert any(
            foreign_key[2:5] == ("posts", "post_id", "post_id")
            for foreign_key in baseline[3]["post_review_events"]
        )
        assert any(
            foreign_key[2:5] == ("campaigns", "campaign_id", "campaign_id")
            for foreign_key in baseline[3]["posts"]
        )

        with closing(sqlite3.connect(backup_path)) as backup:
            source.backup(backup)

    with closing(sqlite3.connect(backup_path)) as backup:
        assert snapshot(backup) == baseline
        with closing(sqlite3.connect(restored_path)) as restored:
            backup.backup(restored)

    assert len({source_path, backup_path, restored_path}) == 3
    assert source_path.exists()
    assert backup_path.exists()
    assert restored_path.exists()

    with closing(sqlite3.connect(restored_path)) as restored:
        restored.row_factory = sqlite3.Row
        restored.execute("PRAGMA foreign_keys = ON")
        assert [
            tuple(row)
            for row in restored.execute("PRAGMA integrity_check").fetchall()
        ] == [("ok",)]
        assert restored.execute("PRAGMA foreign_key_check").fetchall() == []
        assert snapshot(restored) == baseline

    monkeypatch.setenv("MARKETING_DB_PATH", str(restored_path))
    for _ in range(2):
        with closing(service._connect()) as initialized:
            assert initialized.execute("PRAGMA foreign_keys").fetchone()[0] == 1
            initialized_snapshot = snapshot(initialized)
            assert initialized_snapshot == baseline
            assert {
                table: len(rows)
                for table, rows in initialized_snapshot[0].items()
            } == baseline_counts


def test_campaign_requires_core_fields():
    response = client.post("/campaigns", json={"name": "Missing fields"})
    assert response.status_code == 422


def test_generate_linkedin_and_facebook_drafts_pending_approval():
    campaign = create_test_campaign()
    response = client.post(f"/campaigns/{campaign['campaign_id']}/drafts")
    assert response.status_code == 201
    posts = response.json()
    assert {post["platform"] for post in posts} == {"linkedin", "facebook"}
    assert all(post["status"] == "PENDING_APPROVAL" for post in posts)
    assert all(post["reviewed_at"] is None for post in posts)


def test_campaign_remains_persisted_when_draft_generation_fails(monkeypatch):
    campaign = create_test_campaign()

    def fail_draft_generation(_campaign_id):
        raise RuntimeError("injected draft-generation failure")

    monkeypatch.setattr(api_main, "generate_drafts", fail_draft_generation)
    client_without_server_exceptions = TestClient(
        app,
        headers={"Authorization": f"Bearer {TEST_SECRET}"},
        raise_server_exceptions=False,
    )

    response = client_without_server_exceptions.post(
        f"/campaigns/{campaign['campaign_id']}/drafts"
    )

    assert response.status_code == 500
    assert "injected draft-generation failure" not in response.text
    assert service.get_campaign(campaign["campaign_id"]) == campaign
    assert service.list_posts(campaign["campaign_id"]) == []


def test_generating_drafts_twice_does_not_duplicate_posts():
    campaign = create_test_campaign()
    path = f"/campaigns/{campaign['campaign_id']}/drafts"
    first = client.post(path).json()
    second = client.post(path).json()
    assert {post["post_id"] for post in first} == {post["post_id"] for post in second}
    assert len(client.get("/posts").json()) == 2


def test_edit_pending_draft_persists_content_and_increments_version():
    campaign = create_test_campaign()
    posts = client.post(f"/campaigns/{campaign['campaign_id']}/drafts").json()
    post = posts[0]
    edited_content = "Updated content for review."

    response = client.patch(
        f"/posts/{post['post_id']}/draft",
        json={"content": edited_content, "version": post["version"]},
    )

    assert response.status_code == 200
    saved = response.json()
    assert saved["content"] == edited_content
    assert saved["version"] == post["version"] + 1
    assert saved["status"] == "PENDING_APPROVAL"
    assert saved["platform"] == post["platform"]
    assert saved["generation_source"] == post["generation_source"]

    reloaded = next(
        item for item in client.get("/posts").json()
        if item["post_id"] == post["post_id"]
    )
    assert reloaded["content"] == edited_content
    assert reloaded["version"] == saved["version"]

    approved = client.post(f"/posts/{post['post_id']}/approve")
    assert approved.status_code == 200
    assert approved.json()["content"] == edited_content
    assert approved.json()["status"] == "APPROVED"


def test_edit_with_stale_version_returns_conflict_without_overwriting():
    campaign = create_test_campaign()
    post = client.post(
        f"/campaigns/{campaign['campaign_id']}/drafts"
    ).json()[0]
    first = client.patch(
        f"/posts/{post['post_id']}/draft",
        json={"content": "First saved update.", "version": post["version"]},
    )
    stale = client.patch(
        f"/posts/{post['post_id']}/draft",
        json={"content": "Stale update.", "version": post["version"]},
    )

    assert first.status_code == 200
    assert stale.status_code == 409
    assert "changed after you loaded it" in stale.json()["detail"]
    assert "Refresh" in stale.json()["detail"]
    saved = next(
        item for item in client.get("/posts").json()
        if item["post_id"] == post["post_id"]
    )
    assert saved["content"] == "First saved update."
    assert saved["version"] == post["version"] + 1
    assert saved["status"] == "PENDING_APPROVAL"


def test_competing_draft_updates_only_accept_one_version():
    campaign = create_test_campaign()
    post = client.post(
        f"/campaigns/{campaign['campaign_id']}/drafts"
    ).json()[0]
    barrier = Barrier(2)

    def update(content):
        barrier.wait(timeout=5)
        return client.patch(
            f"/posts/{post['post_id']}/draft",
            json={"content": content, "version": post["version"]},
        )

    with ThreadPoolExecutor(max_workers=2) as executor:
        results = [
            executor.submit(update, "Competing update one."),
            executor.submit(update, "Competing update two."),
        ]
        responses = [result.result(timeout=10) for result in results]

    assert sorted(response.status_code for response in responses) == [200, 409]
    saved = next(
        item for item in client.get("/posts").json()
        if item["post_id"] == post["post_id"]
    )
    winner = next(response.json() for response in responses if response.status_code == 200)
    assert saved["content"] == winner["content"]
    assert saved["version"] == post["version"] + 1
    assert saved["status"] == "PENDING_APPROVAL"


@pytest.mark.parametrize(
    "content,payload",
    [
        ("whitespace", {"content": "   ", "version": 1}),
        ("too long", {"content": "x" * 10_001, "version": 1}),
        ("missing version", {"content": "Valid content."}),
        ("invalid version", {"content": "Valid content.", "version": 0}),
    ],
)
def test_invalid_draft_edit_is_rejected_without_changing_content(content, payload):
    campaign = create_test_campaign()
    post = client.post(
        f"/campaigns/{campaign['campaign_id']}/drafts"
    ).json()[0]

    response = client.patch(f"/posts/{post['post_id']}/draft", json=payload)

    assert response.status_code == 422
    saved = next(
        item for item in client.get("/posts").json()
        if item["post_id"] == post["post_id"]
    )
    assert saved["content"] == post["content"]
    assert saved["version"] == post["version"]
    assert saved["status"] == "PENDING_APPROVAL"


@pytest.mark.parametrize(
    "state",
    ["APPROVED", "REJECTED", "PUBLISHED", "IN_PROGRESS", "UNCERTAIN"],
)
def test_draft_edit_is_disallowed_after_review_or_publish_claim(state):
    campaign = create_test_campaign()
    post = client.post(
        f"/campaigns/{campaign['campaign_id']}/drafts"
    ).json()[0]
    if state == "APPROVED":
        assert client.post(f"/posts/{post['post_id']}/approve").status_code == 200
    elif state == "REJECTED":
        assert client.post(f"/posts/{post['post_id']}/reject").status_code == 200
    elif state == "PUBLISHED":
        with service._managed_connection() as connection:
            connection.execute(
                """
                UPDATE posts SET status = 'APPROVED', external_post_id = ?,
                    published_at = CURRENT_TIMESTAMP
                WHERE post_id = ?
                """,
                ("123456_789", post["post_id"]),
            )
    else:
        publication_error = (
            service.PUBLISH_IN_PROGRESS
            if state == "IN_PROGRESS"
            else service.PUBLISH_OUTCOME_UNCERTAIN
        )
        with service._managed_connection() as connection:
            connection.execute(
                "UPDATE posts SET publication_error = ? WHERE post_id = ?",
                (publication_error, post["post_id"]),
            )

    response = client.patch(
        f"/posts/{post['post_id']}/draft",
        json={"content": "Must remain unchanged.", "version": post["version"]},
    )

    assert response.status_code == 409
    assert "Only pending drafts" in response.json()["detail"]
    saved = next(
        item for item in client.get("/posts").json()
        if item["post_id"] == post["post_id"]
    )
    assert saved["content"] == post["content"]
    assert saved["version"] == post["version"]
    expected_status = {
        "APPROVED": "APPROVED",
        "REJECTED": "REJECTED",
        "PUBLISHED": "APPROVED",
        "IN_PROGRESS": "PENDING_APPROVAL",
        "UNCERTAIN": "PENDING_APPROVAL",
    }[state]
    assert saved["status"] == expected_status


def test_missing_campaign_returns_404():
    response = client.post("/campaigns/unknown/drafts")
    assert response.status_code == 404


def test_post_must_be_approved_explicitly():
    campaign = create_test_campaign()
    posts = client.post(f"/campaigns/{campaign['campaign_id']}/drafts").json()
    assert posts[0]["status"] == "PENDING_APPROVAL"

    response = client.post(f"/posts/{posts[0]['post_id']}/approve")
    assert response.status_code == 200
    assert response.json()["status"] == "APPROVED"
    assert response.json()["reviewed_at"] is not None
    with service._connect() as connection:
        event = connection.execute(
            "SELECT action, previous_status, new_status, reviewed_at "
            "FROM post_review_events WHERE post_id = ?",
            (posts[0]["post_id"],),
        ).fetchone()
    assert tuple(event) == (
        "APPROVED",
        "PENDING_APPROVAL",
        "APPROVED",
        response.json()["reviewed_at"],
    )


def test_reject_post_and_prevent_second_review():
    campaign = create_test_campaign()
    posts = client.post(f"/campaigns/{campaign['campaign_id']}/drafts").json()
    post_id = posts[0]["post_id"]

    rejected = client.post(f"/posts/{post_id}/reject")
    assert rejected.status_code == 200
    assert rejected.json()["status"] == "REJECTED"
    with service._connect() as connection:
        event = connection.execute(
            "SELECT action, previous_status, new_status, reviewed_at "
            "FROM post_review_events WHERE post_id = ?",
            (post_id,),
        ).fetchone()
    assert tuple(event) == (
        "REJECTED",
        "PENDING_APPROVAL",
        "REJECTED",
        rejected.json()["reviewed_at"],
    )

    second_review = client.post(f"/posts/{post_id}/approve")
    assert second_review.status_code == 409


def test_approved_post_cannot_be_rejected():
    campaign = create_test_campaign()
    posts = client.post(f"/campaigns/{campaign['campaign_id']}/drafts").json()
    post_id = posts[0]["post_id"]
    approved = client.post(f"/posts/{post_id}/approve")
    assert approved.status_code == 200

    rejected = client.post(f"/posts/{post_id}/reject")

    assert rejected.status_code == 409
    final_post = next(
        post for post in client.get("/posts").json()
        if post["post_id"] == post_id
    )
    assert final_post["status"] == "APPROVED"


def test_repeated_approval_returns_conflict_and_records_one_event():
    campaign = create_test_campaign()
    posts = client.post(f"/campaigns/{campaign['campaign_id']}/drafts").json()
    post_id = posts[0]["post_id"]

    first = client.post(f"/posts/{post_id}/approve")
    second = client.post(f"/posts/{post_id}/approve")

    assert first.status_code == 200
    assert second.status_code == 409
    with service._connect() as connection:
        events = connection.execute(
            "SELECT action FROM post_review_events WHERE post_id = ?",
            (post_id,),
        ).fetchall()
    assert [event["action"] for event in events] == ["APPROVED"]


def test_concurrent_conflicting_reviews_only_allow_one_transition():
    campaign = create_test_campaign()
    posts = client.post(f"/campaigns/{campaign['campaign_id']}/drafts").json()
    post_id = posts[0]["post_id"]
    barrier = Barrier(2)

    def review(action):
        barrier.wait(timeout=5)
        return client.post(f"/posts/{post_id}/{action}")

    with ThreadPoolExecutor(max_workers=2) as executor:
        approve = executor.submit(review, "approve")
        reject = executor.submit(review, "reject")
        responses = [approve.result(timeout=10), reject.result(timeout=10)]

    assert sorted(response.status_code for response in responses) == [200, 409]
    final_status = next(
        response.json()["status"] for response in responses
        if response.status_code == 200
    )
    with service._connect() as connection:
        post = connection.execute(
            "SELECT status FROM posts WHERE post_id = ?", (post_id,)
        ).fetchone()
        events = connection.execute(
            "SELECT action, previous_status, new_status "
            "FROM post_review_events WHERE post_id = ?",
            (post_id,),
        ).fetchall()
    assert post["status"] == final_status
    assert len(events) == 1
    assert events[0]["action"] == final_status
    assert events[0]["previous_status"] == "PENDING_APPROVAL"
    assert events[0]["new_status"] == final_status


def test_review_audit_database_failure_rolls_back_without_exposing_db_error():
    campaign = create_test_campaign()
    posts = client.post(f"/campaigns/{campaign['campaign_id']}/drafts").json()
    post_id = posts[0]["post_id"]
    with service._connect() as connection:
        connection.execute(
            """
            CREATE TRIGGER reject_review_audit
            BEFORE INSERT ON post_review_events
            BEGIN
                SELECT RAISE(ABORT, 'sensitive database failure detail');
            END
            """
        )

    response = client.post(f"/posts/{post_id}/approve")

    assert response.status_code == 500
    assert response.json()["detail"] == "Review action could not be saved."
    assert "sensitive database failure detail" not in response.text
    with service._connect() as connection:
        post = connection.execute(
            "SELECT status, reviewed_at FROM posts WHERE post_id = ?",
            (post_id,),
        ).fetchone()
        event_count = connection.execute(
            "SELECT COUNT(*) FROM post_review_events WHERE post_id = ?",
            (post_id,),
        ).fetchone()[0]
    assert post["status"] == "PENDING_APPROVAL"
    assert post["reviewed_at"] is None
    assert event_count == 0


def test_unknown_post_returns_404():
    response = client.post("/posts/unknown/approve")
    assert response.status_code == 404


def test_facebook_post_must_be_approved_before_explicit_publishing():
    campaign = create_test_campaign()
    posts = client.post(f"/campaigns/{campaign['campaign_id']}/drafts").json()
    facebook = next(post for post in posts if post["platform"] == "facebook")

    response = client.post(f"/posts/{facebook['post_id']}/publish/facebook")

    assert response.status_code == 409
    assert "approved" in response.json()["detail"].lower()


def test_facebook_approval_workflow_posts_approved_content_once(monkeypatch):
    campaign = create_test_campaign()
    posts = client.post(f"/campaigns/{campaign['campaign_id']}/drafts").json()
    facebook = next(post for post in posts if post["platform"] == "facebook")
    approved = client.post(f"/posts/{facebook['post_id']}/approve")
    assert approved.status_code == 200
    assert approved.json()["status"] == "APPROVED"
    monkeypatch.setenv("FACEBOOK_PAGE_ID", "123456")
    monkeypatch.setenv("FACEBOOK_PAGE_ACCESS_TOKEN", "test-page-token")

    class FakeResponse:
        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

        def getcode(self):
            return 200

        def read(self):
            return b'{"id":"123456_789012"}'

    def fake_urlopen(http_request, timeout):
        assert timeout == 20
        assert http_request.full_url == "https://graph.facebook.com/v26.0/123456/feed"
        assert http_request.method == "POST"
        assert http_request.get_header("Authorization") == "Bearer test-page-token"
        assert http_request.get_header("Content-type") == "application/json"
        assert json.loads(http_request.data.decode()) == {"message": facebook["content"]}
        assert "test-page-token" not in http_request.full_url
        return FakeResponse()

    monkeypatch.setattr(service.request, "urlopen", fake_urlopen)

    response = client.post(f"/posts/{facebook['post_id']}/publish/facebook")

    assert response.status_code == 200
    assert response.json()["external_post_id"] == "123456_789012"
    assert response.json()["published_at"] is not None

    duplicate = client.post(f"/posts/{facebook['post_id']}/publish/facebook")
    assert duplicate.status_code == 409
    assert "already been published" in duplicate.json()["detail"]


def test_facebook_publishing_fails_closed_when_configuration_is_missing(monkeypatch):
    campaign = create_test_campaign()
    posts = client.post(f"/campaigns/{campaign['campaign_id']}/drafts").json()
    facebook = next(post for post in posts if post["platform"] == "facebook")
    client.post(f"/posts/{facebook['post_id']}/approve")
    monkeypatch.delenv("FACEBOOK_PAGE_ID", raising=False)
    monkeypatch.delenv("FACEBOOK_PAGE_ACCESS_TOKEN", raising=False)

    response = client.post(f"/posts/{facebook['post_id']}/publish/facebook")

    assert response.status_code == 503
    assert "not configured" in response.json()["detail"].lower()


@pytest.mark.parametrize("status", ["PENDING_APPROVAL", "REJECTED"])
def test_facebook_publishing_rejects_unpublishable_status(status):
    campaign = create_test_campaign()
    posts = client.post(f"/campaigns/{campaign['campaign_id']}/drafts").json()
    facebook = next(post for post in posts if post["platform"] == "facebook")
    if status == "REJECTED":
        client.post(f"/posts/{facebook['post_id']}/reject")

    response = client.post(f"/posts/{facebook['post_id']}/publish/facebook")

    assert response.status_code == 409
    assert "approved" in response.json()["detail"].lower()


def test_facebook_publishing_rejects_linkedin_posts(monkeypatch):
    campaign = create_test_campaign()
    posts = client.post(f"/campaigns/{campaign['campaign_id']}/drafts").json()
    linkedin = next(post for post in posts if post["platform"] == "linkedin")
    client.post(f"/posts/{linkedin['post_id']}/approve")
    monkeypatch.setenv("FACEBOOK_PAGE_ID", "123456")
    monkeypatch.setenv("FACEBOOK_PAGE_ACCESS_TOKEN", "test-page-token")

    response = client.post(f"/posts/{linkedin['post_id']}/publish/facebook")

    assert response.status_code == 409
    assert "facebook posts" in response.json()["detail"].lower()


@pytest.mark.parametrize("status", [400, 403])
def test_facebook_confirmed_http_rejection_returns_sanitized_meta_details(
    monkeypatch, status
):
    campaign = create_test_campaign()
    posts = client.post(f"/campaigns/{campaign['campaign_id']}/drafts").json()
    facebook = next(post for post in posts if post["platform"] == "facebook")
    client.post(f"/posts/{facebook['post_id']}/approve")
    monkeypatch.setenv("FACEBOOK_PAGE_ID", "123456")
    monkeypatch.setenv("FACEBOOK_PAGE_ACCESS_TOKEN", "test-page-token")
    monkeypatch.setenv("FACEBOOK_APP_SECRET", "test-app-secret")
    body = json.dumps({
        "error": {
            "message": (
                "Permission pages_manage_posts denied for test-page-token; "
                "Authorization: Bearer test-auth-header; "
                "app_secret=test-app-secret"
            ),
            "type": "OAuthException",
            "code": 200,
            "error_subcode": 1815014,
            "fbtrace_id": "trace-123",
        }
    }).encode()

    def fail(*_args, **_kwargs):
        raise error.HTTPError(
            "https://graph.facebook.com/v26.0/123456/feed",
            status,
            "Meta error",
            {},
            io.BytesIO(body),
        )

    monkeypatch.setattr(service.request, "urlopen", fail)

    response = client.post(f"/posts/{facebook['post_id']}/publish/facebook")

    assert response.status_code == 502
    detail = response.json()["detail"]
    assert f"HTTP {status}" in detail
    assert "code 200" in detail
    assert "subcode 1815014" in detail
    assert "type: OAuthException" in detail
    assert "pages_manage_posts denied" in detail
    assert "fbtrace_id: trace-123" in detail
    assert "test-page-token" not in detail
    assert "test-auth-header" not in detail
    assert "test-app-secret" not in detail
    assert client.get("/posts").json()[0]["publication_error"] is None


def test_facebook_error_details_sanitize_credentials_in_all_text_fields(monkeypatch):
    monkeypatch.setenv("FACEBOOK_PAGE_ACCESS_TOKEN", "test-page-token")
    monkeypatch.setenv("FACEBOOK_APP_SECRET", "test-app-secret")
    response = error.HTTPError(
        "https://graph.facebook.com/v26.0/123456/feed",
        403,
        "Meta error",
        {},
        io.BytesIO(json.dumps({
            "error": {
                "message": "Denied: test-page-token app_secret=test-app-secret",
                "type": "OAuthException Bearer test-auth-header",
                "code": 200,
                "error_subcode": 1815014,
                "fbtrace_id": "trace-test-page-token",
            }
        }).encode()),
    )

    detail = service._facebook_http_error_detail(response, "test-page-token")

    assert "type: OAuthException" in detail
    assert "code 200" in detail
    assert "subcode 1815014" in detail
    assert "fbtrace_id: trace-" in detail
    assert "test-page-token" not in detail
    assert "test-app-secret" not in detail
    assert "test-auth-header" not in detail


def test_facebook_error_details_redact_bearer_authorization_text():
    bearer = "Be" + "arer "
    auth_value = "test-auth-header"
    response = error.HTTPError(
        "https://graph.facebook.com/v26.0/123456/feed",
        403,
        "Meta error",
        {},
        io.BytesIO(json.dumps({
            "error": {
                "message": f"Authorization: {bearer}{auth_value}; permission denied",
                "type": f"OAuthException {bearer}{auth_value}",
                "code": 200,
            }
        }).encode()),
    )

    detail = service._facebook_http_error_detail(response, "test-page-token")

    assert "permission denied" in detail
    assert "OAuthException" in detail
    assert auth_value not in detail


def test_facebook_error_details_omit_missing_optional_fields(monkeypatch):
    monkeypatch.setenv("FACEBOOK_PAGE_ID", "123456")
    monkeypatch.setenv("FACEBOOK_PAGE_ACCESS_TOKEN", "test-page-token")
    campaign = create_test_campaign()
    posts = client.post(f"/campaigns/{campaign['campaign_id']}/drafts").json()
    facebook = next(post for post in posts if post["platform"] == "facebook")
    client.post(f"/posts/{facebook['post_id']}/approve")

    def fail(*_args, **_kwargs):
        raise error.HTTPError(
            "https://graph.facebook.com/v26.0/123456/feed",
            403,
            "Meta error",
            {},
            io.BytesIO(b'{"error":{"message":"Permission denied","code":200}}'),
        )

    monkeypatch.setattr(service.request, "urlopen", fail)
    response = client.post(f"/posts/{facebook['post_id']}/publish/facebook")

    assert response.status_code == 502
    detail = response.json()["detail"]
    assert "code 200" in detail
    assert "message: Permission denied" in detail
    assert "type:" not in detail
    assert "subcode" not in detail
    assert "fbtrace_id:" not in detail
    post = next(
        item for item in client.get("/posts").json()
        if item["post_id"] == facebook["post_id"]
    )
    assert post["publication_error"] is None


def test_facebook_confirmed_http_rejection_allows_a_retry(monkeypatch):
    campaign = create_test_campaign()
    posts = client.post(f"/campaigns/{campaign['campaign_id']}/drafts").json()
    facebook = next(post for post in posts if post["platform"] == "facebook")
    client.post(f"/posts/{facebook['post_id']}/approve")
    monkeypatch.setenv("FACEBOOK_PAGE_ID", "123456")
    monkeypatch.setenv("FACEBOOK_PAGE_ACCESS_TOKEN", "test-page-token")

    failure = error.HTTPError(
        "https://graph.facebook.com/v26.0/123456/feed",
        403,
        "Meta error",
        {},
        io.BytesIO(b'{"error":{"message":"Permission denied","code":10}}'),
    )
    monkeypatch.setattr(service.request, "urlopen", lambda *_args, **_kwargs: (_ for _ in ()).throw(failure))
    rejected = client.post(f"/posts/{facebook['post_id']}/publish/facebook")
    assert rejected.status_code == 502
    assert client.get("/posts").json()[0]["publication_error"] is None

    class SuccessResponse:
        def __enter__(self):
            return self
        def __exit__(self, *args):
            return False
        def getcode(self):
            return 200
        def read(self):
            return b'{"id":"123456_789012"}'

    monkeypatch.setattr(service.request, "urlopen", lambda *_args, **_kwargs: SuccessResponse())
    retried = client.post(f"/posts/{facebook['post_id']}/publish/facebook")
    assert retried.status_code == 200
    assert retried.json()["external_post_id"] == "123456_789012"


def test_facebook_success_database_save_failure_persists_uncertain_outcome(monkeypatch):
    campaign = create_test_campaign()
    posts = client.post(f"/campaigns/{campaign['campaign_id']}/drafts").json()
    facebook = next(post for post in posts if post["platform"] == "facebook")
    client.post(f"/posts/{facebook['post_id']}/approve")
    monkeypatch.setenv("FACEBOOK_PAGE_ID", "123456")
    monkeypatch.setenv("FACEBOOK_PAGE_ACCESS_TOKEN", "test-page-token")
    calls = 0

    class SuccessResponse:
        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

        def getcode(self):
            return 200

        def read(self):
            return b'{"id":"123456_789012"}'

    def publish_response(*_args, **_kwargs):
        nonlocal calls
        calls += 1
        return SuccessResponse()

    monkeypatch.setattr(service.request, "urlopen", publish_response)
    original_connect = service._connect

    class FailFinalSaveConnection:
        def __init__(self, connection):
            self.connection = connection
            self.final_save = False

        def __enter__(self):
            self.connection.__enter__()
            return self

        def __exit__(self, *args):
            return self.connection.__exit__(*args)

        def close(self):
            self.connection.close()

        def execute(self, statement, parameters=()):
            if "published_at = CURRENT_TIMESTAMP" in statement:
                self.final_save = True
            return self.connection.execute(statement, parameters)

        def commit(self):
            if self.final_save:
                raise sqlite3.OperationalError("controlled final save failure")
            self.connection.commit()

    def guarded_connect():
        return FailFinalSaveConnection(original_connect())

    monkeypatch.setattr(service, "_connect", guarded_connect)

    response = client.post(f"/posts/{facebook['post_id']}/publish/facebook")

    assert response.status_code == 502
    assert "valid post ID" in response.json()["detail"]
    assert "Verify the Page before retrying" in response.json()["detail"]
    assert calls == 1
    with original_connect() as connection:
        post = connection.execute(
            "SELECT external_post_id, publication_error FROM posts WHERE post_id = ?",
            (facebook["post_id"],),
        ).fetchone()
    assert post["external_post_id"] == "123456_789012"
    assert post["publication_error"] == service.PUBLISH_OUTCOME_UNCERTAIN

    blocked = client.post(f"/posts/{facebook['post_id']}/publish/facebook")
    assert blocked.status_code == 409
    assert calls == 1


def test_facebook_success_database_save_and_uncertain_save_failure(monkeypatch, caplog):
    campaign = create_test_campaign()
    posts = client.post(f"/campaigns/{campaign['campaign_id']}/drafts").json()
    facebook = next(post for post in posts if post["platform"] == "facebook")
    client.post(f"/posts/{facebook['post_id']}/approve")
    monkeypatch.setenv("FACEBOOK_PAGE_ID", "123456")
    monkeypatch.setenv("FACEBOOK_PAGE_ACCESS_TOKEN", "test-page-token")
    calls = 0

    class SuccessResponse:
        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

        def getcode(self):
            return 200

        def read(self):
            return b'{"id":"123456_789012"}'

    def publish_response(*_args, **_kwargs):
        nonlocal calls
        calls += 1
        return SuccessResponse()

    monkeypatch.setattr(service.request, "urlopen", publish_response)
    original_connect = service._connect

    class FailBothRecoveryCommits:
        def __init__(self, connection):
            self.connection = connection
            self.fail_on_commit = False

        def __enter__(self):
            self.connection.__enter__()
            return self

        def __exit__(self, *args):
            return self.connection.__exit__(*args)

        def close(self):
            self.connection.close()

        def execute(self, statement, parameters=()):
            if "published_at = CURRENT_TIMESTAMP" in statement:
                self.fail_on_commit = True
            elif "external_post_id = COALESCE" in statement:
                self.fail_on_commit = True
            return self.connection.execute(statement, parameters)

        def commit(self):
            if self.fail_on_commit:
                raise sqlite3.OperationalError("controlled recovery save failure")
            self.connection.commit()

    monkeypatch.setattr(
        service,
        "_connect",
        lambda: FailBothRecoveryCommits(original_connect()),
    )

    response = client.post(f"/posts/{facebook['post_id']}/publish/facebook")

    assert response.status_code == 502
    assert "safe recovery state could not be recorded" in response.json()["detail"]
    assert "do not retry" in response.json()["detail"].lower()
    assert calls == 1
    assert "database recovery is unavailable" in caplog.text
    assert "test-page-token" not in response.text
    with original_connect() as connection:
        post = connection.execute(
            "SELECT external_post_id, publication_error FROM posts WHERE post_id = ?",
            (facebook["post_id"],),
        ).fetchone()
    assert post["external_post_id"] is None
    assert post["publication_error"] == service.PUBLISH_IN_PROGRESS


def test_facebook_non_json_http_error_uses_safe_fallback(monkeypatch):
    campaign = create_test_campaign()
    posts = client.post(f"/campaigns/{campaign['campaign_id']}/drafts").json()
    facebook = next(post for post in posts if post["platform"] == "facebook")
    client.post(f"/posts/{facebook['post_id']}/approve")
    monkeypatch.setenv("FACEBOOK_PAGE_ID", "123456")
    monkeypatch.setenv("FACEBOOK_PAGE_ACCESS_TOKEN", "test-page-token")

    def fail(*_args, **_kwargs):
        raise error.HTTPError(
            "https://graph.facebook.com/v26.0/123456/feed",
            403,
            "forbidden",
            {},
            io.BytesIO(b"test-page-token is invalid"),
        )

    monkeypatch.setattr(service.request, "urlopen", fail)
    response = client.post(f"/posts/{facebook['post_id']}/publish/facebook")

    assert response.status_code == 502
    assert response.json()["detail"] == "Facebook rejected the post (HTTP 403)"
    assert "test-page-token" not in str(response.json())


@pytest.mark.parametrize("failure", [TimeoutError("timed out"), error.URLError("network unavailable")])
def test_facebook_ambiguous_failure_blocks_retry_until_manual_verification(
    monkeypatch, failure
):
    campaign = create_test_campaign()
    posts = client.post(f"/campaigns/{campaign['campaign_id']}/drafts").json()
    facebook = next(post for post in posts if post["platform"] == "facebook")
    client.post(f"/posts/{facebook['post_id']}/approve")
    monkeypatch.setenv("FACEBOOK_PAGE_ID", "123456")
    monkeypatch.setenv("FACEBOOK_PAGE_ACCESS_TOKEN", "test-page-token")
    calls = 0

    def fail(*_args, **_kwargs):
        nonlocal calls
        calls += 1
        raise failure

    monkeypatch.setattr(service.request, "urlopen", fail)
    response = client.post(f"/posts/{facebook['post_id']}/publish/facebook")

    assert response.status_code == 502
    assert "uncertain" in response.json()["detail"].lower()
    assert calls == 1
    post = next(
        item for item in client.get("/posts").json()
        if item["post_id"] == facebook["post_id"]
    )
    assert post["external_post_id"] is None
    assert post["publication_error"] == service.PUBLISH_OUTCOME_UNCERTAIN

    blocked = client.post(f"/posts/{facebook['post_id']}/publish/facebook")
    assert blocked.status_code == 409
    assert "uncertain" in blocked.json()["detail"].lower()
    assert calls == 1


@pytest.mark.parametrize("body", [b"{}", b"[]", b'{"id":""}', b'{"id":"bad id"}'])
def test_facebook_publish_requires_valid_success_identifier(monkeypatch, body):
    campaign = create_test_campaign()
    posts = client.post(f"/campaigns/{campaign['campaign_id']}/drafts").json()
    facebook = next(post for post in posts if post["platform"] == "facebook")
    client.post(f"/posts/{facebook['post_id']}/approve")
    monkeypatch.setenv("FACEBOOK_PAGE_ID", "123456")
    monkeypatch.setenv("FACEBOOK_PAGE_ACCESS_TOKEN", "test-page-token")

    class InvalidResponse:
        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

        def getcode(self):
            return 200

        def read(self):
            return body

    monkeypatch.setattr(service.request, "urlopen", lambda *_args, **_kwargs: InvalidResponse())

    response = client.post(f"/posts/{facebook['post_id']}/publish/facebook")

    assert response.status_code == 502
    post = next(
        item for item in client.get("/posts").json()
        if item["post_id"] == facebook["post_id"]
    )
    assert post["external_post_id"] is None
    assert post["publication_error"] == service.PUBLISH_OUTCOME_UNCERTAIN


def test_facebook_http_server_error_blocks_retry(monkeypatch):
    campaign = create_test_campaign()
    posts = client.post(f"/campaigns/{campaign['campaign_id']}/drafts").json()
    facebook = next(post for post in posts if post["platform"] == "facebook")
    client.post(f"/posts/{facebook['post_id']}/approve")
    monkeypatch.setenv("FACEBOOK_PAGE_ID", "123456")
    monkeypatch.setenv("FACEBOOK_PAGE_ACCESS_TOKEN", "test-page-token")

    def fail(*_args, **_kwargs):
        raise error.HTTPError(
            "https://graph.facebook.com/v26.0/123456/feed",
            503,
            "unavailable",
            {},
            io.BytesIO(b'{"error":{"message":"Unavailable"}}'),
        )

    monkeypatch.setattr(service.request, "urlopen", fail)
    response = client.post(f"/posts/{facebook['post_id']}/publish/facebook")

    assert response.status_code == 502
    assert "uncertain" in response.json()["detail"]
    blocked = client.post(f"/posts/{facebook['post_id']}/publish/facebook")
    assert blocked.status_code == 409


def test_concurrent_facebook_publish_requests_only_call_meta_once(monkeypatch):
    campaign = create_test_campaign()
    posts = client.post(f"/campaigns/{campaign['campaign_id']}/drafts").json()
    facebook = next(post for post in posts if post["platform"] == "facebook")
    client.post(f"/posts/{facebook['post_id']}/approve")
    monkeypatch.setenv("FACEBOOK_PAGE_ID", "123456")
    monkeypatch.setenv("FACEBOOK_PAGE_ACCESS_TOKEN", "test-page-token")
    entered = Event()
    release = Event()
    calls = 0

    class ConcurrentResponse:
        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

        def getcode(self):
            return 200

        def read(self):
            return b'{"id":"123456_789012"}'

    def concurrent_urlopen(*_args, **_kwargs):
        nonlocal calls
        calls += 1
        entered.set()
        assert release.wait(timeout=5)
        return ConcurrentResponse()

    monkeypatch.setattr(service.request, "urlopen", concurrent_urlopen)

    with ThreadPoolExecutor(max_workers=2) as executor:
        first = executor.submit(
            client.post, f"/posts/{facebook['post_id']}/publish/facebook"
        )
        assert entered.wait(timeout=5)
        second = executor.submit(
            client.post, f"/posts/{facebook['post_id']}/publish/facebook"
        )
        second_response = second.result(timeout=5)
        release.set()
        first_response = first.result(timeout=5)

    assert second_response.status_code == 409
    assert "already being published" in second_response.json()["detail"]
    assert first_response.status_code == 200
    assert calls == 1


def test_facebook_uncertain_outcome_blocks_request_with_stale_eligibility(monkeypatch):
    campaign = create_test_campaign()
    posts = client.post(f"/campaigns/{campaign['campaign_id']}/drafts").json()
    facebook = next(post for post in posts if post["platform"] == "facebook")
    client.post(f"/posts/{facebook['post_id']}/approve")
    monkeypatch.setenv("FACEBOOK_PAGE_ID", "123456")
    monkeypatch.setenv("FACEBOOK_PAGE_ACCESS_TOKEN", "test-page-token")

    delayed_request_passed_eligibility = Event()
    release_delayed_request = Event()
    configuration_lock = Lock()
    configuration_calls = 0
    provider_calls = 0
    original_config = service._facebook_publishing_config

    def controlled_config():
        nonlocal configuration_calls
        with configuration_lock:
            configuration_calls += 1
            call_number = configuration_calls
        if call_number == 1:
            delayed_request_passed_eligibility.set()
            assert release_delayed_request.wait(timeout=5)
        return original_config()

    def uncertain_provider_outcome(*_args, **_kwargs):
        nonlocal provider_calls
        provider_calls += 1
        raise TimeoutError("simulated uncertain provider outcome")

    monkeypatch.setattr(service, "_facebook_publishing_config", controlled_config)
    monkeypatch.setattr(service.request, "urlopen", uncertain_provider_outcome)

    with ThreadPoolExecutor(max_workers=2) as executor:
        delayed = executor.submit(
            client.post, f"/posts/{facebook['post_id']}/publish/facebook"
        )
        assert delayed_request_passed_eligibility.wait(timeout=5)
        first = executor.submit(
            client.post, f"/posts/{facebook['post_id']}/publish/facebook"
        )
        first_response = first.result(timeout=5)
        assert first_response.status_code == 502

        release_delayed_request.set()
        delayed_response = delayed.result(timeout=5)

    assert delayed_response.status_code == 409
    assert "no longer eligible" in delayed_response.json()["detail"].lower()
    assert provider_calls == 1
    post = next(
        item for item in client.get("/posts").json()
        if item["post_id"] == facebook["post_id"]
    )
    assert post["external_post_id"] is None
    assert post["publication_error"] == service.PUBLISH_OUTCOME_UNCERTAIN


def test_gemini_generator_returns_ai_content(monkeypatch):
    monkeypatch.setenv("MARKETING_AI_ENABLED", "true")
    monkeypatch.setenv("GEMINI_API_KEY", "test-key")
    monkeypatch.setenv("GEMINI_MODEL", "gemini-3.6-flash")

    expected_content = (
        "Make your business smarter with practical AI automation. "
        "Contact Slimes.Hustlers today. #BusinessAutomation"
    )

    class FakeModels:
        def generate_content(self, **kwargs):
            assert kwargs["model"] == "gemini-3.6-flash"
            assert "Slimes.Hustlers AI Solutions" in kwargs["contents"]
            return SimpleNamespace(text=expected_content)

    class FakeClient:
        def __init__(self, api_key, http_options):
            assert api_key == "test-key"
            assert http_options.timeout == 20000
            self.models = FakeModels()

        def close(self):
            pass

    monkeypatch.setattr(
        ai_generator.genai,
        "Client",
        FakeClient,
    )

    campaign = {
        "name": "AI awareness",
        "objective": "Introduce practical AI automation",
        "audience": "Small businesses in Lesotho",
        "offer": "Book an introductory conversation",
        "call_to_action": "Contact Slimes.Hustlers today",
    }

    result = ai_generator.generate_ai_draft(campaign, "linkedin")

    assert result == expected_content


def test_template_fallback_is_used_when_ai_returns_none(monkeypatch):
    monkeypatch.setattr(
        service,
        "generate_ai_draft",
        lambda campaign, platform: None,
    )

    campaign = create_test_campaign()

    response = client.post(
        f"/campaigns/{campaign['campaign_id']}/drafts"
    )

    assert response.status_code == 201

    posts = response.json()
    assert len(posts) == 2
    assert all(post["status"] == "PENDING_APPROVAL" for post in posts)

    linkedin_post = next(
        post for post in posts if post["platform"] == "linkedin"
    )
    assert "Looking for smarter ways to run your business?" in (
        linkedin_post["content"]
    )


def run_body_middleware(chunks, headers=()):
    async def exercise():
        incoming = iter(chunks)
        sent = []
        downstream_body = bytearray()
        receive_calls = 0

        async def receive():
            nonlocal receive_calls
            receive_calls += 1
            return next(incoming)

        async def send(message):
            sent.append(message)

        async def downstream(_scope, downstream_receive, downstream_send):
            while True:
                message = await downstream_receive()
                downstream_body.extend(message.get("body", b""))
                if not message.get("more_body", False):
                    break
            await downstream_send({"type": "http.response.start", "status": 200, "headers": []})
            await downstream_send({"type": "http.response.body", "body": b"ok"})

        middleware = RequestBodyLimitMiddleware(downstream, max_body_size=32)
        await middleware({"type": "http", "headers": list(headers)}, receive, send)
        return sent, bytes(downstream_body), receive_calls

    return asyncio.run(exercise())


def test_body_limit_rejects_oversized_accurate_content_length_without_reading_body():
    sent, _, receive_calls = run_body_middleware(
        [{"type": "http.request", "body": b"x" * 33, "more_body": False}],
        [(b"content-length", b"33")],
    )
    assert sent[0]["status"] == 413
    assert receive_calls == 0


@pytest.mark.parametrize("headers", [
    (),
    ((b"content-length", b"4"),),
])
def test_body_limit_rejects_oversized_chunked_or_inaccurate_length(headers):
    sent, _, _ = run_body_middleware([
        {"type": "http.request", "body": b"a" * 20, "more_body": True},
        {"type": "http.request", "body": b"b" * 13, "more_body": True},
    ], headers)
    assert sent[0]["status"] == 413
    assert sent[1]["body"] == b'{"detail":"Request body too large."}'


def test_body_limit_forwards_normal_sized_json_to_api_route():
    response = client.post("/campaigns", json={
        "name": "Normal request",
        "objective": "Verify bounded request forwarding",
        "audience": "Pilot user",
    })
    assert response.status_code == 201


def test_body_limit_forwards_normal_sized_body_to_downstream():
    payload = b'{"name":"test"}'
    sent, body, _ = run_body_middleware([
        {"type": "http.request", "body": payload, "more_body": False},
    ], [(b"content-length", str(len(payload)).encode("ascii"))])
    assert body == payload
    assert sent[0]["status"] == 200


def test_body_limit_returns_cleanly_on_client_disconnect():
    sent, _, _ = run_body_middleware([
        {"type": "http.disconnect"},
    ])
    assert sent == []


def test_production_cors_rejects_wildcard_origin():
    environment = dict(os.environ)
    environment.update({"APP_ENV": "production", "CORS_ALLOWED_ORIGINS": "*"})
    result = subprocess.run(
        [sys.executable, "-c", "import src.api.main"],
        env=environment,
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode != 0
    assert "exact origins" in result.stderr


@pytest.mark.parametrize("origin", [
    "http://app.example.com",
    "https://app.example.com/",
    "https://app.example.com/path",
    "https://app.example.com?query=1",
    "https://app.example.com#fragment",
    "https://user:password@app.example.com",
    "https://app.example.com:99999",
    "https://app.example.com:",
    "https://localhost",
    "https://127.0.0.2",
    "not-an-origin",
    "https://bad host.example",
])
def test_production_cors_rejects_non_exact_or_non_https_origins(origin):
    with pytest.raises(RuntimeError):
        _production_origins(origin)


def test_production_cors_canonicalizes_valid_https_origins():
    assert _production_origins("https://APP.example.com,https://api.example.com:443") == [
        "https://app.example.com",
        "https://api.example.com",
    ]


def test_production_cors_rejects_wildcard_origin_directly():
    with pytest.raises(RuntimeError, match="exact origins"):
        _production_origins("*")


def test_production_cors_preflight_uses_canonical_allowed_origins(tmp_path):
    environment = dict(os.environ)
    environment.update({
        "APP_ENV": "production",
        "MARKETING_AUTH_SECRET": TEST_SECRET,
        "MARKETING_DB_PATH": str(tmp_path / "marketing.db"),
        "CORS_ALLOWED_ORIGINS": "https://APP.example.com:443",
    })
    result = subprocess.run(
        [sys.executable, "-c", (
            "from fastapi.testclient import TestClient\n"
            "from src.api.main import app\n"
            "with TestClient(app) as client:\n"
            " allowed = client.options('/campaigns', headers={"
            "'Origin': 'https://app.example.com', "
            "'Access-Control-Request-Method': 'GET', "
            "'Access-Control-Request-Headers': 'authorization'})\n"
            " denied = client.options('/campaigns', headers={"
            "'Origin': 'https://unlisted.example', "
            "'Access-Control-Request-Method': 'GET'})\n"
            " assert allowed.status_code == 200\n"
            " assert allowed.headers.get('access-control-allow-origin') == 'https://app.example.com'\n"
            " assert denied.status_code == 400\n"
            " assert denied.headers.get('access-control-allow-origin') is None\n"
        )],
        env=environment,
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr


def test_production_runtime_configuration_fails_closed(monkeypatch, tmp_path):
    monkeypatch.delenv("APP_ENV", raising=False)
    with pytest.raises(RuntimeError, match="APP_ENV"):
        validate_runtime_config()

    monkeypatch.setenv("APP_ENV", "production")
    monkeypatch.setenv("MARKETING_AUTH_SECRET", "short")
    with pytest.raises(RuntimeError, match="at least 32 bytes"):
        validate_runtime_config()

    with pytest.raises(RuntimeError, match="MARKETING_AUTH_SECRET"):
        monkeypatch.delenv("MARKETING_AUTH_SECRET")
        validate_runtime_config()

    monkeypatch.setenv("MARKETING_AUTH_SECRET", TEST_SECRET)
    monkeypatch.delenv("MARKETING_DB_PATH", raising=False)
    with pytest.raises(RuntimeError, match="MARKETING_DB_PATH"):
        validate_runtime_config()

    monkeypatch.setenv("MARKETING_DB_PATH", "relative/marketing.db")
    with pytest.raises(RuntimeError, match="absolute path"):
        validate_runtime_config()

    monkeypatch.setenv("MARKETING_DB_PATH", str(tmp_path / "volume" / "marketing.db"))
    with pytest.raises(RuntimeError, match="existing writable directory"):
        validate_runtime_config()

    volume = tmp_path / "volume"
    volume.mkdir()
    monkeypatch.setenv("MARKETING_DB_PATH", str(volume / "marketing.db"))
    monkeypatch.delenv("CORS_ALLOWED_ORIGINS", raising=False)
    with pytest.raises(RuntimeError, match="CORS_ALLOWED_ORIGINS"):
        validate_runtime_config()

    monkeypatch.setenv("CORS_ALLOWED_ORIGINS", "https://app.example.com")
    validate_runtime_config()


def test_production_startup_fails_before_serving_when_configuration_is_invalid(monkeypatch):
    monkeypatch.setenv("APP_ENV", "production")
    monkeypatch.delenv("MARKETING_AUTH_SECRET", raising=False)
    with pytest.raises(RuntimeError, match="MARKETING_AUTH_SECRET"):
        with TestClient(app):
            pass


def test_backend_import_requires_explicit_app_environment():
    environment = dict(os.environ)
    environment.pop("APP_ENV", None)
    result = subprocess.run(
        [sys.executable, "-c", "import src.api.main"],
        env=environment,
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode != 0
    assert "APP_ENV must be explicitly set" in result.stderr


def test_production_disables_api_documentation_routes():
    environment = dict(os.environ)
    environment.update({
        "APP_ENV": "production",
        "CORS_ALLOWED_ORIGINS": "https://app.example.com",
    })
    result = subprocess.run(
        [sys.executable, "-c", (
            "from src.api.main import app; "
            "assert app.docs_url is None and app.redoc_url is None "
            "and app.openapi_url is None"
        )],
        env=environment,
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr


def test_body_limit_rejects_chunked_oversize_without_content_length():
    async def exercise():
        body_chunks = iter([
            {"type": "http.request", "body": b"a" * 800_000, "more_body": True},
            {"type": "http.request", "body": b"b" * 248_577, "more_body": True},
        ])
        sent = []
        called_app = False

        async def receive():
            return next(body_chunks)

        async def send(message):
            sent.append(message)

        async def downstream(_scope, _receive, _send):
            nonlocal called_app
            called_app = True

        middleware = RequestBodyLimitMiddleware(downstream)
        await middleware({"type": "http", "headers": []}, receive, send)
        assert not called_app
        assert sent[0]["status"] == 413
        assert b"".join(message.get("body", b"") for message in sent[1:]) == (
            b'{"detail":"Request body too large."}'
        )

    asyncio.run(exercise())


def test_gemini_capacity_limit_uses_template_fallback(monkeypatch):
    monkeypatch.setenv("MARKETING_AI_ENABLED", "true")
    monkeypatch.setenv("GEMINI_API_KEY", "test-key")

    class FullCapacity:
        def acquire(self, timeout):
            assert timeout == 1
            return False

    monkeypatch.setattr(ai_generator, "_generation_slots", FullCapacity())
    result = ai_generator.generate_ai_draft({
        "name": "test",
        "objective": "test objective",
        "audience": "test audience",
        "offer": "",
        "call_to_action": "test CTA",
    }, "linkedin")
    assert result is None


def test_gemini_provider_errors_fall_back_without_logging_exception_details(
    monkeypatch, caplog
):
    monkeypatch.setenv("MARKETING_AI_ENABLED", "true")
    monkeypatch.setenv("GEMINI_API_KEY", "test-provider-secret")

    class FakeModels:
        def generate_content(self, **_kwargs):
            raise RuntimeError("provider echoed test-provider-secret and private prompt")

    class FakeClient:
        def __init__(self, **_kwargs):
            self.models = FakeModels()

        def close(self):
            pass

    monkeypatch.setattr(ai_generator.genai, "Client", FakeClient)
    result = ai_generator.generate_ai_draft({
        "name": "private campaign",
        "objective": "private objective",
        "audience": "private audience",
        "offer": "",
        "call_to_action": "private CTA",
    }, "linkedin")

    assert result is None
    assert "test-provider-secret" not in caplog.text
    assert "private prompt" not in caplog.text


def test_gemini_server_error_retries_once_then_returns_ai_content(monkeypatch):
    from google.genai import errors

    monkeypatch.setenv("MARKETING_AI_ENABLED", "true")
    monkeypatch.setenv("GEMINI_API_KEY", "test-key")

    expected_content = "Useful AI automation ideas. #BusinessAutomation"

    class FakeModels:
        def __init__(self):
            self.calls = 0

        def generate_content(self, **_kwargs):
            self.calls += 1
            if self.calls == 1:
                raise errors.ServerError(503, {"status": "UNAVAILABLE"})
            return SimpleNamespace(text=expected_content)

    class FakeClient:
        def __init__(self, **_kwargs):
            self.models = FakeModels()

        def close(self):
            pass

    fake_client = FakeClient()
    monkeypatch.setattr(
        ai_generator.genai, "Client", lambda **_kwargs: fake_client
    )

    result = ai_generator.generate_ai_draft({
        "name": "test",
        "objective": "test objective",
        "audience": "test audience",
        "offer": "",
        "call_to_action": "test CTA",
    }, "facebook")

    assert result == expected_content
    assert fake_client.models.calls == 2


def test_gemini_server_error_after_retry_uses_fallback(monkeypatch, caplog):
    from google.genai import errors

    monkeypatch.setenv("MARKETING_AI_ENABLED", "true")
    monkeypatch.setenv("GEMINI_API_KEY", "test-key")

    class FakeModels:
        def __init__(self):
            self.calls = 0

        def generate_content(self, **_kwargs):
            self.calls += 1
            raise errors.ServerError(
                503,
                {"status": "UNAVAILABLE", "message": "private provider detail"},
            )

    class FakeClient:
        def __init__(self, **_kwargs):
            self.models = FakeModels()

        def close(self):
            pass

    fake_client = FakeClient()
    monkeypatch.setattr(
        ai_generator.genai, "Client", lambda **_kwargs: fake_client
    )

    result = ai_generator.generate_ai_draft({
        "name": "private campaign",
        "objective": "private objective",
        "audience": "private audience",
        "offer": "",
        "call_to_action": "private CTA",
    }, "facebook")

    assert result is None
    assert fake_client.models.calls == 2
    assert "private provider detail" not in caplog.text
