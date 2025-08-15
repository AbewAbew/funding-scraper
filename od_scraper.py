#od_scraper.py

import requests
from bs4 import BeautifulSoup
import time
from urllib.parse import urlsplit
import logging
from datetime import datetime, timezone
from dateutil.relativedelta import relativedelta
from dateutil.parser import parse as parse_date
import config
from utils import create_retry_session

logger = logging.getLogger(__name__)

HEADERS = {
    'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/91.0.4472.124 Safari/537.36'
}

def get_opportunity_links(session: requests.Session) -> list[str]:
    """
    Scrapes OD grants pages to get recent opportunity links.
    Filters out articles older than the `ARTICLE_CUTOFF_MONTHS`.
    """
    recent_links = []
    
    cutoff_date = datetime.now(timezone.utc) - relativedelta(months=config.ARTICLE_CUTOFF_MONTHS)
    logger.info(f"OD: Will only consider articles published after {cutoff_date.strftime('%Y-%m-%d')}.")

    base_url = "https://opportunitydesk.org/category/grants/"
    page_num = 1
    
    while True:
        current_page_url = f"{base_url}page/{page_num}/" if page_num > 1 else base_url
        logger.info(f"OD: Fetching links and dates from page {page_num}...")
        try:
            # *** FIX: Increased timeout for better resilience ***
            response = session.get(current_page_url, timeout=30)
            
            if response.status_code == 404:
                logger.info(f"OD: Reached the last page (404 Not Found at {current_page_url}). Stopping.")
                break
            
            response.raise_for_status()
            
            soup = BeautifulSoup(response.content, 'html.parser')
            articles = soup.find_all('article', class_='l-post')
            if not articles:
                logger.info("OD: No more articles found on the page. Ending link search.")
                break
                
            for article in articles:
                link_element = article.select_one('h2.post-title > a')
                date_element = article.select_one('time.post-date')

                if link_element and date_element and link_element.get('href'):
                    link = link_element.get('href')
                    date_str = date_element.get_text(strip=True)
                    
                    try:
                        pub_date = parse_date(date_str).replace(tzinfo=timezone.utc)
                        
                        if pub_date >= cutoff_date:
                            recent_links.append(link)
                        else:
                            logger.debug(f"OD: Discarding old article from {pub_date.strftime('%Y-%m-%d')}: {link}")

                    except (ValueError, TypeError) as e:
                        logger.warning(f"OD: Could not parse date '{date_str}' for link {link}. Skipping. Error: {e}")
                else:
                    logger.debug("OD: An article was found missing a link or date element.")
                    
            page_num += 1
            # *** FIX: Increased delay to be more polite to the server ***
            time.sleep(2.0)

        except requests.exceptions.RequestException as e:
            logger.error(f"OD: A network error occurred on page {page_num} after all retries.", exc_info=True)
            break
            
    return recent_links

def scrape_opportunity_details(url: str, session: requests.Session) -> dict | None:
    """
    Scrapes the title and full text content from a single opportunity detail page.
    """
    try:
        # *** FIX: Increased timeout for better resilience ***
        response = session.get(url, timeout=30)
        response.raise_for_status()
        soup = BeautifulSoup(response.content, 'html.parser')
        
        title_element = soup.find('h1', class_='entry-title')
        
        if title_element:
            title = title_element.get_text(strip=True)
        else:
            logger.warning(f"OD: H1 title not found for {url}. Generating from URL slug.")
            try:
                path = urlsplit(url).path
                slug = path.strip('/').split('/')[-1]
                title = slug.replace('-', ' ').title()
            except Exception:
                logger.error(f"OD: Could not generate title from URL slug for {url}.", exc_info=True)
                title = "Title Generation Failed"

        content_element = soup.find('div', class_='entry-content')
        full_text = content_element.get_text(separator=' ', strip=True) if content_element else ""

        if not full_text:
            logger.warning(f"OD: Failed to find content for {url}.")

        return {'title': title, 'link': url, 'source': 'Opportunity Desk', 'full_text': full_text}
        
    except requests.exceptions.RequestException as e:
        logger.error(f"OD: Error scraping detail page {url} after all retries.", exc_info=True)
        return None

def scrape_od(existing_links: set) -> list[dict]:
    """
    Main orchestrator for the Opportunity Desk scraper.
    """
    logger.info("--- Starting Scraper: Opportunity Desk ---")
    
    session = create_retry_session(use_proxy=True)  # OD scraper uses proxy
    # Don't override headers when using proxy
    if not session.proxies:
        session.headers.update(HEADERS)
    
    all_recent_links = get_opportunity_links(session)
    logger.info(f"OD: Found {len(all_recent_links)} recently published opportunities based on the date cutoff.")
    
    links_to_scrape = [link for link in all_recent_links if link not in existing_links]
    logger.info(f"OD: Of the recent links, {len(links_to_scrape)} are new to our database.")

    if config.SCRAPER_TEST_LIMIT and config.SCRAPER_TEST_LIMIT > 0:
        limit = min(len(links_to_scrape), config.SCRAPER_TEST_LIMIT)
        links_to_scrape = links_to_scrape[:limit]
        logger.info(f"OD: Applying test limit. Will scrape details for {len(links_to_scrape)} opportunities.")

    all_opportunities = []
    if not links_to_scrape:
        logger.info("OD: No new opportunity links to scrape.")
    else:
        logger.info(f"OD: Now scraping details for {len(links_to_scrape)} new opportunities...")
        for i, link in enumerate(links_to_scrape):
            logger.debug(f"OD: Scraping link {i+1}/{len(links_to_scrape)}...")
            details = scrape_opportunity_details(link, session)
            if details:
                all_opportunities.append(details)
            # *** FIX: Increased delay between detail page requests ***
            time.sleep(1.5)
            
    logger.info(f"--- Finished OD Scraper. Scraped details for {len(all_opportunities)} new opportunities. ---")
    return all_opportunities