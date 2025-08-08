import asyncio
import json
import time
import argparse
import sqlite3
import logging
import traceback
from datetime import datetime, timedelta
from typing import Dict, List, Optional, Any
from pydantic import BaseModel, Field
from fastapi import FastAPI, HTTPException, Request, status
from starlette.middleware.base import BaseHTTPMiddleware
from fastapi.responses import JSONResponse
import requests
from requests_html import HTMLSession, AsyncHTMLSession
import random
import chardet
import uvicorn
import sys
import re
from bs4 import BeautifulSoup
import threading
import tweepy
from selenium import webdriver
from selenium.webdriver.chrome.options import Options
from selenium.webdriver.common.by import By
from selenium.webdriver.support.ui import WebDriverWait
from selenium.webdriver.support import expected_conditions as EC
from selenium.common.exceptions import TimeoutException, NoSuchElementException

# Set up logging
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    handlers=[
        logging.FileHandler("osint.log"),
        logging.StreamHandler()
    ]
)
logger = logging.getLogger(__name__)

# Twitter API Credentials
TWITTER_BEARER_TOKEN = "AAAAAAAAAAAAAAAAAAAAAGHR3QEAAAAA%2BLrGJpHxkiJRo%2FK6XEhN2XLZ638%3D91rzjAxBwjBo3cezSrD7xGh136Xjk5WcrGSUaMHDMgqMThTdRy"
TWITTER_API_KEY = "PhoTxXyC5MSWKkxexBycP0shn"
TWITTER_API_SECRET = "Pi1LKHmUBX9HSQQjZA0UiBCillstwraoJqHj14tAhVVX6gEv43"
TWITTER_ACCESS_TOKEN = "1950983510103384064-lfmrQdsd1jnF085QuSezkIWtYBAj3G"
TWITTER_ACCESS_TOKEN_SECRET = "4iuHUGJfjFXdMVrJoEw3zAtQQxtuDgGCXU0sKqUj0cZmu"

# Configuration
class Config:
    DATABASE_URL = "sqlite:///./osint.db"
    CACHE_EXPIRE_HOURS = 6
    DATA_RETENTION_DAYS = 30
    X_RATE_LIMIT = 3  # requests per minute
    FB_RATE_LIMIT = 2  # requests per minute
    USER_AGENTS = [
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/91.0.4472.124 Safari/537.36",
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/14.1.1 Safari/605.1.15",
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:89.0) Gecko/20100101 Firefox/89.0"
    ]
    PROXY_LIST = []  # Add proxies if needed
    CAPTCHA_API_KEY = None  # Add 2Captcha API key if needed
    # Facebook cookies - replace with your actual cookies
    FB_COOKIES = {
        "c_user": "YOUR_FACEBOOK_USER_ID",
        "xs": "YOUR_XS_TOKEN",
        "fr": "YOUR_FR_TOKEN"
    }
    # Selenium configuration
    SELENIUM_HEADLESS = True
    CHROME_DRIVER_PATH = "/path/to/chromedriver"  # Update with your path

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

# Models
class Tweet(BaseModel):
    text: str
    time: str
    likes: Optional[int] = None
    retweets: Optional[int] = None
    replies: Optional[int] = None

class TwitterData(BaseModel):
    bio: Optional[str] = None
    location: Optional[str] = None
    join_date: Optional[str] = None
    tweets: List[Tweet] = []
    metrics: Dict[str, Any] = {}

class FacebookPost(BaseModel):
    text: str
    time: str

class FacebookData(BaseModel):
    name: Optional[str] = None
    bio: Optional[str] = None
    category: Optional[str] = None
    profile_photo_url: Optional[str] = None
    posts: List[FacebookPost] = []
    metrics: Dict[str, Any] = {}

class SocialProfileRequest(BaseModel):
    username: str = Field(..., min_length=1, max_length=50)

class SocialProfileResponse(BaseModel):
    platform: str
    username: str
    data: Dict[str, Any]

class UnifiedProfile(BaseModel):
    username: str
    platform: str
    created_at: datetime
    content_metadata: Dict[str, Any]

