import os
import requests # For fetching web pages
from bs4 import BeautifulSoup # For reading sitemap (XML/HTML) files
import sys
import time # To add a small delay

# --- Configuration ---
# Get settings from the environment variables set in the workflow file
SITEMAP_URL = os.environ.get("SITEMAP_URL")
KNOWN_URLS_FILE = os.environ.get("KNOWN_URLS_FILE", "known_urls.txt") # Default filename if not set

# <<< --- Polite User Agent --- >>>
# Identify our script politely to the website owner (optional but good practice)
# You can change 'ame-888/SiteMapMonitor' to your actual GitHub username and repo name
USER_AGENT = "GitHubActionsSitemapMonitor/1.0 (+https://github.com/your_username/your_repo)"

# --- Helper Functions ---

def fetch_sitemap_urls(url):
    """
    Fetches URLs from a given sitemap URL.
    Handles both single sitemaps and sitemap index files.
    Returns a set (unique list) of URLs found.
    """
    print(f"Attempting to fetch sitemap: {url}")
    urls_found = set()
    headers = {'User-Agent': USER_AGENT}
    try:
        # Wait a tiny bit before making the request
        time.sleep(1)
        response = requests.get(url, headers=headers, timeout=45) # Allow 45 seconds to get response
        response.raise_for_status() # Check if the request was successful (status code 200 OK)
        print(f"Successfully fetched: {url} (Status: {response.status_code})")

        # Use 'lxml' for robust XML parsing
        soup = BeautifulSoup(response.content, 'xml')

        # Check if it's a Sitemap Index file (points to other sitemaps)
        sitemap_tags = soup.find_all('sitemap')
        if sitemap_tags:
            print(f"Detected Sitemap Index file. Processing sub-sitemaps...")
            for sitemap in sitemap_tags:
                loc = sitemap.find('loc') # Find the URL of the sub-sitemap
                if loc and loc.text:
                    sub_sitemap_url = loc.text.strip()
                    print(f"--- Found sub-sitemap: {sub_sitemap_url}")
                    # Recursively call this function to get URLs from the sub-sitemap
                    urls_found.update(fetch_sitemap_urls(sub_sitemap_url))
                else:
                     print(f"Warning: Found a <sitemap> tag without a <loc> URL inside.")

        # Otherwise, assume it's a regular Sitemap file (contains page URLs)
        else:
            url_tags = soup.find_all('url')
            print(f"Detected regular sitemap file. Processing {len(url_tags)} <url> tags...")
            count = 0
            for url_tag in url_tags:
                loc = url_tag.find('loc') # Find the actual page URL
                if loc and loc.text:
                    page_url = loc.text.strip()
                    urls_found.add(page_url)
                    count += 1
                else:
                    print(f"Warning: Found a <url> tag without a <loc> URL inside.")
            print(f"--- Found {count} URLs in this sitemap.")

    except requests.exceptions.Timeout:
        print(f"Error: Request timed out while fetching {url}", file=sys.stderr)
    except requests.exceptions.RequestException as e:
        print(f"Error: Failed to fetch {url}. Reason: {e}", file=sys.stderr)
    except Exception as e:
        # Catch other potential errors during parsing
        print(f"Error: Failed to parse {url}. Reason: {e}", file=sys.stderr)

    return urls_found

def load_known_urls(filename):
    """Loads the list of URLs we found during the previous run."""
    print(f"Loading previously known URLs from file: {filename}")
    if not os.path.exists(filename):
        print("Known URLs file not found. Assuming this is the first run.")
        return set() # Return an empty set if the file doesn't exist yet
    try:
        with open(filename, 'r', encoding='utf-8') as f:
            # Read each line, strip whitespace, and ignore empty lines
            known_set = set(line.strip() for line in f if line.strip())
            print(f"Loaded {len(known_set)} known URLs.")
            return known_set
    except Exception as e:
        print(f"Error: Could not read known URLs file '{filename}'. Reason: {e}", file=sys.stderr)
        # In case of error, return empty set to avoid losing data if fetch works
        return set()

def save_known_urls(filename, urls_to_save):
    """Saves the current list of URLs to the file for the next run."""
    print(f"Saving {len(urls_to_save)} URLs to file: {filename}")
    try:
        # Sort the URLs before saving for consistency
        sorted_urls = sorted(list(urls_to_save))
        with open(filename, 'w', encoding='utf-8') as f:
            for url in sorted_urls:
                f.write(url + '\n')
        print("Successfully saved known URLs.")
    except Exception as e:
        print(f"Error: Could not write known URLs file '{filename}'. Reason: {e}", file=sys.stderr)

# --- Main Execution Logic ---
if __name__ == "__main__":
    print("\n--- Starting Sitemap Monitor Script ---")

    if not SITEMAP_URL:
        print("Error: SITEMAP_URL environment variable is not set. Cannot proceed.", file=sys.stderr)
        sys.exit(1) # Exit with an error code

    # 1. Load old URLs
    known_urls = load_known_urls(KNOWN_URLS_FILE)

    # 2. Fetch current URLs
    print(f"\nFetching current URLs from entry point: {SITEMAP_URL}")
    current_urls = fetch_sitemap_urls(SITEMAP_URL)

    if not current_urls:
        print("\nWarning: Failed to fetch any URLs from the sitemap(s).", file=sys.stderr)
        # If fetching fails completely, we should NOT wipe the known URLs.
        # We'll skip the comparison and saving steps to preserve the last known state.
        print("Skipping comparison and saving to avoid data loss.")
        new_urls_list_output = ""
        new_urls_count_output = 0
    else:
        print(f"\nSuccessfully fetched a total of {len(current_urls)} unique URLs.")
        # 3. Compare: Find URLs in `current_urls` that are NOT in `known_urls`
        new_urls = current_urls - known_urls
        new_urls_count = len(new_urls)
        print(f"\nComparison complete. Found {new_urls_count} new URL(s).")

        # Prepare output for GitHub Actions (used in later steps like notification and commit)
        new_urls_count_output = new_urls_count

        if new_urls_count > 0:
            print("\nNew URLs found:")
            # Sort for readability
            sorted_new_urls = sorted(list(new_urls))
            for url in sorted_new_urls:
                print(f"- {url}")

            # Limit the list length for the notification message to avoid being too long
            max_urls_in_notification = 50
            limited_new_urls = sorted_new_urls[:max_urls_in_notification]
            new_urls_list_output = "\n".join(limited_new_urls)
            if new_urls_count > max_urls_in_notification:
                new_urls_list_output += f"\n...and {new_urls_count - max_urls_in_notification} more."

            # Escape special characters for the GitHub Actions output variable
            new_urls_list_output = new_urls_list_output.replace('%', '%25').replace('\n', '%0A').replace('\r', '%0D')

            # 4. Save the *complete current* list for the next run
            save_known_urls(KNOWN_URLS_FILE, current_urls)
        else:
            print("\nNo new URLs detected since the last check.")
            new_urls_list_output = ""
            # Save the list even if no changes, to ensure the file exists if it was the first run
            if not os.path.exists(KNOWN_URLS_FILE):
                 print(f"Saving initial URL list to {KNOWN_URLS_FILE}")
                 save_known_urls(KNOWN_URLS_FILE, current_urls)


    # Set output variables for GitHub Actions workflow
    # These lines communicate results back to the .yml file
    print(f"\nSetting GitHub Actions outputs...")
    print(f"::set-output name=new_urls_count::{new_urls_count_output}")
    print(f"::set-output name=new_urls_list::{new_urls_list_output}")

    print("\n--- Sitemap Monitor Script Finished ---")
