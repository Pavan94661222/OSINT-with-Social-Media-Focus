
        # Author: Pavan Kumar R, 7th sem, AIML dept, Global Academy of Technology

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
import random
import chardet
import uvicorn
import sys
import re
from bs4 import BeautifulSoup
from concurrent.futures import ThreadPoolExecutor
from playwright.sync_api import sync_playwright

# Set up logging first
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    handlers=[
        logging.FileHandler("osint.log"),
        logging.StreamHandler()
    ]
)
logger = logging.getLogger(__name__)

# Configuration
class Config:
    DATABASE_URL = "sqlite:///./osint.db"
    CACHE_EXPIRE_HOURS = 6
    DATA_RETENTION_DAYS = 30
    FB_RATE_LIMIT = 2  # requests per minute
    USER_AGENTS = [
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.1 Safari/605.1.15",
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:121.0) Gecko/20100101 Firefox/121.0"
    ]
    PROXY_LIST = []  # Add proxies if needed
    # Facebook cookies - updated with provided values
    FB_COOKIES = {
        "c_user": "61559332525913",
        "xs": "26%3ALETKE4H9J0eVhQ%3A2%3A1754545142%3A-1%3A-1",
        "fr": "02OvFJr80AVXUtWrD.AWex2zhAPupevQATTzyMMZQNruw9TRsj0P5b15oDlh64UuyVRg0.Bl_w-A..AAA.0.0.BolD0e.AWcedgidfxIBtM57ykAsXElJmkY"
    }
    # Server configuration - changed to port 8003
    SERVER_PORT = 8003
    # Enable caching for production use
    DISABLE_CACHE = False

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
class FacebookPost(BaseModel):
    text: str
    time: str

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
        self.fb_buckets = {}

    def check_fb_rate_limit(self, ip: str) -> bool:
        if ip not in self.fb_buckets:
            self.fb_buckets[ip] = LeakyBucket(Config.FB_RATE_LIMIT, Config.FB_RATE_LIMIT / 60)
        return self.fb_buckets[ip].add()

    def get_fb_wait_time(self, ip: str) -> float:
        if ip not in self.fb_buckets:
            return 0
        return self.fb_buckets[ip].estimate_wait_time()

# Data Normalizer
class DataNormalizer:
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
            "profile_picture": raw_data.get("profile_picture", ""),
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