# Rate Limiter
class LeakyBucket:
    def __init__(self, capacity: int, leak_rate: float):
        self.capacity = capacity
        self.leak_rate = leak_rate  # leaks per second
        self.last_leak = time.time()
        self.water = 0.0

    def add(self, amount: int = 1) -> bool:
        now = time.time()
        elapsed = now - self.last_leak
        self.water = max(0, self.water - elapsed * self.leak_rate)
        self.last_leak = now
        
        if self.water + amount <= self.capacity:
            self.water += amount
            return True
        return False

    def estimate_wait_time(self) -> float:
        if self.water <= 0:
            return 0
        return self.water / self.leak_rate

class RateLimiter:
    def __init__(self):
        self.x_buckets = {}
        self.fb_buckets = {}

    def check_x_rate_limit(self, ip: str) -> bool:
        if ip not in self.x_buckets:
            self.x_buckets[ip] = LeakyBucket(Config.X_RATE_LIMIT, Config.X_RATE_LIMIT / 60)
        return self.x_buckets[ip].add()

    def check_fb_rate_limit(self, ip: str) -> bool:
        if ip not in self.fb_buckets:
            self.fb_buckets[ip] = LeakyBucket(Config.FB_RATE_LIMIT, Config.FB_RATE_LIMIT / 60)
        return self.fb_buckets[ip].add()

    def get_x_wait_time(self, ip: str) -> float:
        if ip not in self.x_buckets:
            return 0
        return self.x_buckets[ip].estimate_wait_time()

    def get_fb_wait_time(self, ip: str) -> float:
        if ip not in self.fb_buckets:
            return 0
        return self.fb_buckets[ip].estimate_wait_time()

# Anti-Scraping Measures
class AntiScraping:
    def __init__(self):
        self.session = HTMLSession()
        self.async_session = None
        self.driver = None
    
    async def get_async_session(self):
        if self.async_session is None:
            self.async_session = AsyncHTMLSession()
        return self.async_session
    
    def get_random_user_agent(self) -> str:
        return random.choice(Config.USER_AGENTS)
    
    def get_random_proxy(self) -> dict:
        if not Config.PROXY_LIST:
            return {}
        proxy = random.choice(Config.PROXY_LIST)
        return {"http": proxy, "https": proxy}
    
    def detect_encoding(self, content: bytes) -> str:
        result = chardet.detect(content)
        return result.get('encoding', 'utf-8')
    
    def render_javascript_sync(self, url: str, wait: float = 2.0) -> HTMLSession:
        headers = {"User-Agent": self.get_random_user_agent()}
        proxies = self.get_random_proxy()
        
        response = self.session.get(url, headers=headers, proxies=proxies)
        response.html.render(timeout=10, sleep=wait)
        return response
    
    async def render_javascript_async(self, url: str, wait: float = 2.0):
        headers = {"User-Agent": self.get_random_user_agent()}
        proxies = self.get_random_proxy()
        
        session = await self.get_async_session()
        response = await session.get(url, headers=headers, proxies=proxies)
        await response.html.arender(timeout=10, sleep=wait)
        return response
    
    def get_selenium_driver(self):
        if self.driver is None:
            options = Options()
            if Config.SELENIUM_HEADLESS:
                options.add_argument("--headless")
            options.add_argument("--no-sandbox")
            options.add_argument("--disable-dev-shm-usage")
            options.add_argument("--disable-gpu")
            options.add_argument(f"--user-agent={self.get_random_user_agent()}")
            
            if Config.PROXY_LIST:
                proxy = random.choice(Config.PROXY_LIST)
                options.add_argument(f"--proxy-server={proxy}")
            
            self.driver = webdriver.Chrome(executable_path=Config.CHROME_DRIVER_PATH, options=options)
        return self.driver
    
    def close_selenium_driver(self):
        if self.driver:
            self.driver.quit()
            self.driver = None

