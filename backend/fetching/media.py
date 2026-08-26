"""Download tweet photos onto the media volume and rewrite feed URLs.

Videos stay on X (inline playback, zero storage). Photos are the durability
bet: a few GB, and the post still has a picture if X deletes the original.
"""
from __future__ import annotations

import hashlib
import logging
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.parse import urlparse
from urllib.request import Request, urlopen

from django.conf import settings
from django.utils import timezone

from tweets.models import MediaAsset, Tweet

logger = logging.getLogger(__name__)

# Only X's image CDN. A stored URL is operator-adjacent data but still an
# outbound fetch, so the host allowlist is the SSRF ceiling.
_ALLOWED_SUFFIX = ".twimg.com"
_MAX_BYTES = 8 * 1024 * 1024
_TIMEOUT = 15
_PHOTO = "photo"
_NESTED = ("quoted_tweet", "retweeted_tweet")
_IMAGE_EXTS = {".jpg", ".jpeg", ".png", ".webp", ".gif"}


def is_allowed_photo_url(url: str) -> bool:
    parsed = urlparse(url)
    if parsed.scheme != "https":
        return False
    host = (parsed.hostname or "").lower()
    return host == "twimg.com" or host.endswith(_ALLOWED_SUFFIX)


def photo_urls_from_extras(extras) -> list[str]:
    urls: list[str] = []

    def walk(blob) -> None:
        if not isinstance(blob, dict):
            return
        media = blob.get("media")
        if isinstance(media, list):
            for item in media:
                if not isinstance(item, dict) or item.get("type") != _PHOTO:
                    continue
                url = item.get("url")
                if isinstance(url, str) and url:
                    urls.append(url)
        for key in _NESTED:
            walk(blob.get(key))

    walk(extras if isinstance(extras, dict) else {})
    return urls


def relative_path_for(url: str) -> str:
    digest = hashlib.sha256(url.encode()).hexdigest()
    ext = Path(urlparse(url).path).suffix.lower()
    if ext not in _IMAGE_EXTS:
        ext = ".jpg"
    return f"{digest[:2]}/{digest}{ext}"


def local_url(relative_path: str) -> str:
    return f"{settings.MEDIA_URL.rstrip('/')}/{relative_path.lstrip('/')}"


def rewrite_media_blob(blob, assets: dict[str, str]):
    """Copy a tweet extras dict, swapping photo URLs we have on disk."""
    if not isinstance(blob, dict) or not assets:
        return blob
    out = dict(blob)
    media = blob.get("media")
    if isinstance(media, list):
        rewritten = []
        for item in media:
            if not isinstance(item, dict):
                rewritten.append(item)
                continue
            copy = dict(item)
            url = copy.get("url")
            if copy.get("type") == _PHOTO and url in assets:
                copy["url"] = assets[url]
            rewritten.append(copy)
        out["media"] = rewritten
    for key in _NESTED:
        nested = blob.get(key)
        if isinstance(nested, dict):
            out[key] = rewrite_media_blob(nested, assets)
    return out


def lookup_local_urls(remote_urls: list[str]) -> dict[str, str]:
    """remote URL → /media/... for assets whose file is actually on disk."""
    wanted = [url for url in remote_urls if url]
    if not wanted:
        return {}
    root = Path(settings.MEDIA_ROOT)
    found = {}
    for asset in MediaAsset.objects.filter(remote_url__in=wanted).only(
        "remote_url", "relative_path"
    ):
        if (root / asset.relative_path).is_file():
            found[asset.remote_url] = local_url(asset.relative_path)
    return found


def archive_batch(limit: int) -> int:
    """Download up to `limit` missing photos. Returns how many were stored."""
    if limit <= 0:
        return 0
    root = Path(settings.MEDIA_ROOT)
    root.mkdir(parents=True, exist_ok=True)
    ready = {
        asset.remote_url
        for asset in MediaAsset.objects.only("remote_url", "relative_path")
        if (root / asset.relative_path).is_file()
    }
    saved = 0
    tried: set[str] = set()
    for tweet in Tweet.objects.exclude(extras={}).only("extras").iterator(chunk_size=200):
        for url in photo_urls_from_extras(tweet.extras):
            if url in ready or url in tried or not is_allowed_photo_url(url):
                continue
            tried.add(url)
            if _store(url, root) is None:
                continue
            ready.add(url)
            saved += 1
            if saved >= limit:
                return saved
    return saved


def _store(url: str, root: Path) -> MediaAsset | None:
    relative = relative_path_for(url)
    dest = root / relative
    dest.parent.mkdir(parents=True, exist_ok=True)
    tmp = dest.with_suffix(dest.suffix + ".part")
    try:
        request = Request(url, headers={"User-Agent": "twitter-saas-media-archive/1"})
        with urlopen(request, timeout=_TIMEOUT) as response:
            length = int(response.headers.get("Content-Length") or 0)
            if length > _MAX_BYTES:
                logger.warning("archive_media: skip oversized %s (%s bytes)", url, length)
                return None
            body = response.read(_MAX_BYTES + 1)
    except (HTTPError, URLError, TimeoutError, OSError, ValueError) as exc:
        logger.info("archive_media: miss %s (%s)", url, exc)
        return None
    if len(body) > _MAX_BYTES:
        logger.warning("archive_media: skip oversized %s", url)
        return None
    tmp.write_bytes(body)
    tmp.replace(dest)
    asset, _created = MediaAsset.objects.update_or_create(
        remote_url=url,
        defaults={"relative_path": relative, "last_ok_at": timezone.now()},
    )
    return asset
