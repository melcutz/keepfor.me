import json
import logging
import uuid
from contextvars import ContextVar
from datetime import datetime
from typing import Any, Optional

# Context variables for request tracking
_request_context: ContextVar[dict[str, Any]] = ContextVar("request_context", default={})

def get_request_id() -> str:
    """Get or create unique request ID for this context."""
    ctx = _request_context.get().copy()
    if "request_id" not in ctx:
        ctx["request_id"] = str(uuid.uuid4())
        _request_context.set(ctx)
    return ctx["request_id"]

def set_user_context(user_id: str) -> None:
    """Attach user ID to current request context."""
    ctx = _request_context.get().copy()
    ctx["user_id"] = user_id
    _request_context.set(ctx)

def clear_context() -> None:
    """Clear context (for testing or after request)."""
    _request_context.set({})

class JSONFormatter(logging.Formatter):
    """Outputs structured JSON logs for Cloudflare Workers."""
    
    def format(self, record: logging.LogRecord) -> str:
        ctx = _request_context.get()
        log_data = {
            "timestamp": datetime.utcnow().isoformat(),
            "level": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
        }
        
        # Add context if present
        if "request_id" in ctx:
            log_data["request_id"] = ctx["request_id"]
        if "user_id" in ctx:
            log_data["user_id"] = ctx["user_id"]
        
        # Add exception if present
        if record.exc_info:
            log_data["exception"] = self.formatException(record.exc_info)
        
        return json.dumps(log_data)

def setup_logging() -> logging.Logger:
    """Initialize structured logging to stdout."""
    logger = logging.getLogger("keepfor-me")
    logger.setLevel(logging.INFO)
    
    # Remove existing handlers (prevent duplicates)
    logger.handlers = []
    
    # Console handler with JSON formatter
    handler = logging.StreamHandler()
    handler.setFormatter(JSONFormatter())
    logger.addHandler(handler)
    
    return logger

# Global logger instance
logger = setup_logging()

def log_error(msg: str, exc: Optional[Exception] = None, **kwargs) -> None:
    """Log error with optional exception details."""
    if exc:
        logger.error(f"{msg}: {exc}", exc_info=exc)
    else:
        logger.error(msg)

def log_info(msg: str, **kwargs) -> None:
    """Log info message."""
    logger.info(msg)

def log_debug(msg: str, **kwargs) -> None:
    """Log debug message."""
    logger.debug(msg)

def log_warning(msg: str, **kwargs) -> None:
    """Log warning message."""
    logger.warning(msg)