# Data Normalizer
class DataNormalizer:
    def normalize_twitter_data(self, username: str, raw_data: Dict) -> UnifiedProfile:
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
        
        return UnifiedProfile(
            username=username,
            platform="x",
            created_at=datetime.now(),
            content_metadata=content_metadata
        )
    
    def normalize_facebook_data(self, username: str, raw_data: Dict) -> UnifiedProfile:
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
        
        return UnifiedProfile(
            username=username,
            platform="facebook",
            created_at=datetime.now(),
            content_metadata=content_metadata
        )

# Base Scraper
class BaseScraper:
    def __init__(self, platform: str):
        self.platform = platform
        self.anti_scraping = AntiScraping()
        self.normalizer = DataNormalizer()
    
    def check_cache(self, username: str) -> Optional[Dict]:
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
    
    def save_to_cache(self, username: str, raw_data: Dict, metrics: Dict):
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

# Twitter Scraper (Using Twitter API)
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
    
    async def scrape_profile(self, username: str) -> Dict[str, Any]:
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
                    user = await asyncio.to_thread(self.client.get_user, username=username, 
                                                  user_fields=["public_metrics", "created_at", "description", "location", "profile_image_url"])
                    
                    if user.data is None:
                        raise ValueError(f"User {username} not found")
                    
                    user_data = user.data
                    
                    # Get user's tweets
                    tweets_response = await asyncio.to_thread(
                        self.client.get_users_tweets,
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
                    metrics = normalized.content_metadata.get("metrics", {})
                    
                    # Save to cache
                    self.save_to_cache(username, raw_data, metrics)
                    
                    logger.info(f"Successfully scraped Twitter profile for {username} using API")
                    return raw_data
                
                except Exception as api_error:
                    logger.warning(f"Twitter API failed for {username}: {str(api_error)}")
                    # Fall back to scraping method
                    return await self.scrape_profile_fallback(username)
            else:
                # API not available, use scraping
                return await self.scrape_profile_fallback(username)
        
        except Exception as e:
            logger.error(f"Error scraping Twitter profile for {username}: {str(e)}")
            logger.error(traceback.format_exc())
            
            # Handle specific error cases
            if "not found" in str(e).lower() or "suspended" in str(e).lower():
                raise ValueError(f"User {username} not found or account is suspended")
            raise Exception(f"Error scraping Twitter profile: {str(e)}")
    
    async def scrape_profile_fallback(self, username: str) -> Dict[str, Any]:
        """Fallback scraping method when API is not available"""
        try:
            logger.info(f"Using fallback scraping method for {username}")
            
            # Use AsyncHTMLSession to scrape Twitter profile
            url = f"https://twitter.com/{username}"
            response = await self.anti_scraping.render_javascript_async(url)
            
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
            metrics = normalized.content_metadata.get("metrics", {})
            
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
        # Add Facebook cookies here (replace with your actual cookies)
        self.cookies = Config.FB_COOKIES
    
    async def scrape_profile(self, username: str) -> Dict[str, Any]:
        # Check cache first
        cached_data = self.check_cache(username)
        if cached_data:
            logger.info(f"Using cached data for {username}")
            return cached_data["raw_data"]
        
        try:
            logger.info(f"Scraping Facebook profile for {username}")
            
            # Try using Selenium first (most reliable)
            try:
                raw_data = await self.scrape_with_selenium(username)
                if raw_data and (raw_data.get("name") or raw_data.get("posts")):
                    # Normalize data to get metrics
                    normalized = self.normalizer.normalize_facebook_data(username, raw_data)
                    metrics = normalized.content_metadata.get("metrics", {})
                    
                    # Save to cache
                    self.save_to_cache(username, raw_data, metrics)
                    
                    logger.info(f"Successfully scraped Facebook profile for {username} using Selenium")
                    return raw_data
            except Exception as selenium_error:
                logger.warning(f"Selenium scraping failed for {username}: {str(selenium_error)}")
            
            # Fallback to requests-html
            try:
                raw_data = await self.scrape_with_requests_html(username)
                if raw_data and (raw_data.get("name") or raw_data.get("posts")):
                    # Normalize data to get metrics
                    normalized = self.normalizer.normalize_facebook_data(username, raw_data)
                    metrics = normalized.content_metadata.get("metrics", {})
                    
                    # Save to cache
                    self.save_to_cache(username, raw_data, metrics)
                    
                    logger.info(f"Successfully scraped Facebook profile for {username} using requests-html")
                    return raw_data
            except Exception as requests_html_error:
                logger.warning(f"requests-html scraping failed for {username}: {str(requests_html_error)}")
            
            # If all methods fail, return minimal data
            logger.warning(f"All scraping methods failed for {username}")
            return {
                "name": "",
                "about": "",
                "category": "",
                "profile_picture": "",
                "posts": []
            }
        
        except Exception as e:
            logger.error(f"Error scraping Facebook profile for {username}: {str(e)}")
            logger.error(traceback.format_exc())
            
            # Handle specific error cases
            if "not found" in str(e).lower() or "not public" in str(e).lower():
                raise ValueError(f"Facebook profile {username} not found or is not public")
            if "age restricted" in str(e).lower():
                raise ValueError(f"Facebook profile {username} is age-gated and cannot be accessed")
            raise Exception(f"Error scraping Facebook profile: {str(e)}")
    
    async def scrape_with_selenium(self, username: str) -> Dict[str, Any]:
        """Scrape Facebook profile using Selenium"""
        try:
            logger.info(f"Using Selenium to scrape Facebook profile: {username}")
            
            driver = self.anti_scraping.get_selenium_driver()
            
            # Set cookies if available
            if self.cookies:
                driver.get("https://www.facebook.com")
                for name, value in self.cookies.items():
                    driver.add_cookie({
                        'name': name,
                        'value': value,
                        'domain': '.facebook.com'
                    })
            
            # Navigate to profile
            url = f"https://www.facebook.com/{username}"
            driver.get(url)
            
            # Wait for page to load
            WebDriverWait(driver, 10).until(
                EC.presence_of_element_located((By.TAG_NAME, "body"))
            )
            
            # Scroll down to load more content
            for _ in range(3):
                driver.execute_script("window.scrollTo(0, document.body.scrollHeight);")
                time.sleep(2)
            
            # Get page source
            page_source = driver.page_source
            soup = BeautifulSoup(page_source, "html.parser")
            
            # Extract name
            name = ""
            name_selectors = [
                ("h1", {"data-testid": "page_title"}),
                ("h1", {"class": "dati1w0a"}),
                ("h1", {"class": "profileName"}),
                ("span", {"class": "fwb"})
            ]
            
            for tag, attrs in name_selectors:
                name_element = soup.find(tag, attrs)
                if name_element:
                    name = name_element.text.strip()
                    break
            
            # Extract category
            category = ""
            category_selectors = [
                ("div", {"data-testid": "page_category"}),
                ("div", {"class": "_50f4"}),
                ("span", {"class": "_50f5"})
            ]
            
            for tag, attrs in category_selectors:
                category_element = soup.find(tag, attrs)
                if category_element:
                    category = category_element.text.strip()
                    break
            
            # Extract posts
            posts = []
            post_selectors = [
                ("div", {"data-testid": "post_message"}),
                ("div", {"class": "userContent"}),
                ("div", {"class": "_5pbx userContent"}),
                ("div", {"class": "_4-u2 mbm _4mrt"})
            ]
            
            for tag, attrs in post_selectors:
                post_elements = soup.find_all(tag, attrs)
                if post_elements:
                    for post_element in post_elements[:5]:
                        text = post_element.text.strip()
                        
                        # Find timestamp
                        time_element = None
                        parent = post_element.parent
                        while parent and not time_element:
                            time_element = parent.find("time") or parent.find("abbr")
                            parent = parent.parent
                        
                        post_time = ""
                        if time_element:
                            post_time = time_element.get("datetime", "") or time_element.get("data-utime", "")
                            if post_time and post_time.isdigit():
                                try:
                                    post_time = datetime.fromtimestamp(int(post_time)).strftime("%Y-%m-%d %H:%M:%S")
                                except:
                                    post_time = ""
                        
                        posts.append({
                            "text": text,
                            "time": post_time
                        })
                    break
            
            return {
                "name": name,
                "about": "",
                "category": category,
                "profile_picture": "",
                "posts": posts
            }
            
        except Exception as e:
            logger.error(f"Error in Selenium scraping for {username}: {str(e)}")
            raise Exception(f"Error in Selenium scraping: {str(e)}")
    
    async def scrape_with_requests_html(self, username: str) -> Dict[str, Any]:
        """Scrape Facebook profile using requests-html"""
        try:
            logger.info(f"Using requests-html to scrape Facebook profile: {username}")
            
            url = f"https://www.facebook.com/{username}"
            response = await self.anti_scraping.render_javascript_async(url, wait=5.0)
            
            # Check if the page exists
            if "content is not available" in response.text.lower() or "page not found" in response.text.lower():
                logger.warning(f"Facebook profile {username} not found")
                raise ValueError(f"Facebook profile {username} not found")
            
            soup = BeautifulSoup(response.html.html, "html.parser")
            
            # Extract name - try multiple selectors
            name = ""
            name_selectors = [
                ("h1", {"data-testid": "page_title"}),
                ("h1", {"class": "dati1w0a"}),
                ("h1", {"class": "profileName"}),
                ("span", {"class": "fwb"})
            ]
            
            for tag, attrs in name_selectors:
                name_element = soup.find(tag, attrs)
                if name_element:
                    name = name_element.text.strip()
                    break
            
            # Extract category (for pages)
            category = ""
            category_selectors = [
                ("div", {"data-testid": "page_category"}),
                ("div", {"class": "_50f4"}),
                ("span", {"class": "_50f5"})
            ]
            
            for tag, attrs in category_selectors:
                category_element = soup.find(tag, attrs)
                if category_element:
                    category = category_element.text.strip()
                    break
            
            # Extract posts - try multiple selectors
            posts = []
            post_selectors = [
                ("div", {"data-testid": "post_message"}),
                ("div", {"class": "userContent"}),
                ("div", {"class": "_5pbx userContent"}),
                ("div", {"class": "_4-u2 mbm _4mrt"})
            ]
            
            for tag, attrs in post_selectors:
                post_elements = soup.find_all(tag, attrs)
                if post_elements:
                    for post_element in post_elements[:5]:
                        text = post_element.text.strip()
                        
                        # Try to find the time element
                        time_element = None
                        parent = post_element.parent
                        while parent and not time_element:
                            time_element = parent.find("time") or parent.find("abbr")
                            parent = parent.parent
                        
                        post_time = ""
                        if time_element:
                            post_time = time_element.get("datetime", "") or time_element.get("data-utime", "")
                            if post_time and post_time.isdigit():
                                try:
                                    post_time = datetime.fromtimestamp(int(post_time)).strftime("%Y-%m-%d %H:%M:%S")
                                except:
                                    post_time = ""
                        
                        posts.append({
                            "text": text,
                            "time": post_time
                        })
                    break  # Stop if we found posts with this selector
            
            # If no posts found, try to find posts in a different structure
            if not posts:
                # Look for posts in the main content area
                main_content = soup.find("div", {"id": "content"}) or soup.find("div", {"role": "main"})
                if main_content:
                    # Find all divs that might contain posts
                    potential_posts = main_content.find_all("div", recursive=False)
                    for post_div in potential_posts:
                        # Look for text in the div
                        text = post_div.get_text(strip=True)
                        if len(text) > 20:  # Arbitrary length to avoid short texts
                            # Try to find time
                            time_element = post_div.find("time") or post_div.find("abbr")
                            post_time = ""
                            if time_element:
                                post_time = time_element.get("datetime", "") or time_element.get("data-utime", "")
                                if post_time and post_time.isdigit():
                                    try:
                                        post_time = datetime.fromtimestamp(int(post_time)).strftime("%Y-%m-%d %H:%M:%S")
                                    except:
                                        post_time = ""
                            
                            posts.append({
                                "text": text,
                                "time": post_time
                            })
                            if len(posts) >= 5:
                                break
            
            return {
                "name": name,
                "about": "",
                "category": category,
                "profile_picture": "",
                "posts": posts
            }
            
        except Exception as e:
            logger.error(f"Error in requests-html scraping for {username}: {str(e)}")
            raise Exception(f"Error in requests-html scraping: {str(e)}")

# Rate Limiting Middleware
class RateLimitMiddleware(BaseHTTPMiddleware):
    def __init__(self, app, rate_limiter: RateLimiter):
        super().__init__(app)
        self.rate_limiter = rate_limiter
    async def dispatch(self, request: Request, call_next):
        ip = request.client.host
        
        if request.url.path == "/api/x-profile":
            if not self.rate_limiter.check_x_rate_limit(ip):
                wait_time = self.rate_limiter.get_x_wait_time(ip)
                logger.warning(f"Rate limit exceeded for IP {ip} on X endpoint. Wait time: {wait_time} seconds")
                return JSONResponse(
                    status_code=status.HTTP_429_TOO_MANY_REQUESTS,
                    content={"message": "Rate limit exceeded", "retry_after": int(wait_time)},
                    headers={"Retry-After": str(int(wait_time))}
                )
        elif request.url.path == "/api/fb-profile":
            if not self.rate_limiter.check_fb_rate_limit(ip):
                wait_time = self.rate_limiter.get_fb_wait_time(ip)
                logger.warning(f"Rate limit exceeded for IP {ip} on Facebook endpoint. Wait time: {wait_time} seconds")
                return JSONResponse(
                    status_code=status.HTTP_429_TOO_MANY_REQUESTS,
                    content={"message": "Rate limit exceeded", "retry_after": int(wait_time)},
                    headers={"Retry-After": str(int(wait_time))}
                )
        
        response = await call_next(request)
        response.headers["X-OSINT-Warning"] = "public-data-only"
        return response

# Initialize FastAPI app
app = FastAPI()
rate_limiter = RateLimiter()
app.add_middleware(RateLimitMiddleware, rate_limiter=rate_limiter)

# Initialize database
init_db()

# Exception handler for better error logging
@app.exception_handler(Exception)
async def global_exception_handler(request: Request, exc: Exception):
    logger.error(f"Unhandled exception: {str(exc)}")
    logger.error(traceback.format_exc())
    return JSONResponse(
        status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
        content={"detail": "Internal server error"}
    )

# Twitter endpoint
@app.post("/api/x-profile", response_model=SocialProfileResponse)
async def get_x_profile(profile_data: SocialProfileRequest):
    try:
        logger.info(f"Received request for X profile: {profile_data.username}")
        scraper = TwitterScraper()
        raw_data = await scraper.scrape_profile(profile_data.username)
        
        # Normalize data
        normalized = scraper.normalizer.normalize_twitter_data(
            profile_data.username, 
            raw_data
        )
        
        # Format response
        response_data = {
            "platform": "x",
            "username": profile_data.username,
            "data": normalized.content_metadata
        }
        
        logger.info(f"Successfully processed X profile: {profile_data.username}")
        return response_data
    
    except ValueError as e:
        logger.warning(f"Value error for X profile {profile_data.username}: {str(e)}")
        # Handle account not found or suspended
        if "not found" in str(e).lower() or "suspended" in str(e).lower():
            raise HTTPException(
                status_code=status.HTTP_451_UNAVAILABLE_FOR_LEGAL_REASONS,
                detail=str(e)
            )
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=str(e)
        )
    
    except Exception as e:
        logger.error(f"Error processing X profile {profile_data.username}: {str(e)}")
        logger.error(traceback.format_exc())
        # Handle temporary blocks
        if "temporary" in str(e).lower() or "block" in str(e).lower():
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail={"message": str(e), "wait_time": 300}
            )
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"An error occurred: {str(e)}"
        )

