"""Write a feed export to the media volume, off the request thread.

This used to be a StreamingHttpResponse built inside the view. The queryset it
streams has no row ceiling -- the feed's own filters are the only bound, and
"All time with no filters" is a valid answer -- so a full-archive export held a
gunicorn worker for as long as it took. With `--workers 2`, two concurrent
exports meant nobody could load the console at all.

Everything here runs on the control worker, which already owns the chunked
retention jobs for the same reason: it is the one worker whose latency nobody
is waiting on.
"""
from __future__ import annotations

import csv
import json
import logging
from datetime import timedelta
from pathlib import Path

from django.conf import settings
from django.http import QueryDict
from django.utils import timezone

from tweets.models import ExportJob

logger = logging.getLogger(__name__)

# Where generated files live on the media volume. Deliberately NOT served by
# nginx (see frontend/nginx.conf): a bulk extract of the archive is a different
# thing from one archived photo, and only the former needs an auth check.
EXPORT_SUBDIR = "exports"

# Every engagement column the row carries, not just the three the ranking uses.
# An export that silently drops replies and quotes makes offline analysis
# disagree with the console for no visible reason.
COLUMNS = (
    "tweet_id", "account", "created_at", "type",
    "likes", "retweets", "replies", "quotes", "bookmarks", "views",
    "text", "url",
)


def export_root() -> Path:
    return Path(settings.MEDIA_ROOT) / EXPORT_SUBDIR


def record(tweet, raw_text: bool) -> dict:
    return {
        "tweet_id": tweet.tweet_id,
        "account": tweet.account,
        "created_at": tweet.created_at.isoformat() if tweet.created_at else None,
        "type": tweet.type,
        "likes": tweet.likes,
        "retweets": tweet.retweets,
        "replies": tweet.replies,
        "quotes": tweet.quotes,
        "bookmarks": tweet.bookmarks,
        "views": tweet.views,
        # `raw` returns X's verbatim string, entities and duplicate links intact.
        "text": tweet.text if raw_text else (tweet.text_clean or tweet.text),
        "url": tweet.url,
    }


def filename_for(job: ExportJob) -> str:
    """What the browser saves it as -- readable, unlike the on-disk token."""
    from tweets.analytics import normalize_handles

    params = QueryDict(job.params.get("query", ""))
    accounts = normalize_handles(params.getlist("account"))
    day = job.created_at.strftime("%Y-%m-%d") if job.created_at else "export"
    if accounts:
        tag = "_".join(accounts[:2])
        if len(accounts) > 2:
            tag += f"_plus_{len(accounts) - 2}"
        return f"tweets_{tag}_{day}.{job.fmt}"
    return f"tweets_{day}.{job.fmt}"


def write_export(job: ExportJob) -> ExportJob:
    """Materialize one job's file. Returns the job with its outcome recorded."""
    from tweets.views import feed_queryset

    ExportJob.objects.filter(pk=job.pk).update(status="running")
    params = QueryDict(job.params.get("query", ""))
    raw_text = str(params.get("text") or "clean").lower() == "raw"
    limit = int(settings.EXPORT_MAX_ROWS)

    root = export_root()
    root.mkdir(parents=True, exist_ok=True)
    destination = root / f"{job.token}.{job.fmt}"
    # Written under a temporary name and moved into place, so a reader can never
    # observe a half-written file: the job is only marked completed after the
    # rename, and the download view serves nothing until then.
    partial = destination.with_suffix(destination.suffix + ".part")

    rows = 0
    try:
        queryset = feed_queryset(params)
        with partial.open("w", encoding="utf-8", newline="") as handle:
            writer = csv.writer(handle) if job.fmt == "csv" else None
            if writer is not None:
                writer.writerow(COLUMNS)
            # limit + 1 so "there was more than the ceiling" is distinguishable
            # from "the archive happened to hold exactly the ceiling".
            for tweet in queryset[: limit + 1].iterator(chunk_size=500):
                if rows >= limit:
                    job.truncated = True
                    break
                values = record(tweet, raw_text)
                if writer is not None:
                    writer.writerow(
                        [values[column] if values[column] is not None else "" for column in COLUMNS]
                    )
                else:
                    handle.write(json.dumps(values, ensure_ascii=False) + "\n")
                rows += 1
        partial.replace(destination)
    except Exception as exc:  # pragma: no cover - defensive
        partial.unlink(missing_ok=True)
        logger.exception("export %s failed", job.token[:8])
        ExportJob.objects.filter(pk=job.pk).update(
            status="failed", error=str(exc)[:2000], finished_at=timezone.now()
        )
        job.refresh_from_db()
        return job

    ExportJob.objects.filter(pk=job.pk).update(
        status="completed",
        relative_path=f"{EXPORT_SUBDIR}/{destination.name}",
        row_count=rows,
        truncated=job.truncated,
        finished_at=timezone.now(),
    )
    job.refresh_from_db()
    logger.info(
        "export %s: %d row(s)%s", job.token[:8], rows, " (truncated)" if job.truncated else ""
    )
    return job


def purge_expired(ttl_hours: int) -> int:
    """Delete finished exports and their files past the TTL.

    Both halves matter: the row without the file is a broken download link, and
    the file without the row is bytes nothing will ever reclaim.
    """
    cutoff = timezone.now() - timedelta(hours=ttl_hours)
    root = export_root()
    deleted = 0
    for job in ExportJob.objects.filter(created_at__lt=cutoff):
        if job.relative_path:
            (Path(settings.MEDIA_ROOT) / job.relative_path).unlink(missing_ok=True)
        job.delete()
        deleted += 1
    # Orphans: a file whose row went without it (a crash between the two, or a
    # job deleted by hand). Nothing else would ever remove these.
    if root.exists():
        known = set(
            ExportJob.objects.exclude(relative_path="").values_list("relative_path", flat=True)
        )
        for path in root.iterdir():
            if not path.is_file():
                continue
            if f"{EXPORT_SUBDIR}/{path.name}" in known:
                continue
            if path.stat().st_mtime < cutoff.timestamp():
                path.unlink(missing_ok=True)
    return deleted