# Facebook Scraper - Now using Playwright
class FacebookScraper:
    def __init__(self):
        self.platform = "facebook"
        self.cookies = Config.FB_COOKIES
        self.normalizer = DataNormalizer()
    
    def check_cache(self, username: str) -> Optional[Dict]:
        # Check cache if enabled
        if Config.DISABLE_CACHE:
            return None
            
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
                    # Only use cache if it has meaningful data
                    parsed_data = json.loads(raw_data)
                    name = parsed_data.get("name", "")
                    posts = parsed_data.get("posts", [])
                    # Only use cache if name is not empty and not a generic term like "Notifications"
                    if name and name != "Notifications" and len(posts) > 0:
                        return {"raw_data": parsed_data, "metrics": json.loads(metrics)}
            
            return None
        except Exception as e:
            logger.error(f"Error checking cache for {username}: {str(e)}")
            return None
    
    def save_to_cache(self, username: str, raw_data: Dict, metrics: Dict):
        # Save to cache if enabled
        if Config.DISABLE_CACHE:
            return
            
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
    
    async def scrape_profile(self, username: str) -> Dict[str, Any]:
        # Check cache first, but only if it has meaningful data
        cached_data = self.check_cache(username)
        if cached_data:
            logger.info(f"Using cached data for {username}")
            return cached_data["raw_data"]
        
        try:
            logger.info(f"Scraping Facebook profile for {username}")
            
            # Use Playwright to scrape the profile
            raw_data = await self.scrape_with_playwright(username)
            
            # Normalize data to get metrics
            normalized = self.normalizer.normalize_facebook_data(username, raw_data)
            metrics = normalized.content_metadata.get("metrics", {})
            
            # Save to cache
            self.save_to_cache(username, raw_data, metrics)
            
            logger.info(f"Successfully scraped Facebook profile for {username} using Playwright")
            return raw_data
        
        except Exception as e:
            logger.error(f"Error scraping Facebook profile for {username}: {str(e)}")
            logger.error(traceback.format_exc())
            
            # Handle specific error cases
            if "not found" in str(e).lower() or "not public" in str(e).lower():
                raise ValueError(f"Facebook profile {username} not found or is not public")
            if "age restricted" in str(e).lower():
                raise ValueError(f"Facebook profile {username} is age-gated and cannot be accessed")
            raise Exception(f"Error scraping Facebook profile: {str(e)}")
    
    async def scrape_with_playwright(self, username: str) -> Dict[str, Any]:
        """Scrape Facebook profile using Playwright"""
        loop = asyncio.get_event_loop()
        with ThreadPoolExecutor() as pool:
            result = await loop.run_in_executor(pool, self._sync_scrape_with_playwright, username)
        return result
    
    def _sync_scrape_with_playwright(self, username: str) -> Dict[str, Any]:
        """Synchronous Playwright scraping function"""
        url = f"https://www.facebook.com/{username}"
        try:
            with sync_playwright() as p:
                browser = p.chromium.launch(headless=True)
                context = browser.new_context(user_agent=(
                    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                    "(KHTML, like Gecko) Chrome/102.0.0.0 Safari/537.36"
                ))
                page = context.new_page()
                
                # Navigate to profile without using cookies to ensure only public data is accessed
                page.goto(url, timeout=60000)
                page.wait_for_timeout(5000)  # Wait for JS-rendered content
                
                html = page.content()
                soup = BeautifulSoup(html, "html.parser")
                
                # Extract name
                name = ""
                title = soup.find("title")
                if title:
                    title_text = title.text.strip()
                    # Extract name from title (format: "Name | Facebook")
                    if "|" in title_text:
                        name = title_text.split("|")[0].strip()
                        # Remove "Verified account" if present
                        name = name.replace("Verified account", "").strip()
                
                # If not found in title, try meta tags
                if not name:
                    meta_element = soup.find("meta", {"property": "og:title"})
                    if meta_element:
                        name = meta_element.get("content", "").strip()
                        # Remove "Verified account" if present
                        name = name.replace("Verified account", "").strip()
                
                # If still not found, try h1 tags
                if not name:
                    h1_elements = soup.find_all("h1")
                    for h1_element in h1_elements:
                        h1_text = h1_element.text.strip()
                        if h1_text and h1_text not in ["Notifications", "Home", "Menu"]:
                            name = h1_text
                            # Remove "Verified account" if present
                            name = name.replace("Verified account", "").strip()
                            break
                
                # Extract category
                category = ""
                meta_element = soup.find("meta", {"property": "og:description"})
                if meta_element:
                    description = meta_element.get("content", "").strip()
                    # Extract category from description
                    if description:
                        # Look for category-like text in description
                        category_match = re.search(r'([A-Z][a-z]+(?:\s+[A-Z][a-z]+)*)', description)
                        if category_match:
                            category = category_match.group(1)
                
                # Extract profile picture
                profile_picture = ""
                # Method 1: Look for profile image in meta tags
                meta_img = soup.find("meta", {"property": "og:image"})
                if meta_img and meta_img.get("content"):
                    profile_picture = meta_img.get("content")
                
                # Method 2: Look for profile image by class
                if not profile_picture:
                    profile_img_element = soup.find("img", class_=re.compile(r"profilePic|profile_photo", re.I))
                    if profile_img_element and profile_img_element.get("src"):
                        profile_picture = profile_img_element.get("src")
                
                # Method 3: Look for any image in the profile header
                if not profile_picture:
                    header = soup.find("div", {"data-pagelet": "ProfileTilesFeed"})
                    if header:
                        img_element = header.find("img")
                        if img_element and img_element.get("src"):
                            profile_picture = img_element.get("src")
                
                # Attempt to extract posts
                posts = []
                post_elements = soup.find_all("div", {"data-testid": "fbfeed_story"})
                
                if not post_elements:
                    post_elements = soup.find_all("div", {"role": "article"})
                
                if not post_elements:
                    post_elements = soup.find_all("div", {"data-ft": True})
                
                # Process the found posts
                for i, post_element in enumerate(post_elements[:5]):  # Limit to 5 posts
                    try:
                        # Extract post text
                        text_element = post_element.find("div", {"data-testid": "post_message"})
                        text = ""
                        if text_element:
                            text = text_element.get_text(strip=True)
                        
                        # If not found, try alternative selectors
                        if not text:
                            text_element = post_element.find("div", class_=re.compile(r"userContent"))
                            if text_element:
                                text = text_element.get_text(strip=True)
                        
                        # Extract post time
                        time_element = post_element.find("abbr", {"data-utime": True})
                        post_time = ""
                        if time_element:
                            post_time = time_element.get("data-utime", "")
                            if post_time:
                                try:
                                    post_time = datetime.fromtimestamp(int(post_time)).strftime("%Y-%m-%d %H:%M:%S")
                                except:
                                    post_time = ""
                        
                        # If we found text, add it to posts
                        if text:
                            posts.append({
                                "text": text,
                                "time": post_time
                            })
                    
                    except Exception as e:
                        logger.warning(f"Error processing post {i+1}: {str(e)}")
                        continue
                
                # If no posts found, try a different approach
                if not posts:
                    for div in soup.find_all("div", string=re.compile(".*")):
                        text = div.get_text(strip=True)
                        if len(text) > 100 and len(posts) < 3:
                            posts.append({
                                "text": text,
                                "time": datetime.utcnow().isoformat()
                            })
                
                # Calculate metrics
                metrics = {
                    "post_frequency": f"{len(posts)}/week",
                    "last_active": datetime.utcnow().date().isoformat()
                }
                
                browser.close()
                
                return {
                    "name": name,
                    "category": category,
                    "profile_picture": profile_picture,
                    "posts": posts,
                    "metrics": metrics
                }
                
        except Exception as e:
            # Handle specific error cases
            if "not found" in str(e).lower() or "not accessible" in str(e).lower():
                raise ValueError(f"Facebook profile {username} not found or is not public")
            if "age restricted" in str(e).lower():
                raise ValueError(f"Facebook profile {username} is age-gated and cannot be accessed")
            raise Exception(f"Scraping blocked or profile not accessible: {str(e)}")

# Rate Limiting Middleware
class RateLimitMiddleware(BaseHTTPMiddleware):
    def __init__(self, app, rate_limiter: RateLimiter):
        super().__init__(app)
        self.rate_limiter = rate_limiter
        
    async def dispatch(self, request: Request, call_next):
        ip = request.client.host
        
        if request.url.path == "/api/fb-profile":
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
app = FastAPI(title="Facebook Profile Scraper API")
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
    parser = argparse.ArgumentParser(description="Validate Facebook Profile Scraper API")
    parser.add_argument("--username", required=True, help="Username to test")
    
    args = parser.parse_args()
    
    result = validate_fb_profile_direct(args.username)
    print(json.dumps(result, indent=2))

def run_server():
    logger.info(f"Starting Facebook Profile Scraper API server on port {Config.SERVER_PORT}")
    uvicorn.run(app, host="0.0.0.0", port=Config.SERVER_PORT)

if __name__ == "__main__":
    # Check if we should run in validation mode
    if len(sys.argv) > 1 and sys.argv[1] == "validate":
        # Remove "validate" from sys.argv to avoid confusing argparse
        sys.argv.pop(1)
        validate_cli()
    else:
        # Run the server
        run_server()