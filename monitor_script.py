import requests
from bs4 import BeautifulSoup
import json
import hashlib
import os
import sys
import time
import logging
import feedparser
from github import Github, GithubException
from urllib.parse import urljoin, urlparse

# --- Configuration ---
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')
REQUEST_TIMEOUT = 30  # seconds
USER_AGENT = "GitHubActions Sitemap & Content Monitor (https://github.com/ame-888/SiteMapMonitor)" # Customize this!
MAX_OUTPUT_LIST_ITEMS = 100 # Limit list length in output for brevity

# --- GitHub Actions Output Helper ---
def set_output(name, value):
    """Sets output for GitHub Actions."""
    # print(f"::set-output name={name}::{value}") # Old syntax
    with open(os.environ['GITHUB_OUTPUT'], 'a') as fh:
         print(f"{name}={value}", file=fh)

def format_output_list(items):
    """Formats a list for output, escaping newlines."""
    if not items:
        return ""
    # Just join items, replacing literal newlines within an item
    # Use the GitHub Actions multiline output format by joining with '\n' (escaped in bash later)
    formatted = "\\n".join([str(item).replace('\n', ' ').replace('\r', '') for item in items])
    return formatted

# --- State Handling ---
def load_state(filepath):
    """Loads state from a JSON file."""
    if os.path.exists(filepath):
        try:
            with open(filepath, 'r') as f:
                return json.load(f)
        except json.JSONDecodeError:
            logging.error(f"Error decoding JSON from state file: {filepath}")
            return {}
        except Exception as e:
            logging.error(f"Error loading state file {filepath}: {e}")
            return {}
    return {}

def save_state(filepath, state):
    """Saves state to a JSON file."""
    try:
        with open(filepath, 'w') as f:
            json.dump(state, f, indent=2)
        logging.info(f"State saved successfully to {filepath}")
    except Exception as e:
        logging.error(f"Error saving state file {filepath}: {e}")

# --- Helper Functions ---
def fetch_url(url, is_xml=False):
    """Fetches URL content with error handling."""
    headers = {'User-Agent': USER_AGENT}
    try:
        response = requests.get(url, headers=headers, timeout=REQUEST_TIMEOUT, allow_redirects=True)
        response.raise_for_status() # Raise HTTPError for bad responses (4xx or 5xx)
        content_type = response.headers.get('content-type', '').lower()

        # Check if content type matches expectation (helps catch HTML error pages for XML requests)
        if is_xml and 'xml' not in content_type:
            logging.warning(f"Expected XML content type but got {content_type} for URL: {url}")
            # Decide if you want to proceed or return None. Let's return None for XML.
            return None
        if not is_xml and 'html' not in content_type and 'text/plain' not in content_type:
             # Allow plain text for some assets maybe? Be more strict if needed.
             logging.debug(f"Unexpected content type {content_type} for non-XML URL: {url}")

        return response
    except requests.exceptions.RequestException as e:
        logging.warning(f"Could not fetch URL {url}: {e}")
        return None

def get_content_hash(text):
    """Calculates SHA256 hash of the given text."""
    if text is None:
        return None
    return hashlib.sha256(text.encode('utf-8')).hexdigest()

def normalize_url(url):
    """ Basic normalization """
    parsed = urlparse(url)
    # Remove trailing slash from path if it's not the root
    path = parsed.path.rstrip('/') if parsed.path != '/' else '/'
    # Rebuild without fragment, could add query param sorting/filtering later
    return f"{parsed.scheme}://{parsed.netloc}{path}"

# --- Monitoring Functions ---

def check_sitemap(sitemap_url, known_urls_set):
    """
    Recursively checks sitemap(s) and returns a list of new URLs found.
    Handles sitemap index files.
    """
    newly_discovered_urls = set()
    urls_to_process = {sitemap_url}
    processed_sitemaps = set()

    while urls_to_process:
        current_sitemap_url = urls_to_process.pop()
        if current_sitemap_url in processed_sitemaps:
            continue
        processed_sitemaps.add(current_sitemap_url)

        logging.info(f"Processing sitemap: {current_sitemap_url}")
        response = fetch_url(current_sitemap_url, is_xml=True)
        if not response:
            continue

        try:
            soup = BeautifulSoup(response.content, 'lxml-xml') # Use lxml-xml parser

            # Check if it's a sitemap index
            sitemap_tags = soup.find_all('sitemap')
            if sitemap_tags:
                logging.info(f"Found sitemap index: {current_sitemap_url}")
                for sitemap in sitemap_tags:
                    loc = sitemap.find('loc')
                    if loc and loc.text:
                        urls_to_process.add(loc.text.strip())
            else:
                # Process as a regular sitemap
                url_tags = soup.find_all('url')
                for url_tag in url_tags:
                    loc = url_tag.find('loc')
                    if loc and loc.text:
                        url = normalize_url(loc.text.strip())
                        if url not in known_urls_set:
                            newly_discovered_urls.add(url)

        except Exception as e:
            logging.error(f"Error parsing sitemap {current_sitemap_url}: {e}")

    logging.info(f"Sitemap check found {len(newly_discovered_urls)} new URLs.")
    return list(newly_discovered_urls)