# Facebook endpoint
@app.post("/api/fb-profile", response_model=SocialProfileResponse)
async def get_fb_profile(profile_data: SocialProfileRequest):
    try:
        logger.info(f"Received request for Facebook profile: {profile_data.username}")
        scraper = FacebookScraper()
        raw_data = await scraper.scrape_profile(profile_data.username)
        
        # Normalize data
        normalized = scraper.normalizer.normalize_facebook_data(
            profile_data.username, 
            raw_data
        )
        
        # Format response
        response_data = {
            "platform": "facebook",
            "username": profile_data.username,
            "data": normalized.content_metadata
        }
        
        logger.info(f"Successfully processed Facebook profile: {profile_data.username}")
        return response_data
    
    except ValueError as e:
        logger.warning(f"Value error for Facebook profile {profile_data.username}: {str(e)}")
        # Handle account not found or suspended
        if "not found" in str(e).lower() or "not public" in str(e).lower():
            raise HTTPException(
                status_code=status.HTTP_451_UNAVAILABLE_FOR_LEGAL_REASONS,
                detail=str(e)
            )
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=str(e)
        )
    
    except Exception as e:
        logger.error(f"Error processing Facebook profile {profile_data.username}: {str(e)}")
        logger.error(traceback.format_exc())
        # Handle temporary blocks
        if "temporary" in str(e).lower() or "block" in str(e).lower():
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail={"message": str(e), "wait_time": 300}
            )
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"An error occurred: {str(e)}"
        )

