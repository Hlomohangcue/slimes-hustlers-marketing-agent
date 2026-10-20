
import logging
import os
from typing import Any

from dotenv import load_dotenv
from google import genai

logger = logging.getLogger(__name__)

load_dotenv()


def generate_ai_draft(
    campaign: dict[str, Any],
    platform: str,
) -> str | None:
    """Generate a marketing draft with Gemini, returning None on failure."""
    enabled = os.getenv(
        "MARKETING_AI_ENABLED",
        os.getenv("AI_ENABLED", "false"),
    ).strip().lower() in {"1", "true", "yes", "on"}

    api_key = os.getenv("GEMINI_API_KEY")
    if not enabled or not api_key:
        return None

    model = os.getenv("GEMINI_MODEL", "gemini-3.6-flash")

    if platform == "linkedin":
        style = (
            "Write a professional LinkedIn post, around 100-180 words. "
            "Use a strong opening, practical business value, "
            "and relevant hashtags."
        )
    elif platform == "facebook":
        style = (
            "Write an approachable Facebook post, around 50-100 words. "
            "Use a clear opening, concise business value, "
            "and relevant hashtags."
        )
    else:
        raise ValueError(f"Unsupported platform: {platform}")

    prompt = f"""
You write marketing content for Slimes.Hustlers AI Solutions,
a business based in Maseru, Lesotho.

Create one original {platform} marketing draft.
{style}

Campaign name: {campaign['name']}
Objective: {campaign['objective']}
Target audience: {campaign['audience']}
Offer: {campaign['offer'] or 'No specific offer provided'}
Call to action: {campaign['call_to_action']}

Rules:
- Use clear, natural, professional English.
- Explain practical AI, automation, or business value relevant to this campaign.
- Do not invent customer testimonials, statistics, prices, guarantees,
  results, or services not supported by the campaign details.
- Include the supplied call to action.
- Return only the post text, without explanations or quotation marks.
"""

    client = None
    try:
        client = genai.Client(api_key=api_key)
        response = client.models.generate_content(
            model=model,
            contents=prompt,
        )
        content = (response.text or "").strip()
        return content or None
    except Exception:
        logger.warning(
            "Gemini draft generation failed for %s; using template fallback.",
            platform,
            exc_info=True,
        )
        return None
    finally:
        if client is not None:
            try:
                client.close()
            except Exception:
                logger.debug(
                    "Could not close Gemini client.",
                    exc_info=True,
                )