def check_key_pages(pages_selectors, known_hashes_map):
    """
    Checks specific pages/selectors for content changes based on hashes.
    Returns a list of changed URLs/selectors and a map of current hashes.
    """
    changed_pages_list = []
    current_hashes_map = {}
    total_checked = 0

    for item in pages_selectors:
        try:
            url, selector = item.split('|', 1)
            url = url.strip()
            selector = selector.strip()
        except ValueError:
            logging.warning(f"Invalid format in key_pages_selectors: '{item}'. Skipping. Expected 'URL|Selector'.")
            continue

        total_checked += 1
        logging.info(f"Checking content for: {url} with selector: '{selector}'")
        response = fetch_url(url)
        if not response:
            continue

        try:
            soup = BeautifulSoup(response.text, 'lxml')
            target_element = soup.select_one(selector)

            if not target_element:
                logging.warning(f"Selector '{selector}' not found on page {url}")
                content_text = "" # Or handle differently? Maybe hash the whole body?
            else:
                 # Get text content, could use .prettify() for HTML structure hash
                content_text = target_element.get_text(separator=' ', strip=True)

            current_hash = get_content_hash(content_text)
            current_hashes_map[item] = current_hash # Store with combined key

            known_hash = known_hashes_map.get(item)

            if current_hash is not None and current_hash != known_hash:
                logging.info(f"Content change detected for: {item} (Hash: {known_hash} -> {current_hash})")
                changed_pages_list.append(item)
            elif known_hash is None and current_hash is not None:
                 logging.info(f"New page/selector tracked: {item}")
                 # Optionally treat initial discovery as a change? Let's not for now.

        except Exception as e:
            logging.error(f"Error parsing content for {url} with selector '{selector}': {e}")

    logging.info(f"Content check: {len(changed_pages_list)} changes found out of {total_checked} tracked items.")
    return changed_pages_list, current_hashes_map

def check_assets(page_urls, known_asset_map):
    """
    Checks specified pages for changes in linked JS/CSS assets.
    Returns a list of pages with changed assets and the current map of assets per page.
    """
    changed_assets_list = []
    current_asset_map = {}

    for page_url in page_urls:
        logging.info(f"Checking assets on page: {page_url}")
        response = fetch_url(page_url)
        if not response:
            current_asset_map[page_url] = [] # Store empty list if page fetch fails
            continue

        current_page_assets = set()
        try:
            soup = BeautifulSoup(response.text, 'lxml')
            # Find script tags with src attribute
            for script_tag in soup.find_all('script', src=True):
                src = urljoin(page_url, script_tag['src']) # Resolve relative URLs
                current_page_assets.add(normalize_url(src))
            # Find link tags for stylesheets
            for link_tag in soup.find_all('link', rel='stylesheet', href=True):
                href = urljoin(page_url, link_tag['href']) # Resolve relative URLs
                current_page_assets.add(normalize_url(href))

        except Exception as e:
            logging.error(f"Error parsing assets for {page_url}: {e}")

        current_asset_map[page_url] = sorted(list(current_page_assets)) # Store sorted list
        known_page_assets = set(known_asset_map.get(page_url, []))

        # Check for differences
        if current_page_assets != known_page_assets:
             new_assets = current_page_assets - known_page_assets
             removed_assets = known_page_assets - current_page_assets
             logging.info(f"Asset change detected for: {page_url} (+{len(new_assets)} / -{len(removed_assets)})")
             if new_assets or removed_assets: # Ensure there's an actual change
                 changed_assets_list.append(f"{page_url} (+{len(new_assets)} / -{len(removed_assets)})")

    logging.info(f"Asset check: {len(changed_assets_list)} pages with asset changes found.")
    return changed_assets_list, current_asset_map


