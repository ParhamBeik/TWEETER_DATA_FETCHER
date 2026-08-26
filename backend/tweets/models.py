"""Postgres models: the sole durable store for tweets, users, searches, and
the canonical fetcher's internal state (raw pages, endpoint watermarks, tx/query
health). No filesystem data/ layer exists in this project.
"""
from __future__ import annotations

from django.db import models


class TwitterUser(models.Model):
    """An X account we know about (tracked = the fetcher polls it)."""

    handle = models.CharField(max_length=100, unique=True)
    rest_id = models.CharField(max_length=64, blank=True, null=True, db_index=True)
    display_name = models.CharField(max_length=255, blank=True, default="")
    avatar_url = models.URLField(max_length=500, blank=True, default="")
    verified = models.BooleanField(default=False)
    verified_type = models.CharField(max_length=64, blank=True, default="")
    tracking = models.BooleanField(default=False)
    priority = models.PositiveSmallIntegerField(default=7)
    quarantined = models.BooleanField(default=False)
    quarantine_reason = models.CharField(max_length=255, blank=True, default="")
    quarantined_at = models.DateTimeField(null=True, blank=True)
    # Set only when a historical-backfill chunk covering this account completes
    # fully (status="completed"); left alone on partial/failed chunks so the
    # account stays at the front of the next chunk's queue instead of losing
    # its place. Ordering by this ascending (nulls first) is what makes the
    # chunked backfill self-resuming across ticks.
    historical_backfilled_at = models.DateTimeField(null=True, blank=True)
    # How often to poll this account, measured from how often it actually posts
    # and clamped into the band its priority allows (see
    # fetching.tasks.recompute_poll_intervals). Null until there is enough
    # history to measure, at which point the tier default applies instead.
    # observed_median_gap_seconds is the raw measurement, kept so the console can
    # show why an interval is what it is.
    poll_interval_seconds = models.PositiveIntegerField(null=True, blank=True)
    observed_median_gap_seconds = models.PositiveIntegerField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    def __str__(self) -> str:  # pragma: no cover - trivial
        return f"@{self.handle}"


class Tweet(models.Model):
    """Normalized tweet. Unique key mirrors merge dedup: author_id:tweet_id."""

    dedup_key = models.CharField(max_length=160, unique=True)
    tweet_id = models.CharField(max_length=64, db_index=True)
    # X author rest_id. Named *_rest_id (not author_id) to avoid clashing with
    # the auto-generated FK column of the `author` relation below.
    author_rest_id = models.CharField(max_length=64, blank=True, null=True, db_index=True)
    account = models.CharField(max_length=100, db_index=True)
    author = models.ForeignKey(
        TwitterUser, on_delete=models.SET_NULL, null=True, blank=True, related_name="tweets"
    )

    text = models.TextField(blank=True, default="")
    url = models.URLField(max_length=500, blank=True, default="")
    type = models.CharField(max_length=20, default="Tweet")

    created_at = models.DateTimeField(db_index=True, null=True, blank=True)
    raw_created_at = models.CharField(max_length=64, blank=True, default="")

    likes = models.BigIntegerField(default=0)
    retweets = models.BigIntegerField(default=0)
    replies = models.BigIntegerField(default=0)
    quotes = models.BigIntegerField(default=0)
    bookmarks = models.BigIntegerField(default=0)
    views = models.BigIntegerField(default=0)

    source_language = models.CharField(max_length=16, blank=True, null=True)
    source_endpoint = models.CharField(max_length=64, blank=True, default="")
    # Which pipeline first captured this tweet: live, historical, or search.
    # source_endpoint cannot answer that -- live and the archive walk both hit
    # UserTweets. Written on insert only (it is deliberately absent from
    # ingest._TWEET_UPDATE_FIELDS), so a live poll re-seeing a backfilled tweet
    # does not rewrite the credit. Blank on rows ingested before this existed.
    source_subsystem = models.CharField(max_length=16, blank=True, default="", db_index=True)
    conversation_id = models.CharField(max_length=64, blank=True, null=True)

    entities = models.JSONField(default=dict, blank=True)
    extras = models.JSONField(default=dict, blank=True)  # media/card/quote/RT for list APIs
    payload = models.JSONField(default=dict, blank=True)  # full normalized dict (debug)

    ingested_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-created_at", "-id"]
        indexes = [
            models.Index(fields=["account", "-created_at"]),
            models.Index(fields=["-created_at", "-id"]),
            # Ingestion analytics bucket on when we saw a tweet, not when it was
            # posted, and split that by which pipeline captured it.
            models.Index(fields=["-ingested_at"]),
            models.Index(fields=["source_subsystem", "-ingested_at"]),
        ]

    def __str__(self) -> str:  # pragma: no cover - trivial
        return f"{self.account}:{self.tweet_id}"


class TweetMetric(models.Model):
    """Point-in-time engagement snapshot written when ingest sees a change."""

    tweet = models.ForeignKey(Tweet, on_delete=models.CASCADE, related_name="metrics")
    likes = models.BigIntegerField(default=0)
    retweets = models.BigIntegerField(default=0)
    views = models.BigIntegerField(default=0)
    captured_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        indexes = [models.Index(fields=["tweet", "-captured_at"], name="tweets_twee_tweet_i_7c1e4a_idx")]