# Validation Script - Direct execution without server
def validate_x_profile_direct(username: str) -> Dict[str, Any]:
    """Directly validate X profile without HTTP server"""
    try:
        logger.info(f"Directly validating X profile: {username}")
        
        # Create a new event loop
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        
        try:
            # Create a new scraper instance with a new AntiScraping instance
            scraper = TwitterScraper()
            raw_data = loop.run_until_complete(scraper.scrape_profile(username))
            normalized = scraper.normalizer.normalize_twitter_data(username, raw_data)
            
            result = {
                "platform": "x",
                "username": username,
                "data": normalized.content_metadata
            }
            
            logger.info(f"Successfully validated X profile: {username}")
            return result
        finally:
            loop.close()
    except Exception as e:
        logger.error(f"Error validating X profile {username}: {str(e)}")
        return {"error": f"Error: {str(e)}"}

def validate_fb_profile_direct(username: str) -> Dict[str, Any]:
    """Directly validate Facebook profile without HTTP server"""
    try:
        logger.info(f"Directly validating Facebook profile: {username}")
        
        # Create a new event loop
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        
        try:
            scraper = FacebookScraper()
            raw_data = loop.run_until_complete(scraper.scrape_profile(username))
            normalized = scraper.normalizer.normalize_facebook_data(username, raw_data)
            
            result = {
                "platform": "facebook",
                "username": username,
                "data": normalized.content_metadata
            }
            
            logger.info(f"Successfully validated Facebook profile: {username}")
            return result
        finally:
            loop.close()
    except Exception as e:
        logger.error(f"Error validating Facebook profile {username}: {str(e)}")
        return {"error": f"Error: {str(e)}"}

def validate_cli():
    parser = argparse.ArgumentParser(description="Validate OSINT Social Media Scraper API")
    parser.add_argument("--platform", choices=["x", "facebook"], required=True, help="Platform to test")
    parser.add_argument("--username", required=True, help="Username to test")
    parser.add_argument("--base-url", default="http://localhost:8000", help="Base URL of the API")
    
    args = parser.parse_args()
    
    if args.platform == "x":
        result = validate_x_profile_direct(args.username)
    else:
        result = validate_fb_profile_direct(args.username)
    
    print(json.dumps(result, indent=2))

def run_server():
    logger.info("Starting OSINT Social Media Scraper API server")
    uvicorn.run(app, host="0.0.0.0", port=8000)

if __name__ == "__main__":
    # Check if we should run in validation mode
    if len(sys.argv) > 1 and sys.argv[1] == "validate":
        # Remove "validate" from sys.argv to avoid confusing argparse
        sys.argv.pop(1)
        validate_cli()
    else:
        # Run the server
        run_server()