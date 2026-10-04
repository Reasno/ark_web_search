"""Constants for the Ark Web Search integration."""
from __future__ import annotations

DOMAIN = "ark_web_search"

CONF_API_KEY = "api_key"
CONF_DEFAULT_COUNT = "default_count"
CONF_TIMEOUT = "timeout"

DEFAULT_COUNT = 5
DEFAULT_TIMEOUT = 20

API_URL = "https://open.feedcoopapi.com/search_api/web_search"

# Hard limits to keep the tool result from blowing up the model context.
MAX_COUNT = 10
MAX_SUMMARY_CHARS = 800
MAX_TOTAL_CHARS = 6000

TIME_RANGES = ["OneDay", "OneWeek", "OneMonth", "OneYear"]

# Local content libraries (stories / English reading).
CONF_STORIES_DIRECTORY = "stories_directory"
CONF_ENGLISH_DIRECTORY = "english_directory"
CONF_CONTENT_DEFAULT_LIMIT = "content_default_limit"
CONF_CONTENT_MAX_FETCH_CHARS = "content_max_fetch_chars"

DEFAULT_CONTENT_ROOT = "/config/media/reachy_content"
DEFAULT_STORIES_DIRECTORY = f"{DEFAULT_CONTENT_ROOT}/stories"
DEFAULT_ENGLISH_DIRECTORY = f"{DEFAULT_CONTENT_ROOT}/english"
DEFAULT_CONTENT_LIMIT = 8
DEFAULT_CONTENT_MAX_FETCH_CHARS = 12000

CONTENT_LIMIT_MIN = 1
CONTENT_LIMIT_MAX = 20
CONTENT_FETCH_CHARS_MIN = 2000
CONTENT_FETCH_CHARS_MAX = 20000
