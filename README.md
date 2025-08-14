

# Resilient, Self-Healing Funding Aggregator & AI Pipeline

This project is an automated, resilient, multi-stage data engineering pipeline that scrapes funding opportunities, analyzes them with AI, and stores them in a persistent cloud database. It is designed for high performance, fault tolerance, and efficiency. The system is **self-healing**, automatically retrying temporary network or API failures and isolating permanent data quality issues to ensure the pipeline never gets stuck.

The system's core is a three-stage process:
1.  **Maintenance:** Automatically purges old, expired opportunities from the final database to keep the dataset clean and relevant.
2.  **Collector:** Concurrently scrapes multiple websites for new funding opportunities. It intelligently filters out very old posts at the source, handles relative dates (e.g., "2 weeks ago"), and uses resilient retry mechanisms to overcome network errors.
3.  **Processor:** Fetches pending opportunities from a persistent cloud queue. It runs them through a sophisticated, two-step AI analysis pipeline that first filters for geographic relevance and then performs a full data enrichment using heavily optimized prompts. The processor intelligently handles different types of AI failures, proactively discards expired opportunities, and saves only clean, validated data to the final database.

This two-table, multi-stage architecture ensures that the expensive AI analysis is decoupled from the scraping process, making the system robust, re-processable, and cost-effective.

---

### Key Features

-   **Robust Three-Stage Pipeline Architecture:**
    -   **Maintenance Stage:** Automatically deletes long-expired opportunities at the start of each run.
    -   **Collector Stage:** Scrapes all sources, pre-filters by publication date, and saves raw data to a `raw_opportunities` queue. Fast, cheap, and isolated from downstream failures.
    -   **Processor Stage:** Independently works through the queue, performing all AI analysis and data validation.
-   **Intelligent, Multi-Tiered Error Handling:**
    -   **Scraper Retries:** Automatically retries failed web requests with exponential backoff to handle temporary server issues. Scrapers are also equipped with browser-like headers to avoid being blocked.
    -   **AI Retries:** Intelligently handles API rate limit errors by waiting for the duration recommended by the Google API.
    -   **Self-Healing for Temporary AI Failures:** If an AI call fails due to a temporary issue (e.g., rate limit, network blip), the opportunity is left in the queue to be **automatically retried on the next run**.
    -   **Isolation of Permanent AI Failures:** If the AI returns corrupted or malformed data (e.g., invalid JSON), the item is flagged with a unique `processed_ai_error` status and **permanently removed from the queue** to prevent the pipeline from getting stuck.
-   **Advanced Caching & Cost-Efficiency:**
    -   **Publication Date Filtering:** Prevents scraping and processing of articles that are too old to be relevant, saving significant time and API costs.
    -   **Proactive Expiration Filtering:** Prevents wasting AI credits by catching and discarding expired opportunities *during* the processing stage, before they are saved to the final database.
    -   **Scraping Cache:** Prevents re-scraping of any link ever seen by checking the `raw_opportunities` table.
    -   **Sophisticated Multi-Status Caching:** The `status` column in the raw data table now tracks multiple states (`pending_analysis`, `processed_relevant`, `processed_irrelevant`, `processed_expired`, `processed_ai_error`), providing granular control and preventing re-processing of any item, regardless of its outcome.
-   **Highly Accurate Geographic Filtering & Data Enrichment:**
    -   Leverages AI for initial geographic extraction, followed by a high-precision Python script that applies "Specifics over Generals" logic to eliminate false positives.
    -   **Enhanced AI Prompts:** The AI prompt for deadline extraction has been heavily optimized with specific instructions, examples, and keywords to dramatically improve accuracy.
    -   **Debuggability with Raw AI Output:** A new `raw_deadline_text` column in the final database stores the AI's exact output, making it trivial to diagnose parsing errors and verify prompt effectiveness.
    -   **Flexible Date Parsing:** Upgraded from rigid regex to `python-dateutil`, allowing the system to robustly parse various human-readable date formats provided by the AI.
-   **Persistent Cloud Database (Supabase/PostgreSQL):**
    -   Uses a professional-grade cloud PostgreSQL database.
    -   Enables data accessibility for other applications and team members.
    -   Uses a two-table schema (`raw_opportunities`, `processed_opportunities`) to power the resilient pipeline.

---

### Technology Stack

-   Python 3
-   **Supabase (PostgreSQL):** For the cloud-based, persistent database.
-   `requests` & `urllib3`: For making resilient, retrying HTTP requests.
-   `beautifulsoup4`: For parsing HTML.
-   `google-generativeai`: For interacting with the Gemini API.
-   `supabase-py`: Python client for interacting with the Supabase database.
-   `python-dotenv`: For managing secret API keys.
-   `python-dateutil`: For robustly parsing dates from various string formats.

---

### Project Structure

```
/funding_scraper/
│
├── .env                      # Stores all secret keys (Google, Supabase). DO NOT SHARE.
├── config.py                 # Central configuration for scrapers, AI, and logging.
├── main.py                   # Main orchestrator for the Maintenance, Collector, and Processor stages.
├── database_manager.py       # Manages all interactions with the Supabase cloud database.
├── ai_analyzer.py            # The "brain" with all AI-related functions and retry logic.
├── utils.py                  # Utility functions for creating Supabase clients and retry sessions.
│
├── gso_scraper.py            # Scraper for globalsouthopportunities.com.
├── ofy_scraper.py            # Scraper for opportunitiesforyouth.org.
├── od_scraper.py             # Scraper for opportunitydesk.org.
│
├── requirements.txt          # Lists all Python libraries needed for the project.
├── schema.sql                # SQL script to create the required tables in Supabase.
└── funding_scraper.log       # Detailed log file for monitoring and debugging.
```

---

### Setup and Installation

**1. Clone the Repository:**
   ```bash
   git clone <your-repo-url>
   cd funding_scraper
   ```

**2. Set up Supabase Project:**
   - Go to [supabase.com](https://supabase.com), create a free account, and set up a new project.
   - Navigate to the **SQL Editor** in your new project.
   - **Important:** Use the updated `schema.sql` file from this project. It contains the necessary columns, including the new `raw_deadline_text` field.
   - Copy the entire content of `schema.sql`, paste it into the editor, and click **RUN**. This will set up the `raw_opportunities` and `processed_opportunities` tables correctly.

**3. Set up the Python Environment:**
   ```bash
   # Create a virtual environment
   python -m venv venv

   # Activate it (Windows)
   venv\Scripts\activate
   # Or (macOS/Linux)
   source venv/bin/activate
   ```

**4. Install Dependencies:**
   Create a `requirements.txt` file with the following content:
   ```
   requests
   beautifulsoup4
   google-generativeai
   python-dotenv
   supabase
   python-dateutil
   pyinstaller
   ```
   Then run:
   ```bash
   pip install -r requirements.txt
   ```

**5. Configure your API Keys:**
   - In the project folder, create a file named exactly `.env`.
   - Add your keys to the file. **Get the `service_role` key from Supabase**, not the public `anon` key.

     ```.env
     GOOGLE_API_KEY="PASTE_YOUR_GOOGLE_API_KEY_HERE"
     SUPABASE_URL="PASTE_YOUR_SUPABASE_PROJECT_URL_HERE"
     SUPABASE_SERVICE_KEY="PASTE_YOUR_SUPABASE_SERVICE_ROLE_KEY_HERE"
     ```

### How to Run

Once setup is complete, run the `main.py` script from your activated virtual environment.

```bash
python main.py
```

The script will execute its three stages in order: Maintenance, Collector, and Processor. The final, clean data will be available in the `processed_opportunities` table in your Supabase project.