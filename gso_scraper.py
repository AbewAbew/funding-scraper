import requests
from bs4 import BeautifulSoup
import time
import logging
import re  # <-- NEW: Import regular expressions
from datetime import datetime, timezone, timedelta
from dateutil.relativedelta import relativedelta
from dateutil.parser import parse as parse_date
import config
from utils import create_retry_session

logger = logging.getLogger(__name__)


# --- NEW HELPER FUNCTION TO PARSE RELATIVE AND ABSOLUTE DATES ---
def parse_flexible_date(date_str: str) -> datetime | None:
    """
    Parses a date string that can be either absolute (e.g., "July 20, 2025")
    or relative (e.g., "14 hours ago", "2 weeks ago").

    Returns a timezone-aware datetime object on success, or None on failure.
    """
    date_str = date_str.lower().strip()
    now = datetime.now(timezone.utc)

    # Check for relative time patterns like "X unit(s) ago"
    match = re.match(r'(\d+)\s+(hour|day|week|month)s?\s+ago', date_str)
    if match:
        quantity = int(match.group(1))
        unit = match.group(2)
        
        if unit == 'hour':
            return now - timedelta(hours=quantity)
        elif unit == 'day':
            return now - timedelta(days=quantity)
        elif unit == 'week':
            return now - timedelta(weeks=quantity)
        elif unit == 'month':
            # relativedelta is better for months
            return now - relativedelta(months=quantity)

    # If no relative pattern matched, try the standard parser
    try:
        return parse_date(date_str).replace(tzinfo=timezone.utc)
    except (ValueError, TypeError):
        logger.warning(f"GSO: Could not parse date '{date_str}' using any known format.")
        return None


def get_opportunity_links(session: requests.Session) -> list[str]:
    """
    Scrapes GSO funding category pages to get recent opportunity links.

    This function now uses a flexible date parser to handle both absolute and
    relative dates, and filters out any that are older than the
    `ARTICLE_CUTOFF_MONTHS` setting in config.py.
    """
    recent_links = []
    
    cutoff_date = datetime.now(timezone.utc) - relativedelta(months=config.ARTICLE_CUTOFF_MONTHS)
    logger.info(f"GSO: Will only consider articles published after {cutoff_date.strftime('%Y-%m-%d')}.")

    current_page_url = "https://www.globalsouthopportunities.com/category/funding/"
    page_num = 1
    
    while current_page_url:
        logger.info(f"GSO: Fetching links and dates from page {page_num}...")
        try:
            response = session.get(current_page_url, timeout=15)
            response.raise_for_status()
            soup = BeautifulSoup(response.content, 'html.parser')
            
            posts_container = soup.find('ul', id='posts-container')
            if not posts_container:
                logger.warning(f"GSO: 'posts-container' not found on page {page_num}. Ending link search.")
                break
            
            posts = posts_container.find_all('li', class_='post-item')
            if not posts:
                logger.warning(f"GSO: No 'post-item' list items found on page {page_num}. Ending link search.")
                break

            for post in posts:
                link_element = post.find('a', class_='more-link')
                date_element = post.find('span', class_='date')

                if link_element and date_element and link_element.get('href'):
                    link = link_element.get('href')
                    date_str = date_element.get_text(strip=True)
                    
                    # --- MODIFIED LOGIC ---
                    # Use the new flexible parser. It handles errors internally.
                    pub_date = parse_flexible_date(date_str)
                    
                    if pub_date:
                        # The core filtering logic
                        if pub_date >= cutoff_date:
                            recent_links.append(link)
                        else:
                            # This post is too old, log it for debugging
                            logger.debug(f"GSO: Discarding old article from {pub_date.strftime('%Y-%m-%d')}: {link}")
                    # If pub_date is None, a warning was already logged by the parser
                
                else:
                    logger.debug("GSO: A post item was found missing a link or date element.")

            next_page_element = soup.select_one('span.last-page > a')
            if next_page_element and next_page_element.get('href'):
                current_page_url = next_page_element.get('href')
                page_num += 1
                time.sleep(1)
            else:
                current_page_url = None
                
        except requests.exceptions.RequestException as e:
            logger.error(f"GSO: A network error occurred while fetching {current_page_url} after all retries.", exc_info=True)
            break
            
    return recent_links

def scrape_opportunity_details(url: str, session: requests.Session) -> dict | None:
    """
    Scrapes the title and full text content from a single opportunity detail page.
    """
    try:
        response = session.get(url, timeout=15)
        response.raise_for_status()
        soup = BeautifulSoup(response.content, 'html.parser')
        
        title_element = soup.find('h1', class_='entry-title')
        title = title_element.get_text(strip=True) if title_element else "Title not found"
        
        if title == "Title not found":
            logger.warning(f"GSO: Failed to find title for {url}. Skipping.")
            return None
        
        content_element = soup.find('div', class_='entry-content')
        full_text = content_element.get_text(separator=' ', strip=True) if content_element else ""
        
        if not full_text:
            logger.warning(f"GSO: Failed to find content for {url}.")

        return {'title': title, 'link': url, 'source': 'Global South Opportunities', 'full_text': full_text}
        
    except requests.exceptions.RequestException as e:
        logger.error(f"GSO: Error scraping detail page {url} after all retries.", exc_info=True)
        return None

def scrape_gso(existing_links: set) -> list[dict]:
    """
    Main orchestrator for the GSO scraper.
    Filters links by publication date and against an existing link set.
    """
    logger.info("--- Starting Scraper: Global South Opportunities ---")
    
    session = create_retry_session()
    
    all_recent_links = get_opportunity_links(session)
    logger.info(f"GSO: Found {len(all_recent_links)} recently published opportunities based on the date cutoff.")
    
    links_to_scrape = [link for link in all_recent_links if link not in existing_links]
    logger.info(f"GSO: Of the recent links, {len(links_to_scrape)} are new to our database.")

    if config.SCRAPER_TEST_LIMIT and config.SCRAPER_TEST_LIMIT > 0:
        limit = min(len(links_to_scrape), config.SCRAPER_TEST_LIMIT)
        links_to_scrape = links_to_scrape[:limit]
        logger.info(f"GSO: Applying test limit. Will scrape details for {len(links_to_scrape)} opportunities.")

    all_opportunities = []
    if not links_to_scrape:
        logger.info("GSO: No new opportunity links to scrape.")
    else:
        logger.info(f"GSO: Now scraping details for {len(links_to_scrape)} new opportunities...")
        for i, link in enumerate(links_to_scrape):
            logger.debug(f"GSO: Scraping link {i+1}/{len(links_to_scrape)}...")
            details = scrape_opportunity_details(link, session)
            if details:
                all_opportunities.append(details)
            time.sleep(0.5)
            
    logger.info(f"--- Finished GSO Scraper. Scraped details for {len(all_opportunities)} new opportunities. ---")
    return all_opportunities