def check_github_repos(repo_names, known_repo_state, github_client):
    """
    Checks GitHub repositories for new commits on the default branch.
    Returns a list of updated repos and the current map of repo SHAs.
    """
    if not github_client:
        logging.warning("GitHub client not initialized (no token?). Skipping GitHub checks.")
        return [], known_repo_state # Return unchanged state

    updated_repos_list = []
    current_repo_state = {}

    for repo_name in repo_names:
        logging.info(f"Checking GitHub repo: {repo_name}")
        try:
            repo = github_client.get_repo(repo_name)
            # Get the default branch (usually 'main' or 'master')
            default_branch = repo.get_branch(repo.default_branch)
            latest_commit_sha = default_branch.commit.sha
            current_repo_state[repo_name] = latest_commit_sha

            known_sha = known_repo_state.get(repo_name)

            if latest_commit_sha != known_sha:
                logging.info(f"New commit detected for repo: {repo_name} (SHA: {known_sha} -> {latest_commit_sha})")
                updated_repos_list.append(f"{repo_name} (New SHA: {latest_commit_sha[:7]})")
            elif known_sha is None:
                 logging.info(f"New repo tracked: {repo_name}")

        except GithubException as e:
            logging.error(f"GitHub API error checking repo {repo_name}: {e.status} {e.data}")
        except Exception as e:
            logging.error(f"Error checking GitHub repo {repo_name}: {e}")
        time.sleep(1) # Be nice to the API

    logging.info(f"GitHub check: {len(updated_repos_list)} repos with new commits found.")
    return updated_repos_list, current_repo_state


def check_research_feeds(feed_urls, known_feed_state):
    """
    Checks RSS/Atom feeds for new entries.
    Returns a list of new entry titles/links and the current map of latest entry IDs per feed.
    """
    new_feed_entries_list = []
    current_feed_state = {}

    for feed_url in feed_urls:
        logging.info(f"Checking feed: {feed_url}")
        parsed_feed = feedparser.parse(feed_url)

        if parsed_feed.bozo:
            logging.warning(f"Error parsing feed {feed_url}: {parsed_feed.bozo_exception}")
            current_feed_state[feed_url] = known_feed_state.get(feed_url) # Preserve old state on error
            continue

        if not parsed_feed.entries:
            logging.info(f"Feed {feed_url} has no entries.")
            current_feed_state[feed_url] = None # Or preserve old state? Let's reset if empty.
            continue

        # Try to get a stable ID, fallback to link or title+published
        latest_entry = parsed_feed.entries[0]
        latest_entry_id = latest_entry.get('id') or latest_entry.get('link') or f"{latest_entry.get('title')}_{latest_entry.get('published')}"
        current_feed_state[feed_url] = latest_entry_id

        known_latest_id = known_feed_state.get(feed_url)

        if latest_entry_id != known_latest_id:
            # Find all entries newer than the known one (simple approach: check all if ID changed)
            # More robust: Store timestamps or multiple IDs if needed.
            logging.info(f"New entries detected for feed: {feed_url}")
            count = 0
            # Iterate through entries to find where the known one was (or just list the newest few)
            temp_new_entries = []
            for entry in parsed_feed.entries:
                entry_id = entry.get('id') or entry.get('link') or f"{entry.get('title')}_{entry.get('published')}"
                if entry_id == known_latest_id:
                    break # Stop when we reach the previously known latest entry
                title = entry.get('title', 'No Title')
                link = entry.get('link', feed_url)
                temp_new_entries.append(f"{title} ({link})")
                count += 1
                if count >= MAX_OUTPUT_LIST_ITEMS: # Limit reported entries per feed
                     temp_new_entries.append("... and more")
                     break

            if not known_latest_id and parsed_feed.entries: # First time seeing this feed with entries
                 logging.info(f"Tracking new feed: {feed_url}. Latest entry: {latest_entry.get('title')}")
                 # Optionally report the latest entry on first check? Let's report changes only.
            elif temp_new_entries:
                 new_feed_entries_list.extend(temp_new_entries)

        time.sleep(0.5) # Be nice

    logging.info(f"Feed check: Found indications of {len(new_feed_entries_list)} new entries across feeds.")
    return new_feed_entries_list, current_feed_state


