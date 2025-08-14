import requests
from bs4 import BeautifulSoup
import time
import re
import logging
from datetime import datetime, timezone
from dateutil.relativedelta import relativedelta
from dateutil.parser import parse as parse_date
import config
from utils import create_retry_session

logger = logging.getLogger(__name__)

# --- NEW: Define a standard User-Agent header ---
HEADERS = {
    'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/91.0.4472.124 Safari/537.36'
}

def get_fresh_nonce(session: requests.Session) -> str | None:
    """
    Fetches the main grants page to extract a fresh 'blog_feed_nonce'.
    """
    try:
        logger.info("OFY: Fetching main page to get a fresh security nonce...")
        page_url = "https://opportunitiesforyouth.org/category/grants/"
        response = session.get(page_url, timeout=30)
        response.raise_for_status()
        
        match = re.search(r'"blog_feed_nonce":"(.*?)"', response.text)
        if match:
            nonce = match.group(1)
            logger.info(f"OFY: Successfully found new nonce: {nonce}")
            return nonce
        else:
            logger.critical("OFY: Could not find the nonce on the page. Scraper cannot proceed.")
            return None
    except requests.exceptions.RequestException:
        logger.critical("OFY: Failed to fetch page to get nonce after all retries. Scraper will fail.", exc_info=True)
        return None

def get_all_ofy_links(nonce: str, session: requests.Session) -> list[str]:
    """
    Uses AJAX POST requests to get recent opportunity links.
    Filters out articles older than the `ARTICLE_CUTOFF_MONTHS`.
    """
    recent_links = []
    
    cutoff_date = datetime.now(timezone.utc) - relativedelta(months=config.ARTICLE_CUTOFF_MONTHS)
    logger.info(f"OFY: Will only consider articles published after {cutoff_date.strftime('%Y-%m-%d')}.")

    ajax_url = "https://opportunitiesforyouth.org/wp-admin/admin-ajax.php"
    payload = {
        'action': 'extra_blog_feed_get_content', 'et_load_builder_modules': '1', 'blog_feed_nonce': nonce,
        'to_page': '1', 'posts_per_page': '12', 'order': 'desc', 'orderby': 'date', 'categories': '5',
        'show_featured_image': '1', 'blog_feed_module_type': 'masonry', 'et_column_type': '', 'show_author': '1',
        'show_categories': '1', 'show_date': '1', 'show_rating': '1', 'show_more': '1', 'show_comments': '1',
        'date_format': 'M+j,+Y', 'content_length': 'excerpt', 'hover_overlay_icon': '', 'use_tax_query': '1',
        'tax_query[0][taxonomy]': 'category', 'tax_query[0][terms][]': 'grants', 'tax_query[0][field]': 'slug',
        'tax_query[0][operator]': 'IN', 'tax_query[0][include_children]': 'true'
    }
    page_num = 1
    
    while True:
        logger.info(f"OFY: Requesting data for page {page_num}...")
        payload['to_page'] = str(page_num)
        try:
            response = session.post(ajax_url, data=payload, timeout=30)
            response.raise_for_status()
            html_content = response.text
            
            if not html_content.strip():
                logger.info("OFY: Received empty response, indicating the end of pages.")
                break
                
            soup = BeautifulSoup(html_content, 'html.parser')
            articles = soup.find_all('article')
            if not articles:
                logger.info("OFY: No more <article> tags found. Ending link search.")
                break
            
            for article in articles:
                date_element = article.find('span', class_='updated')
                link_element = article.find('a', class_='read-more-button')
                
                if link_element and date_element and link_element.get('href'):
                    link = link_element.get('href')
                    date_str = date_element.get_text(strip=True)
                    cleaned_date_str = date_str.replace('+', ' ')
                    try:
                        pub_date = parse_date(cleaned_date_str).replace(tzinfo=timezone.utc)
                        if pub_date >= cutoff_date:
                            recent_links.append(link)
                        else:
                            logger.debug(f"OFY: Discarding old article from {pub_date.strftime('%Y-%m-%d')}: {link}")
                    except (ValueError, TypeError) as e:
                        logger.warning(f"OFY: Could not parse date '{date_str}' for link {link}. Skipping. Error: {e}")
                else:
                    logger.debug("OFY: An article was found missing a link or date element, skipping.")

            page_num += 1
            time.sleep(2.0)
            
        except requests.exceptions.RequestException as e:
            logger.error(f"OFY: Error making POST request for page {page_num} after all retries.", exc_info=True)
            break
            
    return recent_links

def scrape_opportunity_details(url: str, session: requests.Session) -> dict | None:
    """
    Scrapes the title and full text content from a single opportunity detail page.
    """
    try:
        response = session.get(url, timeout=30)
        response.raise_for_status()
        soup = BeautifulSoup(response.content, 'html.parser')
        
        title_element = soup.find('h1', class_='entry-title')
        title = title_element.get_text(strip=True) if title_element else "Title not found"
        
        if title == "Title not found":
            logger.warning(f"OFY: Failed to find title for {url}. Skipping.")
            return None

        content_element = soup.find('div', class_='entry-content')
        full_text = content_element.get_text(separator=' ', strip=True) if content_element else ""

        if not full_text:
            logger.warning(f"OFY: Failed to find content for {url}.")

        return {'title': title, 'link': url, 'source': 'Opportunities For Youth', 'full_text': full_text}
        
    except requests.exceptions.RequestException as e:
        logger.error(f"OFY: Error scraping detail page {url} after all retries.", exc_info=True)
        return None

def scrape_ofy(existing_links: set) -> list[dict]:
    """
    Main orchestrator for the OFY scraper.
    """
    logger.info("--- Starting Scraper: Opportunities For Youth ---")
    
    session = create_retry_session()
    
    # --- MODIFIED: Apply the User-Agent header to the session ---
    session.headers.update(HEADERS)
    
    fresh_nonce = get_fresh_nonce(session)
    if not fresh_nonce:
        logger.error("--- Finished OFY Scraper. Could not obtain nonce, so 0 opportunities were found. ---")
        return []
        
    all_recent_links = get_all_ofy_links(fresh_nonce, session)
    logger.info(f"OFY: Found {len(all_recent_links)} recently published opportunities based on the date cutoff.")
    
    links_to_scrape = [link for link in all_recent_links if link not in existing_links]
    logger.info(f"OFY: Of the recent links, {len(links_to_scrape)} are new to our database.")

    if config.SCRAPER_TEST_LIMIT and config.SCRAPER_TEST_LIMIT > 0:
        limit = min(len(links_to_scrape), config.SCRAPER_TEST_LIMIT)
        links_to_scrape = links_to_scrape[:limit]
        logger.info(f"OFY: Applying test limit. Will scrape details for {len(links_to_scrape)} opportunities.")

    all_opportunities = []
    if not links_to_scrape:
        logger.info("OFY: No new opportunity links to scrape.")
    else:
        logger.info(f"OFY: Now scraping details for {len(links_to_scrape)} new opportunities...")
        for i, link in enumerate(links_to_scrape):
            logger.debug(f"OFY: Scraping link {i+1}/{len(links_to_scrape)}...")
            details = scrape_opportunity_details(link, session)
            if details:
                all_opportunities.append(details)
            time.sleep(1.0)

    logger.info(f"--- Finished OFY Scraper. Scraped details for {len(all_opportunities)} new opportunities. ---")
    return all_opportunities