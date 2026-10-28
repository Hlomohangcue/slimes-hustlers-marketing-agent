import json
import pytest
from fastapi.testclient import TestClient
import asyncio
import os
import subprocess
import sys
from types import SimpleNamespace
from urllib import error
from concurrent.futures import ThreadPoolExecutor
from threading import Event

os.environ["APP_ENV"] = "development"

from src.marketing import ai_generator, service
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


def test_generating_drafts_twice_does_not_duplicate_posts():
    campaign = create_test_campaign()
    path = f"/campaigns/{campaign['campaign_id']}/drafts"
    first = client.post(path).json()
    second = client.post(path).json()
    assert {post["post_id"] for post in first} == {post["post_id"] for post in second}
    assert len(client.get("/posts").json()) == 2


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


def test_reject_post_and_prevent_second_review():
    campaign = create_test_campaign()
    posts = client.post(f"/campaigns/{campaign['campaign_id']}/drafts").json()
    post_id = posts[0]["post_id"]

    rejected = client.post(f"/posts/{post_id}/reject")
    assert rejected.status_code == 200
    assert rejected.json()["status"] == "REJECTED"

    second_review = client.post(f"/posts/{post_id}/approve")
    assert second_review.status_code == 409


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


def test_facebook_publishing_posts_approved_content_once(monkeypatch):
    campaign = create_test_campaign()
    posts = client.post(f"/campaigns/{campaign['campaign_id']}/drafts").json()
    facebook = next(post for post in posts if post["platform"] == "facebook")
    client.post(f"/posts/{facebook['post_id']}/approve")
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


@pytest.mark.parametrize(
    ("failure", "expected_detail"),
    [
        (error.HTTPError("https://graph.facebook.com", 403, "forbidden", {}, None),
         "facebook rejected or could not accept"),
        (TimeoutError("timed out"), "facebook rejected or could not accept"),
        (error.URLError("network unavailable"), "facebook rejected or could not accept"),
    ],
)
def test_facebook_publish_network_and_api_failures_are_safe(
    monkeypatch, failure, expected_detail
):
    campaign = create_test_campaign()
    posts = client.post(f"/campaigns/{campaign['campaign_id']}/drafts").json()
    facebook = next(post for post in posts if post["platform"] == "facebook")
    client.post(f"/posts/{facebook['post_id']}/approve")
    monkeypatch.setenv("FACEBOOK_PAGE_ID", "123456")
    monkeypatch.setenv("FACEBOOK_PAGE_ACCESS_TOKEN", "test-page-token")

    def fail(*_args, **_kwargs):
        raise failure

    monkeypatch.setattr(service.request, "urlopen", fail)

    response = client.post(f"/posts/{facebook['post_id']}/publish/facebook")

    assert response.status_code == 502
    assert expected_detail in response.json()["detail"].lower()
    assert "test-page-token" not in str(response.json())
    assert client.get("/posts").json()[0]["external_post_id"] is None


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
    assert client.get("/posts").json()[0]["external_post_id"] is None


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