class Search(models.Model):
    """A saved SearchTimeline query (Top or Latest product)."""

    PRODUCT_CHOICES = [("Top", "Top"), ("Latest", "Latest")]

    name = models.CharField(max_length=255)
    slug = models.SlugField(max_length=255)
    raw_query = models.TextField()
    product = models.CharField(max_length=10, choices=PRODUCT_CHOICES, default="Top")
    pagination_depth = models.PositiveSmallIntegerField(default=1)
    # How far back a run keeps paginating. This is the stop condition that ends
    # deep search runs, so it belongs next to depth rather than hardcoded.
    rolling_hours = models.PositiveIntegerField(default=24)
    enabled = models.BooleanField(default=True)
    # Each search now runs as its own task on its own cadence. They used to share
    # one fleet-wide cycle bounded by a single wall-clock timeout, so whichever
    # query came last was killed mid-run every time.
    interval_seconds = models.PositiveIntegerField(default=1800)
    last_run_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        unique_together = [("slug", "product")]

    def __str__(self) -> str:  # pragma: no cover - trivial
        return f"{self.slug} [{self.product}]"


class SearchResult(models.Model):
    """Ordered link between a Search and a Tweet it returned."""

    search = models.ForeignKey(Search, on_delete=models.CASCADE, related_name="results")
    tweet = models.ForeignKey(Tweet, on_delete=models.CASCADE, related_name="search_results")
    rank = models.IntegerField(default=0)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        unique_together = [("search", "tweet")]
        ordering = ["rank", "-id"]


# --- Fetcher internal state (replaces the on-disk data/ layer) --------------


class FetchRun(models.Model):
    STATUS_CHOICES = [
        ("running", "Running"),
        ("completed", "Completed"),
        ("partial", "Partial"),
        ("failed", "Failed"),
        ("auth_required", "Auth required"),
    ]

    run_id = models.CharField(max_length=64, unique=True)
    task_id = models.CharField(max_length=64, blank=True, default="", db_index=True)
    subsystem = models.CharField(max_length=32, db_index=True)
    target = models.CharField(max_length=255, blank=True, default="", db_index=True)
    status = models.CharField(max_length=20, choices=STATUS_CHOICES, default="running", db_index=True)
    return_code = models.IntegerField(null=True, blank=True)
    summary = models.JSONField(default=dict, blank=True)
    failure_ledger = models.JSONField(default=dict, blank=True)
    log_excerpt = models.TextField(blank=True, default="")
    started_at = models.DateTimeField(auto_now_add=True)
    finished_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ["-started_at"]


class RawPage(models.Model):
    """A single raw GraphQL page, keyed like the on-disk batch/page layout."""

    endpoint = models.CharField(max_length=64, db_index=True)
    account = models.CharField(max_length=120, db_index=True)
    batch = models.CharField(max_length=200, db_index=True)
    page_number = models.IntegerField()
    payload = models.JSONField(default=dict)
    # Stays SET_NULL. Raw pages outlive their run by design -- FetchRun has its
    # own 90-day clock and a page can be reaped out from under one -- so their
    # retention is age-based (fetching.tasks.purge_old_raw_pages), not FK-based.
    # CASCADE here would add a second, *unchunked* delete path over the same
    # millions of JSONB rows that purge task deliberately chunks to keep one
    # transaction from bloating WAL on a 1 GB container.
    fetch_run = models.ForeignKey(
        FetchRun, on_delete=models.SET_NULL, null=True, blank=True, related_name="raw_pages"
    )
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        unique_together = [("endpoint", "account", "batch", "page_number")]
        ordering = ["page_number"]
        indexes = [
            models.Index(fields=["created_at"]),
        ]


class EndpointState(models.Model):
    """Per (account, endpoint) state blob: watermark, raw_batch_path, flags."""

    account = models.CharField(max_length=120, db_index=True)
    endpoint = models.CharField(max_length=64, db_index=True)
    data = models.JSONField(default=dict)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        unique_together = [("account", "endpoint")]


class KeyValueState(models.Model):
    """Generic namespaced JSON state (sync_state, user-id cache, tx/query
    health files) previously stored as JSON files under data/.../state/.
    """

    namespace = models.CharField(max_length=64, db_index=True)
    name = models.CharField(max_length=200, db_index=True)
    data = models.JSONField(default=dict)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        unique_together = [("namespace", "name")]


class MediaAsset(models.Model):
    """A photo we copied off X onto the media volume.

    Postgres still owns identity (which remote URL we have); the bytes live on
    a compose volume because the fetcher scratch tree is deleted after every
    run. Unique on the remote URL so a tweet that is re-ingested does not
    download the same file twice.
    """

    remote_url = models.URLField(max_length=1000, unique=True)
    relative_path = models.CharField(max_length=400)
    created_at = models.DateTimeField(auto_now_add=True)
    last_ok_at = models.DateTimeField(null=True, blank=True)

    def __str__(self) -> str:  # pragma: no cover - trivial
        return self.relative_path


class XSession(models.Model):
    """Single shared operator X session (cookies/bearer). One active row."""

    name = models.CharField(max_length=64, default="default", unique=True)
    cookies = models.JSONField(default=dict, blank=True)
    headers = models.JSONField(default=dict, blank=True)
    # Session-bound config the engine reads from config.json but which must never
    # live in the tracked seed template -- chiefly the captured
    # `real_transaction_ids_by_endpoint` pools, which are signed per browser
    # session. Merged over seed/config.example.json by runner._write_config.
    config_overrides = models.JSONField(default=dict, blank=True)
    active = models.BooleanField(default=True)
    updated_at = models.DateTimeField(auto_now=True)
