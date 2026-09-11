"""Small Redis checks shared by the public readiness probe and operator API."""
from urllib.parse import urlparse

from django.conf import settings
from redis import Redis

QUEUES = ("live", "historical", "search", "control", "celery")


def _redis_client():
    url = settings.CELERY_BROKER_URL
    if urlparse(url).scheme not in {"redis", "rediss"}:
        return None
    return Redis.from_url(url, socket_connect_timeout=2, socket_timeout=2)


def probe_broker() -> bool:
    client = _redis_client()
    if client is None:
        return True
    try:
        return bool(client.ping())
    except Exception:
        return False
    finally:
        client.close()


def queue_health() -> dict:
    client = _redis_client()
    if client is None:
        return {"available": True, "depths": {}, "unexpected_default": 0}
    try:
        pipe = client.pipeline()
        for queue in QUEUES:
            pipe.llen(queue)
        depths = dict(zip(QUEUES, map(int, pipe.execute())))
        return {
            "available": True,
            "depths": depths,
            "unexpected_default": depths["celery"],
        }
    except Exception:
        return {"available": False, "depths": {}, "unexpected_default": 0}
    finally:
        client.close()
