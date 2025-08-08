import requests
import json
import argparse
import asyncio
import logging
import sqlite3
import re
from datetime import datetime, timedelta
from bs4 import BeautifulSoup
from requests_html import HTMLSession
from facebook_scraper import get_profile
import tweepy

# Twitter API Credentials
TWITTER_BEARER_TOKEN = "AAAAAAAAAAAAAAAAAAAAAGHR3QEAAAAA%2BLrGJpHxkiJRo%2FK6XEhN2XLZ638%3D91rzjAxBwjBo3cezSrD7xGh136Xjk5WcrGSUaMHDMgqMThTdRy"
TWITTER_API_KEY = "PhoTxXyC5MSWKkxexBycP0shn"
TWITTER_API_SECRET = "Pi1LKHmUBX9HSQQjZA0UiBCillstwraoJqHj14tAhVVX6gEv43"
TWITTER_ACCESS_TOKEN = "1950983510103384064-lfmrQdsd1jnF085QuSezkIWtYBAj3G"
TWITTER_ACCESS_TOKEN_SECRET = "4iuHUGJfjFXdMVrJoEw3zAtQQxtuDgGCXU0sKqUj0cZmu"

# Set up logging
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# Configuration
class Config:
    DATABASE_URL = "sqlite:///./osint.db"
    CACHE_EXPIRE_HOURS = 6
    DATA_RETENTION_DAYS = 30
    USER_AGENTS = [
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/91.0.4472.124 Safari/537.36",
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/14.1.1 Safari/605.1.15",
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:89.0) Gecko/20100101 Firefox/89.0"
    ]
    PROXY_LIST = []  # Add proxies if needed

# Database setup
def init_db():
    try:
        conn = sqlite3.connect(Config.DATABASE_URL.replace("sqlite:///", ""))
        cursor = conn.cursor()
        cursor.execute("""
        CREATE TABLE IF NOT EXISTS social_profiles (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            platform TEXT NOT NULL,
            username TEXT NOT NULL,
            scraped_at TIMESTAMP NOT NULL,
            raw_data TEXT NOT NULL,
            metrics TEXT NOT NULL,
            UNIQUE(platform, username)
        )
        """)
        conn.commit()
        conn.close()
        logger.info("Database initialized successfully")
    except Exception as e:
        logger.error(f"Error initializing database: {str(e)}")
        raise

# Data Normalizer
class DataNormalizer:
    def normalize_twitter_data(self, username: str, raw_data: dict) -> dict:
        # Calculate engagement rate
        total_engagement = sum(
            (tweet.get("likes_count", 0) + 
             tweet.get("retweets_count", 0) + 
             tweet.get("replies_count", 0))
            for tweet in raw_data.get("tweets", [])
        )
        followers = raw_data.get("followers_count", 1)
        engagement_rate = (total_engagement / (len(raw_data.get("tweets", [1])) * followers)) * 100 if followers > 0 else 0
        
        metrics = {
            "followers": followers,
            "following": raw_data.get("following_count", 0),
            "engagement_rate": round(engagement_rate, 2)
        }
        
        content_metadata = {
            "bio": raw_data.get("bio", ""),
            "location": raw_data.get("location", ""),
            "join_date": raw_data.get("join_date", ""),
            "tweets": [
                {
                    "text": tweet.get("tweet", ""),
                    "time": tweet.get("datetime", ""),
                    "likes": tweet.get("likes_count", 0)
                }
                for tweet in raw_data.get("tweets", [])
            ],
            "metrics": metrics
        }
        
        return {
            "username": username,
            "platform": "x",
            "created_at": datetime.now().isoformat(),
            "content_metadata": content_metadata
        }
    
    def normalize_facebook_data(self, username: str, raw_data: dict) -> dict:
        # Calculate post frequency
        posts = raw_data.get("posts", [])
        post_frequency = f"{len(posts)}/week" if posts else "0/week"
        
        # Determine last active date
        last_active = ""
        if posts:
            last_post_date = posts[0].get("time", "")
            if last_post_date:
                try:
                    last_active = datetime.strptime(last_post_date, "%Y-%m-%d %H:%M:%S").strftime("%Y-%m-%d")
                except:
                    last_active = last_post_date.split()[0] if " " in last_post_date else last_post_date
        
        metrics = {
            "post_frequency": post_frequency,
            "last_active": last_active
        }
        
        content_metadata = {
            "name": raw_data.get("name", ""),
            "category": raw_data.get("category", ""),
            "posts": [
                {
                    "text": post.get("text", ""),
                    "time": post.get("time", "")
                }
                for post in posts
            ],
            "metrics": metrics
        }
        
        return {
            "username": username,
            "platform": "facebook",
            "created_at": datetime.now().isoformat(),
            "content_metadata": content_metadata
        }

