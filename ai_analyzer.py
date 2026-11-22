"""One AI call per opportunity: geography + eligibility + enrichment, returned as validated JSON."""
import json
import logging
import re
from datetime import date

import config
from filters import trim_for_ai
from llm import LLMClient

logger = logging.getLogger(__name__)


class MalformedAIResponse(Exception):
    """The model answered but not with usable JSON (after one retry on another provider)."""


PROMPT_TEMPLATE = """You extract facts from a funding opportunity post for civil society organisations (CSOs) in Ethiopia.
Today's date is {today}. The post was published on {published}.

Return ONLY one JSON object (no markdown, no commentary) with exactly these keys:
{{
  "eligible_locations": [list of countries/regions whose applicants are ELIGIBLE, e.g. "Global", "Africa", "East Africa", "Kenya", "LMICs". Empty list if the post states no geographic restriction],
  "excluded_locations": [countries/regions EXPLICITLY excluded; usually empty],
  "ethiopia_eligible": "yes" | "no" | "unclear"  (can an applicant based in Ethiopia apply?),
  "applicant_types": [subset of {applicant_types}; who may apply],
  "focus_areas": [1 to 3 items, ONLY from: {focus_areas}],
  "funder": "organisation providing the money, or \\"Not Specified\\"",
  "funding_amount": "amount or range as written, e.g. \\"Up to $50,000\\", or \\"Not Specified\\"",
  "deadline": "YYYY-MM-DD" | "Rolling" | "Not Specified",
  "summary": "2-4 plain sentences for a small rural NGO: what it funds, who can apply, what they get"
}}

Rules for "deadline":
- Use the application closing date. If several rounds exist, use the next one on or after today ({today}).
- If the text gives a day and month without a year, choose the year that makes sense given the publish date.
- "Rolling", "ongoing", "open until filled" or "reviewed quarterly" -> "Rolling".
- Only use "Not Specified" if no closing date is stated anywhere.

Title: {title}
Post:
{text}
"""


def build_prompt(title: str, text: str, published: str | None) -> str:
    return PROMPT_TEMPLATE.format(
        today=date.today().isoformat(),
        published=published or "unknown",
        applicant_types=", ".join(config.VALID_APPLICANT_TYPES),
        focus_areas=", ".join(config.VALID_FOCUS_AREAS),
        title=title,
        text=trim_for_ai(text, config.AI_TEXT_CHAR_LIMIT),
    )


def _as_list(value) -> list[str]:
    if value is None:
        return []
    if isinstance(value, str):
        value = [v for v in re.split(r"[;,]", value)]
    return [str(v).strip() for v in value if str(v).strip()]


def _pick_allowed(values: list[str], allowed: list[str], limit: int | None = None) -> list[str]:
    lookup = {a.lower(): a for a in allowed}
    picked = []
    for v in values:
        canon = lookup.get(v.lower().replace(" and ", " & ")) or lookup.get(v.lower())
        if canon and canon not in picked:
            picked.append(canon)
    return picked[:limit] if limit else picked


def parse_response(raw: str) -> dict:
    """Extract and validate the JSON object. Raises MalformedAIResponse."""
    if not raw:
        raise MalformedAIResponse("empty response")
    cleaned = re.sub(r"```(?:json)?", "", raw)
    start, end = cleaned.find("{"), cleaned.rfind("}")
    if start == -1 or end <= start:
        raise MalformedAIResponse("no JSON object found")
    try:
        data = json.loads(cleaned[start:end + 1])
    except json.JSONDecodeError as e:
        raise MalformedAIResponse(f"invalid JSON: {e}") from e
    if not isinstance(data, dict):
        raise MalformedAIResponse("JSON is not an object")

    verdict = str(data.get("ethiopia_eligible", "unclear")).strip().lower()
    result = {
        "eligible_locations": _as_list(data.get("eligible_locations")),
        "excluded_locations": _as_list(data.get("excluded_locations")),
        "ethiopia_eligible": verdict if verdict in ("yes", "no", "unclear") else "unclear",
        "applicant_types": _pick_allowed(_as_list(data.get("applicant_types")), config.VALID_APPLICANT_TYPES),
        "focus_areas": _pick_allowed(_as_list(data.get("focus_areas")), config.VALID_FOCUS_AREAS, limit=3),
        "funder": str(data.get("funder") or "Not Specified").strip(),
        "funding_amount": str(data.get("funding_amount") or "Not Specified").strip(),
        "deadline": str(data.get("deadline") or "Not Specified").strip(),
        "summary": str(data.get("summary") or "").strip(),
    }
    if not result["summary"]:
        raise MalformedAIResponse("missing summary")
    return result


def analyze(client: LLMClient, title: str, text: str, published: str | None = None) -> tuple[dict, str]:
    """
    Returns (analysis_dict, model_label).
    Raises llm.TemporaryAIError (retry next run) or MalformedAIResponse (give up on this item).
    """
    prompt = build_prompt(title, text, published)
    raw, label = client.complete(prompt)
    try:
        return parse_response(raw), label
    except MalformedAIResponse as first_error:
        logger.warning("Malformed AI output from %s for '%s' (%s); retrying once", label, title[:50], first_error)
        if len([p for p in client.providers if not p.disabled_reason]) > 1:
            raw, label = client.complete(prompt, skip={label})
        else:
            raw, label = client.complete(prompt)
        return parse_response(raw), label
