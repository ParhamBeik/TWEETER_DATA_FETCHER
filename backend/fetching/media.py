"""Download tweet photos and videos onto the media volume, and rewrite feed URLs.

Both are the durability bet: the post still has its media if X deletes the
original. Video was previously left on X for "inline playback, zero storage",
but X refuses those requests from a browser that is not x.com -- every
`video.twimg.com` fetch came back 403, so the console showed a play button that
could never play anything and re-requested the file on every render.

Videos are stored at a bounded bitrate rather than at their best available
quality, so one long clip cannot cost tens of megabytes.
"""
from __future__ import annotations

import hashlib
import logging
from datetime import timedelta
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
# Video is an order of magnitude larger than a photo and gets its own ceiling.
_MAX_VIDEO_BYTES = 48 * 1024 * 1024
_TIMEOUT = 15
_VIDEO_TIMEOUT = 60
_PHOTO = "photo"
# X labels looping clips "animated_gif" but still serves them as mp4 variants.
_VIDEO_TYPES = ("video", "animated_gif")
_NESTED = ("quoted_tweet", "retweeted_tweet")
_IMAGE_EXTS = {".jpg", ".jpeg", ".png", ".webp", ".gif"}
# Take the best mp4 at or under this bitrate. Picking the highest available
# instead would archive 1080p masters -- several times the bytes for a clip
# shown in a 590px column.
_MAX_VIDEO_BITRATE = 2_000_000
# How far back video archiving reaches. Wide enough that a clip ingested while
# the archiver was busy or offline still gets picked up on a later pass, narrow
# enough that turning this on does not start downloading the back catalogue.
VIDEO_BACKFILL_DAYS = 3


def best_video_variant(item) -> str | None:
    """The mp4 URL to archive for one media item, or None if there is none.

    Deterministic, and shared by the extractor and the rewriter -- if the two
    disagreed about which variant to use, the file would be downloaded under one
    URL and looked up under another, and nothing would ever appear to be stored.
    """
    if not isinstance(item, dict) or item.get("type") not in _VIDEO_TYPES:
        return None
    variants = item.get("variants")
    if not isinstance(variants, list):
        return None
    mp4s = [
        v for v in variants
        if isinstance(v, dict)
        and str(v.get("content_type") or "").startswith("video/mp4")
        and isinstance(v.get("url"), str)
        and v["url"]
    ]
    if not mp4s:
        return None
    def bitrate(v):
        try:
            return int(v.get("bitrate") or 0)
        except (TypeError, ValueError):
            return 0
    under = [v for v in mp4s if bitrate(v) <= _MAX_VIDEO_BITRATE]
    # Best under the cap; if every variant exceeds it, take the smallest.
    chosen = max(under, key=bitrate) if under else min(mp4s, key=bitrate)
    return chosen["url"]


def is_allowed_photo_url(url: str) -> bool:
    parsed = urlparse(url)
    if parsed.scheme != "https":
        return False
    host = (parsed.hostname or "").lower()
    return host == "twimg.com" or host.endswith(_ALLOWED_SUFFIX)


def photo_urls_from_extras(extras) -> list[str]:
    """Every archivable media URL: photo stills plus the chosen video variant."""
    urls: list[str] = []

    def walk(blob) -> None:
        if not isinstance(blob, dict):
            return
        media = blob.get("media")
        if isinstance(media, list):
            for item in media:
                if not isinstance(item, dict):
                    continue
                if item.get("type") == _PHOTO:
                    url = item.get("url")
                    if isinstance(url, str) and url:
                        urls.append(url)
                    continue
                variant = best_video_variant(item)
                if variant:
                    urls.append(variant)
                # The poster frame is a normal image and is what the card shows
                # before playback, so it is worth keeping too.
                poster = item.get("url")
                if item.get("type") in _VIDEO_TYPES and isinstance(poster, str) and poster:
                    urls.append(poster)
        for key in _NESTED:
            walk(blob.get(key))

    walk(extras if isinstance(extras, dict) else {})
    return urls


def relative_path_for(url: str) -> str:
    digest = hashlib.sha256(url.encode()).hexdigest()
    ext = Path(urlparse(url).path).suffix.lower()
    if ext == ".mp4":
        return f"{digest[:2]}/{digest}.mp4"
    if ext not in _IMAGE_EXTS:
        ext = ".jpg"
    return f"{digest[:2]}/{digest}{ext}"