# Base Scraper
class BaseScraper:
    def __init__(self, platform: str):
        self.platform = platform
        self.session = HTMLSession()
        self.normalizer = DataNormalizer()
    
    def check_cache(self, username: str):
        try:
            conn = sqlite3.connect(Config.DATABASE_URL.replace("sqlite:///", ""))
            cursor = conn.cursor()
            
            cursor.execute(
                "SELECT raw_data, metrics, scraped_at FROM social_profiles WHERE platform = ? AND username = ?",
                (self.platform, username)
            )
            result = cursor.fetchone()
            conn.close()
            
            if result:
                raw_data, metrics, scraped_at = result
                scraped_dt = datetime.strptime(scraped_at, "%Y-%m-%d %H:%M:%S.%f")
                
                # Check if cache is still valid
                if datetime.now() - scraped_dt < timedelta(hours=Config.CACHE_EXPIRE_HOURS):
                    return {"raw_data": json.loads(raw_data), "metrics": json.loads(metrics)}
            
            return None
        except Exception as e:
            logger.error(f"Error checking cache for {username}: {str(e)}")
            return None
    
    def save_to_cache(self, username: str, raw_data: dict, metrics: dict):
        try:
            conn = sqlite3.connect(Config.DATABASE_URL.replace("sqlite:///", ""))
            cursor = conn.cursor()
            
            # Clean up old data
            cutoff_date = datetime.now() - timedelta(days=Config.DATA_RETENTION_DAYS)
            cursor.execute(
                "DELETE FROM social_profiles WHERE scraped_at < ?",
                (cutoff_date.strftime("%Y-%m-%d %H:%M:%S.%f"),)
            )
            
            # Insert or replace current data
            cursor.execute(
                """
                INSERT OR REPLACE INTO social_profiles 
                (platform, username, scraped_at, raw_data, metrics)
                VALUES (?, ?, ?, ?, ?)
                """,
                (
                    self.platform,
                    username,
                    datetime.now().strftime("%Y-%m-%d %H:%M:%S.%f"),
                    json.dumps(raw_data),
                    json.dumps(metrics)
                )
            )
            
            conn.commit()
            conn.close()
            logger.info(f"Saved {username} to cache")
        except Exception as e:
            logger.error(f"Error saving {username} to cache: {str(e)}")

