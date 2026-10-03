"""Application configuration using Pydantic BaseSettings."""

from pydantic_settings import BaseSettings
from typing import Optional

class AppConfig(BaseSettings):
    """Application configuration from environment variables and wrangler.jsonc vars."""
    
    # Authentication
    session_secret: str = "keepfor-me-secret-change-in-production-dev-key"
    allow_public_signups: bool = False
    
    # Deployment
    environment: str = "production"
    debug: bool = False
    
    # Rate limiting (for soft limits in app)
    rate_limit_save_per_min: int = 60  # Save 60 URLs per minute
    rate_limit_search_per_min: int = 300  # Search 300 times per minute
    rate_limit_api_per_min: int = 1000  # General API rate limit
    
    # Import limits
    max_import_size_mb: int = 10
    max_import_items: int = 10000
    
    class Config:
        env_file = ".env"
        env_file_encoding = "utf-8"
        case_sensitive = False
        # Allow reading from environment vars like ALLOW_PUBLIC_SIGNUPS
        extra = "allow"

# Create global config instance
config = AppConfig()