def _is_video_url(url: str) -> bool:
    return Path(urlparse(url).path).suffix.lower() == ".mp4"


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
            elif copy.get("type") in _VIDEO_TYPES:
                # `url` stays the poster frame (swapped for the local copy when
                # we have it); `src` is the playable file. The console plays
                # `src` and never touches video.twimg.com, which is the whole
                # point -- X answers those requests with 403.
                if url in assets:
                    copy["url"] = assets[url]
                variant = best_video_variant(item)
                if variant and variant in assets:
                    copy["src"] = assets[variant]
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
    # Photos backfill across the whole archive, as they always have. Video is
    # forward-only: the back catalogue would be a large, unpredictable download
    # for clips X has mostly stopped serving anyway.
    video_cutoff = timezone.now() - timedelta(days=VIDEO_BACKFILL_DAYS)
    for tweet in (
        Tweet.objects.exclude(extras={}).only("extras", "ingested_at").iterator(chunk_size=200)
    ):
        recent = tweet.ingested_at is not None and tweet.ingested_at >= video_cutoff
        for url in photo_urls_from_extras(tweet.extras):
            if url in ready or url in tried or not is_allowed_photo_url(url):
                continue
            if _is_video_url(url) and not recent:
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
    is_video = _is_video_url(url)
    max_bytes = _MAX_VIDEO_BYTES if is_video else _MAX_BYTES
    timeout = _VIDEO_TIMEOUT if is_video else _TIMEOUT
    dest = root / relative
    dest.parent.mkdir(parents=True, exist_ok=True)
    tmp = dest.with_suffix(dest.suffix + ".part")
    try:
        request = Request(url, headers={"User-Agent": "twitter-saas-media-archive/1"})
        with urlopen(request, timeout=timeout) as response:
            length = int(response.headers.get("Content-Length") or 0)
            if length > max_bytes:
                logger.warning("archive_media: skip oversized %s (%s bytes)", url, length)
                return None
            body = response.read(max_bytes + 1)
    except (HTTPError, URLError, TimeoutError, OSError, ValueError) as exc:
        logger.info("archive_media: miss %s (%s)", url, exc)
        return None
    if len(body) > max_bytes:
        logger.warning("archive_media: skip oversized %s", url)
        return None
    tmp.write_bytes(body)
    tmp.replace(dest)
    asset, _created = MediaAsset.objects.update_or_create(
        remote_url=url,
        defaults={"relative_path": relative, "last_ok_at": timezone.now()},
    )
    return asset


def _self_check() -> None:
    """Variant choice is what makes stored and looked-up URLs agree."""
    item = {
        "type": "video",
        "url": "https://pbs.twimg.com/poster.jpg",
        "variants": [
            {"url": "https://video.twimg.com/a.m3u8", "content_type": "application/x-mpegURL"},
            {"url": "https://video.twimg.com/low.mp4", "content_type": "video/mp4", "bitrate": 256000},
            {"url": "https://video.twimg.com/mid.mp4", "content_type": "video/mp4", "bitrate": 832000},
            {"url": "https://video.twimg.com/high.mp4", "content_type": "video/mp4", "bitrate": 10368000},
        ],
    }
    # Best mp4 at or under the cap -- not the 10Mbps master, not the 256k floor.
    assert best_video_variant(item) == "https://video.twimg.com/mid.mp4"
    # HLS is never chosen: a browser cannot play it without a JS player.
    assert "m3u8" not in (best_video_variant(item) or "")
    # Deterministic, because the rewriter has to reach the same answer.
    assert best_video_variant(item) == best_video_variant(dict(item))
    # Every variant over the cap: take the smallest rather than nothing.
    over = {"type": "video", "variants": [
        {"url": "https://video.twimg.com/big.mp4", "content_type": "video/mp4", "bitrate": 9000000},
        {"url": "https://video.twimg.com/huge.mp4", "content_type": "video/mp4", "bitrate": 12000000},
    ]}
    assert best_video_variant(over) == "https://video.twimg.com/big.mp4"
    # Photos and malformed items are not videos.
    assert best_video_variant({"type": "photo", "url": "x"}) is None
    assert best_video_variant({"type": "video", "variants": []}) is None
    assert best_video_variant(None) is None

    # The extractor yields both the playable file and the poster frame.
    urls = photo_urls_from_extras({"media": [item]})
    assert "https://video.twimg.com/mid.mp4" in urls, urls
    assert "https://pbs.twimg.com/poster.jpg" in urls, urls

    # Rewriting points `src` at the local copy and leaves X's CDN untouched.
    out = rewrite_media_blob(
        {"media": [item]},
        {"https://video.twimg.com/mid.mp4": "/media/ab/cd.mp4"},
    )
    assert out["media"][0]["src"] == "/media/ab/cd.mp4"
    assert relative_path_for("https://video.twimg.com/mid.mp4").endswith(".mp4")
    print("fetching.media self-check passed")


if __name__ == "__main__":  # pragma: no cover - manual check
    _self_check()