# --- Main Execution ---
if __name__ == "__main__":
    logging.info("Starting comprehensive monitor script...")

    # --- Get Configuration from Environment ---
    try:
        matrix_config_json = os.environ.get('MATRIX_CONFIG_JSON')
        if not matrix_config_json:
            sys.exit("Error: MATRIX_CONFIG_JSON environment variable not set.")
        config = json.loads(matrix_config_json)

        state_file = os.environ.get('STATE_FILE')
        if not state_file:
            sys.exit("Error: STATE_FILE environment variable not set.")

        github_token = os.environ.get('GITHUB_TOKEN', None)

    except json.JSONDecodeError:
        sys.exit("Error: Could not decode MATRIX_CONFIG_JSON.")
    except Exception as e:
         sys.exit(f"Error reading environment variables: {e}")

    domain = config.get("domain", "UnknownDomain")
    logging.info(f"--- CONFIGURATION for {domain} ---")
    logging.info(f"State File: {state_file}")
    logging.info(f"Sitemap URL: {config.get('sitemap_url')}")
    logging.info(f"Key Pages/Selectors: {len(config.get('key_pages_selectors', []))}")
    logging.info(f"Asset Monitor Pages: {len(config.get('asset_monitor_pages', []))}")
    logging.info(f"GitHub Repos: {len(config.get('github_repos', []))}")
    logging.info(f"Research Feeds: {len(config.get('research_feeds', []))}")
    logging.info(f"GitHub Token Provided: {'Yes' if github_token else 'No'}")
    logging.info("------------------------------")


    # --- Initialize GitHub Client ---
    g = None
    if github_token:
        try:
            g = Github(github_token)
            user = g.get_user()
            logging.info(f"GitHub client initialized. Rate limit: {g.get_rate_limit().core}")
        except Exception as e:
            logging.error(f"Failed to initialize GitHub client: {e}")
            g = None # Ensure client is None if init fails
    else:
        logging.warning("No GitHub token provided. GitHub repo checks will be skipped or rate limited.")
        # Optionally initialize without auth for public data only, but rate limits are very strict.
        # g = Github()


    # --- Load Previous State ---
    current_state = load_state(state_file)
    new_state = { # Initialize structure for the new state
        "sitemap_urls": current_state.get("sitemap_urls", []),
        "page_content_hashes": current_state.get("page_content_hashes", {}),
        "asset_map": current_state.get("asset_map", {}),
        "github_repo_state": current_state.get("github_repo_state", {}),
        "feed_state": current_state.get("feed_state", {}),
    }

    # --- Run Monitors ---
    results = {
        "new_urls": [],
        "changed_pages": [],
        "changed_assets": [],
        "updated_repos": [],
        "new_feed_entries": [],
    }

    # 1. Sitemap Check
    if config.get("sitemap_url"):
        logging.info("--- Running Sitemap Check ---")
        known_urls_set = set(new_state["sitemap_urls"])
        results["new_urls"] = check_sitemap(config["sitemap_url"], known_urls_set)
        # Update state with ALL urls (old + new)
        new_state["sitemap_urls"] = sorted(list(known_urls_set.union(results["new_urls"])))
        logging.info("-----------------------------")
        time.sleep(1)

    # 2. Key Pages Content Check
    if config.get("key_pages_selectors"):
        logging.info("--- Running Key Pages Content Check ---")
        results["changed_pages"], new_state["page_content_hashes"] = check_key_pages(
            config["key_pages_selectors"],
            new_state["page_content_hashes"] # Pass current known hashes
        )
        logging.info("------------------------------------")
        time.sleep(1)

    # 3. Asset Check
    if config.get("asset_monitor_pages"):
        logging.info("--- Running Asset Check ---")
        results["changed_assets"], new_state["asset_map"] = check_assets(
            config["asset_monitor_pages"],
            new_state["asset_map"] # Pass current known assets
        )
        logging.info("--------------------------")
        time.sleep(1)

    # 4. GitHub Repo Check
    if config.get("github_repos"):
        logging.info("--- Running GitHub Repo Check ---")
        results["updated_repos"], new_state["github_repo_state"] = check_github_repos(
            config["github_repos"],
            new_state["github_repo_state"], # Pass current known SHAs
            g # Pass the initialized GitHub client
        )
        logging.info("------------------------------")
        # No extra sleep needed, check_github_repos has internal sleep

    # 5. Research Feed Check
    if config.get("research_feeds"):
        logging.info("--- Running Research Feed Check ---")
        results["new_feed_entries"], new_state["feed_state"] = check_research_feeds(
            config["research_feeds"],
            new_state["feed_state"] # Pass current known feed states
        )
        logging.info("--------------------------------")
        # No extra sleep needed, check_research_feeds has internal sleep


    # --- Save Updated State ---
    save_state(state_file, new_state)

    # --- Set Outputs for GitHub Actions ---
    logging.info("--- Setting GitHub Actions Outputs ---")
    set_output("new_urls_count", len(results["new_urls"]))
    set_output("new_urls_list", format_output_list(results["new_urls"]))

    set_output("changed_pages_count", len(results["changed_pages"]))
    set_output("changed_pages_list", format_output_list(results["changed_pages"]))

    set_output("changed_assets_count", len(results["changed_assets"]))
    set_output("changed_assets_list", format_output_list(results["changed_assets"]))

    set_output("updated_repos_count", len(results["updated_repos"]))
    set_output("updated_repos_list", format_output_list(results["updated_repos"]))

    set_output("new_feed_entries_count", len(results["new_feed_entries"]))
    set_output("new_feed_entries_list", format_output_list(results["new_feed_entries"]))
    logging.info("------------------------------------")

    logging.info("Monitor script finished.")
