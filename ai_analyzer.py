#ai_analyzer.py



import os
import json
import re
import time
import logging
from dotenv import load_dotenv
import google.generativeai as genai
import config

logger = logging.getLogger(__name__)
load_dotenv()
GOOGLE_API_KEY = os.getenv("GOOGLE_API_KEY")

if GOOGLE_API_KEY:
    genai.configure(api_key=GOOGLE_API_KEY)
    ai_model = genai.GenerativeModel('gemma-3n-e2b-it')
else:
    logger.critical("GOOGLE_API_KEY not found in .env file. AI Analyzer cannot function.")
    ai_model = None     

def _call_gemini_with_retry(prompt, retries=3, base_delay=5):
    """
    Private helper to call Gemini API with robust, adaptive retry mechanism.
    Returns the response text on success, None on failure.
    """
    delay = base_delay
    for i in range(retries):
        try:
            response = ai_model.generate_content(prompt)
            time.sleep(1)
            return response.text
        except Exception as e:
            error_str = str(e)
            if "429" in error_str or "RESOURCE_EXHAUSTED" in error_str:
                match = re.search(r'retry_delay {\s*seconds: (\d+)\s*}', error_str)
                if match:
                    wait_time = int(match.group(1)) + 1
                    logger.warning(f"AI rate limit hit. API suggests waiting {wait_time}s. Attempt {i+1}/{retries}.")
                    time.sleep(wait_time)
                else:
                    logger.warning(f"AI rate limit hit. Retrying in {delay}s... (Attempt {i+1}/{retries})")
                    time.sleep(delay)
                    delay *= 2
            else:
                logger.error(f"AI call failed with a non-rate-limit error: {e}. Retrying in {delay}s...", exc_info=True)
                time.sleep(delay)
    
    logger.error(f"AI call failed permanently after {retries} retries.")
    return None

def get_geographic_scope(title, text_content):
    """
    AI Task 1: Extracts a LIST of all mentioned geographic locations.
    It no longer makes the final decision.
    """
    if not ai_model: return {"eligible": [], "excluded": []}
    
    logger.info(f"AI Task 1: Extracting geographic entities from '{title[:40]}...'")
    text_snippet = text_content[:3000]
    
    prompt = f"""
    Analyze the eligible and excluded geographic locations for the following funding opportunity.

    Tasks:
    1.  Identify all specific countries, regions (e.g., "East Africa", "Sub-Saharan Africa", "MENA"), and global designators ("Global", "International", "Developing Countries") that are ELIGIBLE.
    2.  Identify any specific countries or regions that are EXPLICITLY EXCLUDED.

    Your response MUST be a valid JSON object with two keys: "eligible" and "excluded". Each key should contain a list of strings. If no locations are found for a key, provide an empty list. Do not add any text outside the JSON object.

    Example for a grant open to East Africa but not Somalia:
    {{
      "eligible": ["East Africa"],
      "excluded": ["Somalia"]
    }}
    
    Example for a grant for Nigeria only:
    {{
      "eligible": ["Nigeria"],
      "excluded": []
    }}

    Opportunity Title: {title}
    Opportunity Content: {text_snippet}
    """
    
    response_text = _call_gemini_with_retry(prompt)
    if response_text:
        json_match = re.search(r'\{.*\}', response_text, re.DOTALL)
        if json_match:
            try:
                data = json.loads(json_match.group(0))
                if 'eligible' not in data or 'excluded' not in data:
                    raise ValueError("JSON missing 'eligible' or 'excluded' keys.")
                logger.info(f"AI Geo Extraction found: Eligible: {data.get('eligible')}, Excluded: {data.get('excluded')}")
                return data
            except (json.JSONDecodeError, ValueError) as e:
                logger.error(f"Could not decode or validate geo JSON: {e}", exc_info=True)
                return {"eligible": [], "excluded": []}
        else:
            logger.error("No valid geo JSON object found in AI response.")
            return {"eligible": [], "excluded": []}
    return {"eligible": [], "excluded": []}


def get_enrichment_data(title, text_content):
    """AI Task 2: Extracts key data points and returns a structured JSON object."""
    if not ai_model:
        return {"focus_areas": [], "summary": "AI is not configured.", "funding_amount": "N/A", "funder": "N/A", "deadline": "N/A"}
    
    logger.info(f"AI Task 2: Enriching data for '{title[:40]}...'")
    text_snippet = text_content[:4000]

    # --- MODIFIED: Replaced the 'Deadline' instruction with your improved version ---
    prompt = f"""
    You are a data analyst. For the following funding opportunity, perform these tasks:
    1.  Focus Areas: From this list ONLY: {', '.join(config.VALID_FOCUS_AREAS)}, select the 2-3 MOST RELEVANT focus areas that best match the opportunity's primary objectives. Do not select more than 3 areas. Prioritize the most specific and directly applicable areas.
    2.  Funding Amount: Extract the specific funding amount or range (e.g., "$10,000", "up to €50,000"). If not clearly specified, state "Not Specified".
    3.  Funder: Identify the primary organization providing the funds (e.g., "Ford Foundation", "USAID"). If not clearly specified, state "Not Specified".
    4.  Deadline: Scrutinize the text for any mention of an application closing date or deadline.
        - If you find a specific date (e.g., "March 31, 2025", "24 April", "Closes on Thursday, Sep 5th"), extract it and format it STRICTLY as YYYY-MM-DD. Ignore times of day.
        - If the text explicitly states the deadline is "rolling", "ongoing", or reviewed "quarterly", your response for the deadline MUST be the string "Rolling".
        - If and ONLY IF no date or rolling deadline is mentioned anywhere, your response MUST be the string "Not Specified".
        - Prioritize finding a specific date over any other text.
    5.  Summary: Write a clean, one-paragraph summary for an NGO audience.

    YOUR RESPONSE MUST BE A VALID JSON OBJECT and nothing else. Do not include markdown fences like ```json.
    
    The JSON object must have these five keys: "focus_areas" (a list of strings), "funding_amount" (a string), "funder" (a string), "deadline" (a string), and "summary" (a string).

    Opportunity Title: {title}
    Opportunity Content: {text_snippet}
    """
    
    response_text = _call_gemini_with_retry(prompt)
    if response_text:
        json_match = re.search(r'\{.*\}', response_text, re.DOTALL)
        if json_match:
            try:
                analysis_result = json.loads(json_match.group(0))
                
                # Ensure we don't exceed 3 focus areas
                if len(analysis_result.get("focus_areas", [])) > 3:
                    analysis_result["focus_areas"] = analysis_result["focus_areas"][:3]
                    logger.info(f"Trimmed focus areas to 3 for '{title[:40]}...'")
                
                logger.info(f"AI Enrichment Complete for '{title[:40]}...'")
                return analysis_result
            except json.JSONDecodeError as e:
                logger.error(f"AI Enrichment Failed: Could not decode JSON for '{title[:40]}...'. Error: {e}", exc_info=True)
                return {"focus_areas": [], "summary": "AI returned malformed JSON.", "funding_amount": "Error", "funder": "Error", "deadline": "Error"}
        else:
            logger.error(f"AI Enrichment Failed: No JSON object in the response for '{title[:40]}...'")
            return {"focus_areas": [], "summary": "No JSON object in AI response.", "funding_amount": "Error", "funder": "Error", "deadline": "Error"}
    else:
        return {"focus_areas": [], "summary": "AI call failed after retries.", "funding_amount": "Error", "funder": "Error", "deadline": "Error"}