# Twitter Scraper
class TwitterScraper(BaseScraper):
    def __init__(self):
        super().__init__("x")
        # Initialize Twitter API client
        try:
            self.client = tweepy.Client(
                bearer_token=TWITTER_BEARER_TOKEN,
                consumer_key=TWITTER_API_KEY,
                consumer_secret=TWITTER_API_SECRET,
                access_token=TWITTER_ACCESS_TOKEN,
                access_token_secret=TWITTER_ACCESS_TOKEN_SECRET,
                wait_on_rate_limit=True
            )
            self.api_available = True
            logger.info("Twitter API client initialized successfully")
        except Exception as e:
            logger.error(f"Error initializing Twitter API client: {str(e)}")
            self.api_available = False
    
    def scrape_profile(self, username: str) -> dict:
        # Check cache first
        cached_data = self.check_cache(username)
        if cached_data:
            logger.info(f"Using cached data for {username}")
            return cached_data["raw_data"]
        
        try:
            logger.info(f"Scraping Twitter profile for {username}")
            
            # Try using Twitter API first
            if self.api_available:
                try:
                    # Get user by username
                    user = self.client.get_user(username=username, 
                                              user_fields=["public_metrics", "created_at", "description", "location", "profile_image_url"])
                    
                    if user.data is None:
                        raise ValueError(f"User {username} not found")
                    
                    user_data = user.data
                    
                    # Get user's tweets
                    tweets_response = self.client.get_users_tweets(
                        id=user_data.id,
                        max_results=5,
                        tweet_fields=["public_metrics", "created_at"]
                    )
                    
                    tweets = []
                    if tweets_response.data:
                        for tweet in tweets_response.data:
                            tweets.append({
                                "tweet": tweet.text,
                                "datetime": tweet.created_at.isoformat() if tweet.created_at else "",
                                "likes_count": tweet.public_metrics.get("like_count", 0),
                                "retweets_count": tweet.public_metrics.get("retweet_count", 0),
                                "replies_count": tweet.public_metrics.get("reply_count", 0)
                            })
                    
                    # Prepare raw data
                    raw_data = {
                        "bio": user_data.description or "",
                        "location": user_data.location or "",
                        "join_date": user_data.created_at.strftime("%Y-%m-%d") if user_data.created_at else "",
                        "tweets": tweets,
                        "followers_count": user_data.public_metrics.get("followers_count", 0),
                        "following_count": user_data.public_metrics.get("following_count", 0)
                    }
                    
                    # Normalize data to get metrics
                    normalized = self.normalizer.normalize_twitter_data(username, raw_data)
                    metrics = normalized.get("content_metadata", {}).get("metrics", {})
                    
                    # Save to cache
                    self.save_to_cache(username, raw_data, metrics)
                    
                    logger.info(f"Successfully scraped Twitter profile for {username} using API")
                    return raw_data
                
                except Exception as api_error:
                    logger.warning(f"Twitter API failed for {username}: {str(api_error)}")
                    # Fall back to scraping method
                    return self.scrape_profile_fallback(username)
            else:
                # API not available, use scraping
                return self.scrape_profile_fallback(username)
        
        except Exception as e:
            logger.error(f"Error scraping Twitter profile for {username}: {str(e)}")
            
            # Handle specific error cases
            if "not found" in str(e).lower() or "suspended" in str(e).lower():
                raise ValueError(f"User {username} not found or account is suspended")
            raise Exception(f"Error scraping Twitter profile: {str(e)}")
    
    def scrape_profile_fallback(self, username: str) -> dict:
        """Fallback scraping method when API is not available"""
        try:
            logger.info(f"Using fallback scraping method for {username}")
            
            # Use requests-html to scrape Twitter profile
            url = f"https://twitter.com/{username}"
            headers = {"User-Agent": Config.USER_AGENTS[0]}
            response = self.session.get(url, headers=headers)
            response.html.render(timeout=10, sleep=2)
            
            # Check if account exists
            if "page doesn't exist" in response.text.lower() or "account suspended" in response.text.lower():
                logger.warning(f"User {username} not found or account is suspended")
                raise ValueError(f"User {username} not found or account is suspended")
            
            # Parse profile data
            soup = BeautifulSoup(response.html.html, "html.parser")
            
            # Extract bio
            bio_element = soup.find("div", {"data-testid": "UserDescription"})
            bio = bio_element.text.strip() if bio_element else ""
            
            # Extract location
            location_element = soup.find("span", {"data-testid": "UserLocation"})
            location = location_element.text.strip() if location_element else ""
            
            # Extract join date
            join_date_element = soup.find("span", {"data-testid": "UserJoinDate"})
            join_date = join_date_element.text.strip() if join_date_element else ""
            
            # Extract followers and following counts
            followers_count = 0
            following_count = 0
            
            # Try to find stats using multiple possible selectors
            stats_selectors = [
                {"data-testid": "UserProfileHeader_Items"},
                {"class": "css-1dbjc4n r-13awgt0 r-18u37iz r-1w6e6rj"},
                {"class": "css-1dbjc4n r-1iusvr4 r-16y2uox r-l5o3dw"}
            ]
            
            for selector in stats_selectors:
                stats_container = soup.find("div", selector)
                if stats_container:
                    stats_links = stats_container.find_all("a")
                    for link in stats_links:
                        text = link.text.strip().lower()
                        if "followers" in text:
                            followers_count = self._extract_number(text)
                        elif "following" in text:
                            following_count = self._extract_number(text)
                    if followers_count > 0 and following_count > 0:
                        break
            
            # Extract tweets
            tweets = []
            tweet_selectors = [
                {"data-testid": "tweet"},
                {"class": "css-1dbjc4n r-1iusvr4 r-16y2uox r-1777fci"},
                {"class": "css-1dbjc4n r-1loqt21 r-18u37iz r-1wtj0ep"}
            ]
            
            tweet_count = 0
            for selector in tweet_selectors:
                if tweet_count >= 5:
                    break
                    
                tweet_elements = soup.find_all("div", selector)
                for tweet_element in tweet_elements:
                    if tweet_count >= 5:
                        break
                        
                    try:
                        # Extract tweet text
                        tweet_text_element = tweet_element.find("div", {"lang": True})
                        if not tweet_text_element:
                            tweet_text_element = tweet_element.find("div", {"data-testid": "tweetText"})
                        tweet_text = tweet_text_element.text.strip() if tweet_text_element else ""
                        
                        # Extract tweet time
                        time_element = tweet_element.find("time")
                        tweet_time = time_element.get("datetime", "") if time_element else ""
                        
                        # Extract engagement metrics
                        likes_count = 0
                        retweets_count = 0
                        replies_count = 0
                        
                        # Try different selectors for engagement metrics
                        engagement_selectors = [
                            {"data-testid": "like"},
                            {"data-testid": "retweet"},
                            {"data-testid": "reply"}
                        ]
                        
                        for eng_selector in engagement_selectors:
                            eng_elements = tweet_element.find_all("div", eng_selector)
                            for eng_element in eng_elements:
                                eng_text = eng_element.text.strip()
                                if "like" in eng_selector.get("data-testid", "").lower():
                                    likes_count = self._extract_number(eng_text)
                                elif "retweet" in eng_selector.get("data-testid", "").lower():
                                    retweets_count = self._extract_number(eng_text)
                                elif "reply" in eng_selector.get("data-testid", "").lower():
                                    replies_count = self._extract_number(eng_text)
                        
                        tweets.append({
                            "tweet": tweet_text,
                            "datetime": tweet_time,
                            "likes_count": likes_count,
                            "retweets_count": retweets_count,
                            "replies_count": replies_count
                        })
                        tweet_count += 1
                    except Exception as e:
                        logger.warning(f"Error parsing tweet: {str(e)}")
                        continue
            
            # Prepare raw data
            raw_data = {
                "bio": bio,
                "location": location,
                "join_date": join_date,
                "tweets": tweets,
                "followers_count": followers_count,
                "following_count": following_count
            }
            
            # Normalize data to get metrics
            normalized = self.normalizer.normalize_twitter_data(username, raw_data)
            metrics = normalized.get("content_metadata", {}).get("metrics", {})
            
            # Save to cache
            self.save_to_cache(username, raw_data, metrics)
            
            logger.info(f"Successfully scraped Twitter profile for {username} using fallback method")
            return raw_data
        
        except Exception as e:
            logger.error(f"Error in fallback scraping for {username}: {str(e)}")
            raise Exception(f"Error scraping Twitter profile: {str(e)}")
    
    def _extract_number(self, text: str) -> int:
        """Extract numeric value from text like '1.2K followers'"""
        numbers = re.findall(r'[\d\.]+', text)
        if not numbers:
            return 0
        
        number = float(numbers[0])
        
        # Handle K, M, etc.
        if 'k' in text.lower():
            number *= 1000
        elif 'm' in text.lower():
            number *= 1000000
        elif 'b' in text.lower():
            number *= 1000000000
        
        return int(number)

