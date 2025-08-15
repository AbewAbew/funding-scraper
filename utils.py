#utils.py
import os
import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry
from supabase import create_client, Client
from dotenv import load_dotenv
import logging

logger = logging.getLogger(__name__)

# --- Supabase Client Setup ---
# Load environment variables from .env file
load_dotenv()

# Get Supabase credentials from environment variables
SUPABASE_URL = os.getenv("SUPABASE_URL")
SUPABASE_KEY = os.getenv("SUPABASE_SERVICE_KEY")

def init_supabase_client():
    """
    Initializes and returns the Supabase client if credentials are available.
    Returns None if credentials are not found.
    """
    if SUPABASE_URL and SUPABASE_KEY:
        try:
            logger.info("Initializing Supabase client...")
            supabase: Client = create_client(SUPABASE_URL, SUPABASE_KEY)
            logger.info("Supabase client initialized successfully.")
            return supabase
        except Exception as e:
            logger.critical(f"Failed to initialize Supabase client: {e}", exc_info=True)
            return None
    else:
        logger.critical("Supabase URL or Key not found in .env file. Database functions will be disabled.")
        return None

# --- Resilient Request Session ---
def create_retry_session(
    retries=3,
    backoff_factor=0.5,
    status_forcelist=(500, 502, 503, 504),
    session=None,
):
    """
    Creates a requests.Session with a robust, configurable retry strategy.

    This makes web requests resilient to temporary network issues or server-side errors.

    Args:
        retries (int): Total number of retries to attempt.
        backoff_factor (float): A delay factor for retries. The sleep time will be:
                                {backoff factor} * (2 ** ({number of total retries} - 1)).
                                For example, 0.5 -> 0.5s, 1s, 2s, 4s...
        status_forcelist (tuple): A tuple of HTTP status codes to force a retry on.
        session (requests.Session, optional): An existing session to mount the adapter to.
                                              If None, a new session is created.

    Returns:
        requests.Session: A session object configured with the retry adapter.
    """
    session = session or requests.Session()
    
    # Check if running in GitHub Actions and add better headers
    if os.getenv('GITHUB_ACTIONS'):
        # Add realistic browser headers to avoid blocking
        session.headers.update({
            'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36',
            'Accept': 'text/html,application/xhtml+xml,application/xml;q=0.9,image/webp,*/*;q=0.8',
            'Accept-Language': 'en-US,en;q=0.5',
            'Accept-Encoding': 'gzip, deflate',
            'Connection': 'keep-alive',
            'Upgrade-Insecure-Requests': '1'
        })
        logger.info("GitHub Actions detected: Using enhanced browser headers.")
    
    # Define the retry strategy using urllib3's Retry class
    retry_strategy = Retry(
        total=retries,
        read=retries,
        connect=retries,
        backoff_factor=backoff_factor,
        status_forcelist=status_forcelist,
        # 'method_whitelist' is deprecated, use 'allowed_methods' instead
        allowed_methods=["HEAD", "GET", "POST", "OPTIONS"]
    )
    
    # Create an HTTP adapter with the retry strategy
    adapter = HTTPAdapter(max_retries=retry_strategy)
    
    # Mount the adapter to the session for both http and https protocols
    session.mount("http://", adapter)
    session.mount("https://", adapter)
    
    logger.info(f"Created a requests session with retry strategy: {retries} retries, {backoff_factor}s backoff.")
    
    return session