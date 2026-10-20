import pytest
from fastapi.testclient import TestClient
from types import SimpleNamespace
from src.marketing import ai_generator, service
from src.api.main import app

client = TestClient(app)


@pytest.fixture(autouse=True)
def isolated_database(tmp_path, monkeypatch):
    monkeypatch.setenv("MARKETING_DB_PATH", str(tmp_path / "marketing-test.db"))
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


def test_create_campaign_and_list_it():
    campaign = create_test_campaign()
    assert campaign["name"] == "October AI awareness"
    response = client.get("/campaigns")
    assert response.status_code == 200
    assert any(item["campaign_id"] == campaign["campaign_id"] for item in response.json())


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
        def __init__(self, api_key):
            assert api_key == "test-key"
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
