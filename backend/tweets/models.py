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
    # Verbatim `text` is the archive; `text_clean` is the readable derivation of
    # it (HTML entities decoded, X's repeated t.co collapsed -- see
    # tweets/textclean.py). The feed, search and topic mining read this one so
    # they are not matching against "&amp;"; exports can offer either.
    text_clean = models.TextField(blank=True, default="")
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

    # Deliberate actions only -- the same four fields as
    # analytics.ENGAGEMENT_FIELDS, and views is excluded there for the same
    # reason: an impression is not an interaction, and views run 100-1000x
    # larger, so summing them in makes "most engaged" a synonym for "most
    # viewed". Reach is its own question and has its own sort.
    #
    # Stored rather than computed per query. The feed offers this as a sort over
    # an unbounded window, and an expression sort has no index to use, so
    # ranking the archive meant a full scan plus a sort every time. A persisted
    # generated column can be indexed; the database keeps it in step, so there is
    # no second write path to forget.
    engagement = models.GeneratedField(
        expression=models.F("likes") + models.F("retweets") + models.F("replies") + models.F("quotes"),
        output_field=models.BigIntegerField(),
        db_persist=True,
    )

    class Meta:
        ordering = ["-created_at", "-id"]
        indexes = [
            models.Index(fields=["account", "-created_at"]),
            models.Index(fields=["-created_at", "-id"]),
            # Ingestion analytics bucket on when we saw a tweet, not when it was
            # posted, and split that by which pipeline captured it.
            models.Index(fields=["-ingested_at"]),
            models.Index(fields=["source_subsystem", "-ingested_at"]),
            # The feed's two ranked sorts. Both are offered over "All time", so
            # without these each one scanned and sorted the whole table.
            models.Index(fields=["-engagement", "-id"], name="tweets_engagement_rank_idx"),
            models.Index(fields=["-views", "-id"], name="tweets_views_rank_idx"),
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
        indexes = [
            models.Index(fields=["tweet", "-captured_at"], name="tweets_twee_tweet_i_7c1e4a_idx"),
            # The velocity views scan this table by capture time across every
            # tweet ("what gained engagement during this window"), which the
            # composite above cannot serve -- it leads on tweet_id, so a bare
            # range on captured_at falls back to a full scan of what is already
            # one of the fastest-growing tables here.
            models.Index(fields=["captured_at"], name="tweets_metric_captured_idx"),
        ]


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
    # Celery id of the run this search has waiting in the queue, stamped by
    # dispatch_due_searches and cleared when run_search picks it up. Deleting a
    # search revokes it; without the id there is no way to stop work that was
    # queued for a query the operator has since removed.
    queued_task_id = models.CharField(max_length=64, blank=True, default="")
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        unique_together = [("slug", "product")]

    def __str__(self) -> str:  # pragma: no cover - trivial
        return f"{self.slug} [{self.product}]"


class SearchTweet(models.Model):
    """A SearchTimeline hit.

    Physically separate from `Tweet`: the two come from different endpoints on
    different cadences with different retention, and mixing them made the feed a
    blend of two unrelated collectors. `Tweet` is what the account collector
    (UserTweets) owns; everything here arrived through a saved search.

    No `source_subsystem` column -- for this table it is always "search".
    """

    dedup_key = models.CharField(max_length=160, unique=True)
    tweet_id = models.CharField(max_length=64, db_index=True)
    author_rest_id = models.CharField(max_length=64, blank=True, null=True, db_index=True)
    account = models.CharField(max_length=100, db_index=True)
    author = models.ForeignKey(
        TwitterUser, on_delete=models.SET_NULL, null=True, blank=True, related_name="search_tweets"
    )

    text = models.TextField(blank=True, default="")
    # Verbatim `text` is the archive; `text_clean` is the readable derivation of
    # it (HTML entities decoded, X's repeated t.co collapsed -- see
    # tweets/textclean.py). The feed, search and topic mining read this one so
    # they are not matching against "&amp;"; exports can offer either.
    text_clean = models.TextField(blank=True, default="")
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
    source_endpoint = models.CharField(max_length=64, blank=True, default="SearchTimeline")
    conversation_id = models.CharField(max_length=64, blank=True, null=True)

    entities = models.JSONField(default=dict, blank=True)
    extras = models.JSONField(default=dict, blank=True)
    payload = models.JSONField(default=dict, blank=True)

    ingested_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-created_at", "-id"]
        indexes = [
            models.Index(fields=["-created_at", "-id"]),
            # The Dashboard's collection-flow chart buckets search capture on
            # when we saw the hit, the same way it does for `Tweet`.
            models.Index(fields=["-ingested_at"]),
        ]

    def __str__(self) -> str:  # pragma: no cover - trivial
        return f"{self.account}:{self.tweet_id}"


class SearchHit(models.Model):
    """Ordered link between a Search and a SearchTweet it returned.

    One body per tweet, one hit per (search, tweet): two queries that both match
    a post share the row rather than storing the payload twice.
    """

    search = models.ForeignKey(Search, on_delete=models.CASCADE, related_name="hits")
    search_tweet = models.ForeignKey(
        SearchTweet, on_delete=models.CASCADE, related_name="hits"
    )
    rank = models.IntegerField(default=0)
    first_seen_at = models.DateTimeField(auto_now_add=True)
    last_seen_at = models.DateTimeField(auto_now=True)

    class Meta:
        unique_together = [("search", "search_tweet")]
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
    # Which saved search this run was for, when it was one. SET_NULL rather than
    # CASCADE only in shape -- teardown_search deletes these rows explicitly --
    # but a run must never be able to block deleting the query it ran for.
    # `target` already spells "<slug>:<product>"; this makes "that phrase's run
    # history" one query instead of a string parse against a mutable slug.
    search = models.ForeignKey(
        "Search", on_delete=models.SET_NULL, null=True, blank=True, related_name="runs"
    )
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


class PendingMedia(models.Model):
    """A media URL known to need downloading, but not downloaded yet.

    The archiver used to *search* for work: every tick it stat-ed every
    MediaAsset file and then full-scanned the tweet table, loading the JSONB
    extras column, to find at most a handful of missing URLs. It restarted from
    the top each time, so once the front of the archive was complete it re-walked
    all of it before reaching anything new -- O(archive) work every 120 seconds,
    forever, to do a constant amount of downloading.

    Recording the work at ingest instead makes the archiver a queue consumer.

    `attempts` is what keeps a permanently-dead URL (deleted media, a 404) from
    sitting at the head of the queue being retried on every tick for the life of
    the deployment. Rows are kept after they die rather than deleted, so the
    reason is inspectable and a re-enqueue does not silently resurrect them.
    """

    remote_url = models.URLField(max_length=1000, unique=True)
    # Which tweet first wanted it, for diagnosis only -- the same URL can be
    # reachable from several tweets, and the first one to ask is enough context.
    tweet_id = models.CharField(max_length=64, blank=True, default="")
    attempts = models.PositiveSmallIntegerField(default=0)
    last_error = models.CharField(max_length=500, blank=True, default="")
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        # Oldest first: the queue drains in the order things were seen, so a
        # burst of new posts cannot starve what was already waiting.
        ordering = ["id"]
        indexes = [models.Index(fields=["attempts", "id"], name="tweets_pendingmedia_idx")]

    def __str__(self) -> str:  # pragma: no cover - trivial
        return self.remote_url


class ExportJob(models.Model):
    """One requested feed export, produced off the request thread.

    Exporting used to stream the feed queryset straight out of the view. With no
    row ceiling and only two gunicorn workers, two people exporting the archive
    at once occupied both and the console stopped answering for everyone. The
    work now runs on the control worker, which already owns the long, chunked
    maintenance jobs, and the request thread only ever creates this row.

    `token` is what names the file on disk. It is random rather than derived
    from the id so a file cannot be found by guessing a sequence -- though the
    download still goes through an authenticated view, because an unguessable
    URL is a bearer token that leaks through history and proxy logs.
    """

    STATUS_CHOICES = [
        ("pending", "Pending"),
        ("running", "Running"),
        ("completed", "Completed"),
        ("failed", "Failed"),
    ]
    FORMAT_CHOICES = [("jsonl", "JSONL"), ("csv", "CSV")]

    token = models.CharField(max_length=64, unique=True)
    fmt = models.CharField(max_length=8, choices=FORMAT_CHOICES, default="jsonl")
    # The feed filters this export was asked for, stored verbatim so the worker
    # rebuilds exactly the queryset the operator was looking at.
    params = models.JSONField(default=dict, blank=True)
    requested_by = models.ForeignKey(
        "auth.User", on_delete=models.CASCADE, related_name="export_jobs"
    )
    status = models.CharField(max_length=16, choices=STATUS_CHOICES, default="pending", db_index=True)
    relative_path = models.CharField(max_length=400, blank=True, default="")
    row_count = models.IntegerField(default=0)
    truncated = models.BooleanField(default=False)
    error = models.TextField(blank=True, default="")
    created_at = models.DateTimeField(auto_now_add=True, db_index=True)
    finished_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ["-created_at"]

    def __str__(self) -> str:  # pragma: no cover - trivial
        return f"export {self.token[:8]} ({self.status})"


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