# Facebook Scraper
class FacebookScraper(BaseScraper):
    def __init__(self):
        super().__init__("facebook")
    
    def scrape_profile(self, username: str) -> dict:
        # Check cache first
        cached_data = self.check_cache(username)
        if cached_data:
            logger.info(f"Using cached data for {username}")
            return cached_data["raw_data"]
        
        try:
            logger.info(f"Scraping Facebook profile for {username}")
            
            # Use facebook-scraper to get profile data
            profile = get_profile(
                username,
                cookies=None,  # No authentication
                user_agent=Config.USER_AGENTS[0]
            )
            
            if not profile:
                logger.warning(f"Facebook profile {username} not found or is not public")
                raise ValueError(f"Facebook profile {username} not found or is not public")
            
            # Extract posts
            posts = []
            for post in profile.get("posts", [])[:5]:  # Only take latest 5
                posts.append({
                    "text": post.get("text", ""),
                    "time": post.get("time", "")
                })
            
            # Prepare raw data
            raw_data = {
                "name": profile.get("name", ""),
                "about": profile.get("about", ""),
                "category": profile.get("category", ""),
                "profile_picture": profile.get("profile_picture", ""),
                "posts": posts
            }
            
            # Normalize data to get metrics
            normalized = self.normalizer.normalize_facebook_data(username, raw_data)
            metrics = normalized.get("content_metadata", {}).get("metrics", {})
            
            # Save to cache
            self.save_to_cache(username, raw_data, metrics)
            
            logger.info(f"Successfully scraped Facebook profile for {username}")
            return raw_data
        
        except Exception as e:
            logger.error(f"Error scraping Facebook profile for {username}: {str(e)}")
            
            # Handle specific error cases
            if "not found" in str(e).lower() or "not public" in str(e).lower():
                raise ValueError(f"Facebook profile {username} not found or is not public")
            if "age restricted" in str(e).lower():
                raise ValueError(f"Facebook profile {username} is age-gated and cannot be accessed")
            raise Exception(f"Error scraping Facebook profile: {str(e)}")

