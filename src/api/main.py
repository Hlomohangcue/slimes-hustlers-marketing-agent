from fastapi.middleware.cors import CORSMiddleware
from fastapi import FastAPI, HTTPException
from pydantic import BaseModel, Field

from src.marketing.service import (
    approve_post,
    create_campaign,
    generate_drafts,
    get_campaign,
    list_campaigns,
    list_posts,
    reject_post,
)

app = FastAPI(
    title="Slimes.Hustlers Marketing Agent",
    description=(
        "Marketing campaign drafts and human approval workflow. "
        "This version does not publish to social media."
    ),
    version="0.1.0",
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=[
        "http://localhost:5173",
        "http://127.0.0.1:5173",
    ],
    allow_credentials=False,
    allow_methods=["*"],
    allow_headers=["*"],
)


class CampaignCreate(BaseModel):
    name: str = Field(min_length=1, max_length=120)
    objective: str = Field(min_length=1, max_length=1000)
    audience: str = Field(min_length=1, max_length=300)
    offer: str = Field(default="", max_length=500)
    call_to_action: str = Field(
        default="Contact us to learn more", max_length=300
    )


@app.get("/health")
def health() -> dict[str, str]:
    return {"status": "ok", "service": "marketing-agent"}


@app.post("/campaigns", status_code=201)
def create_campaign_endpoint(payload: CampaignCreate) -> dict:
    try:
        return create_campaign(**payload.model_dump())
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc


@app.get("/campaigns")
def list_campaigns_endpoint() -> list[dict]:
    return list_campaigns()


@app.get("/campaigns/{campaign_id}")
def get_campaign_endpoint(campaign_id: str) -> dict:
    campaign = get_campaign(campaign_id)
    if campaign is None:
        raise HTTPException(status_code=404, detail="Campaign not found.")
    return campaign


@app.post("/campaigns/{campaign_id}/drafts", status_code=201)
def generate_drafts_endpoint(campaign_id: str) -> list[dict]:
    try:
        return generate_drafts(campaign_id)
    except LookupError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@app.get("/posts")
def list_posts_endpoint(campaign_id: str | None = None) -> list[dict]:
    return list_posts(campaign_id)


@app.post("/posts/{post_id}/approve")
def approve_post_endpoint(post_id: str) -> dict:
    try:
        return approve_post(post_id)
    except LookupError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc


@app.post("/posts/{post_id}/reject")
def reject_post_endpoint(post_id: str) -> dict:
    try:
        return reject_post(post_id)
    except LookupError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
