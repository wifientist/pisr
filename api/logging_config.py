"""
Logging configuration for PISR

This module sets up a comprehensive logging configuration for the entire application.
Call setup_logging() at application startup to configure logging.
"""

import logging
import logging.config
import sys
from typing import Dict, Any


def get_logging_config(log_level: str = "INFO") -> Dict[str, Any]:
    """
    Get logging configuration dictionary

    Args:
        log_level: Default log level (DEBUG, INFO, WARNING, ERROR, CRITICAL)

    Returns:
        Logging configuration dict
    """
    return {
        "version": 1,
        "disable_existing_loggers": False,
        "formatters": {
            "default": {
                "format": "%(levelname)s:     %(name)s - %(message)s",
                "datefmt": "%Y-%m-%d %H:%M:%S",
            },
            "detailed": {
                "format": "%(asctime)s - %(name)s - %(levelname)s - %(message)s",
                "datefmt": "%Y-%m-%d %H:%M:%S",
            },
        },
        "handlers": {
            "console": {
                "class": "logging.StreamHandler",
                "level": log_level,
                "formatter": "default",
                "stream": sys.stdout,
            },
        },
        "loggers": {
            # Your application loggers
            "routers": {
                "level": log_level,
                "handlers": ["console"],
                "propagate": False,
            },
            "workflow": {
                "level": log_level,
                "handlers": ["console"],
                "propagate": False,
            },
            "r1api": {
                "level": log_level,
                "handlers": ["console"],
                "propagate": False,
            },
            # Third-party library loggers (set to WARNING to reduce noise)
            "uvicorn": {
                "level": "INFO",
                "handlers": ["console"],
                "propagate": False,
            },
            "uvicorn.access": {
                "level": "WARNING",  # Reduce access log noise
                "handlers": ["console"],
                "propagate": False,
            },
            "sqlalchemy": {
                "level": "WARNING",  # Only show warnings from SQLAlchemy
                "handlers": ["console"],
                "propagate": False,
            },
            "alembic": {
                "level": "INFO",
                "handlers": ["console"],
                "propagate": False,
            },
            # HTTP client libraries - reduce noise
            "httpcore": {
                "level": "WARNING",
                "handlers": ["console"],
                "propagate": False,
            },
            "httpx": {
                "level": "WARNING",
                "handlers": ["console"],
                "propagate": False,
            },
            "httpcore.http11": {
                "level": "WARNING",
                "handlers": ["console"],
                "propagate": False,
            },
            "urllib3": {
                "level": "WARNING",
                "handlers": ["console"],
                "propagate": False,
            },
            "boto3": {
                "level": "WARNING",
                "handlers": ["console"],
                "propagate": False,
            },
            "botocore": {
                "level": "WARNING",
                "handlers": ["console"],
                "propagate": False,
            },
            "fontTools": {
                "level": "WARNING",
                "handlers": ["console"],
                "propagate": False,
            },
        },
        # Root logger - catches everything not caught by specific loggers
        "root": {
            "level": log_level,
            "handlers": ["console"],
        },
    }


def setup_logging(log_level: str = "INFO"):
    """
    Configure logging for the application

    Args:
        log_level: Log level to use (DEBUG, INFO, WARNING, ERROR, CRITICAL)

    Usage:
        # In main.py, before creating the FastAPI app:
        from logging_config import setup_logging
        setup_logging(log_level="INFO")
    """
    config = get_logging_config(log_level)
    logging.config.dictConfig(config)

    # Log that logging has been configured
    logger = logging.getLogger(__name__)
    logger.info(f"Logging configured with level: {log_level}")
    logger.info("=" * 60)
    logger.info("PISR - Logging initialized")
    logger.info("=" * 60)