def validate_x_profile_http(username: str, base_url: str = "http://localhost:8000") -> dict:
    """Validate X profile using HTTP requests to server"""
    url = f"{base_url}/api/x-profile"
    payload = {"username": username}
    
    try:
        response = requests.post(url, json=payload)
        response.raise_for_status()
        return response.json()
    except requests.exceptions.HTTPError as e:
        return {"error": f"HTTP Error: {str(e)}", "status_code": response.status_code}
    except Exception as e:
        return {"error": f"Error: {str(e)}"}

def validate_x_profile_direct(username: str) -> dict:
    """Validate X profile by directly calling scraper functions"""
    try:
        logger.info(f"Directly validating X profile: {username}")
        
        scraper = TwitterScraper()
        raw_data = scraper.scrape_profile(username)
        normalized = scraper.normalizer.normalize_twitter_data(username, raw_data)
        
        result = {
            "platform": "x",
            "username": username,
            "data": normalized.get("content_metadata", {})
        }
        
        logger.info(f"Successfully validated X profile: {username}")
        return result
    except Exception as e:
        logger.error(f"Error validating X profile {username}: {str(e)}")
        return {"error": f"Error: {str(e)}"}

def validate_fb_profile_http(username: str, base_url: str = "http://localhost:8000") -> dict:
    """Validate Facebook profile using HTTP requests to server"""
    url = f"{base_url}/api/fb-profile"
    payload = {"username": username}
    
    try:
        response = requests.post(url, json=payload)
        response.raise_for_status()
        return response.json()
    except requests.exceptions.HTTPError as e:
        return {"error": f"HTTP Error: {str(e)}", "status_code": response.status_code}
    except Exception as e:
        return {"error": f"Error: {str(e)}"}

def validate_fb_profile_direct(username: str) -> dict:
    """Validate Facebook profile by directly calling scraper functions"""
    try:
        logger.info(f"Directly validating Facebook profile: {username}")
        
        scraper = FacebookScraper()
        raw_data = scraper.scrape_profile(username)
        normalized = scraper.normalizer.normalize_facebook_data(username, raw_data)
        
        result = {
            "platform": "facebook",
            "username": username,
            "data": normalized.get("content_metadata", {})
        }
        
        logger.info(f"Successfully validated Facebook profile: {username}")
        return result
    except Exception as e:
        logger.error(f"Error validating Facebook profile {username}: {str(e)}")
        return {"error": f"Error: {str(e)}"}

def main():
    parser = argparse.ArgumentParser(description="Validate OSINT Social Media Scraper API")
    parser.add_argument("--platform", choices=["x", "facebook"], required=True, help="Platform to test")
    parser.add_argument("--username", required=True, help="Username to test")
    parser.add_argument("--base-url", default="http://localhost:8000", help="Base URL of the API")
    parser.add_argument("--direct", action="store_true", help="Use direct validation instead of HTTP")
    
    args = parser.parse_args()
    
    # Initialize database
    init_db()
    
    if args.platform == "x":
        if args.direct:
            result = validate_x_profile_direct(args.username)
        else:
            result = validate_x_profile_http(args.username, args.base_url)
    else:
        if args.direct:
            result = validate_fb_profile_direct(args.username)
        else:
            result = validate_fb_profile_http(args.username, args.base_url)
    
    print(json.dumps(result, indent=2))

if __name__ == "__main__":
    main()