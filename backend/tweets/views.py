"""Read APIs: feed, accounts, cycles, and searches."""
from __future__ import annotations

import hashlib
import secrets
from datetime import timedelta, timezone as dt_timezone
from pathlib import Path

from fetcher.processing import TZ as FEED_TZ

from django.conf import settings
from django.db.models import Count, Exists, OuterRef, Q, Subquery
from django.db.models.functions import Coalesce
from django.http import FileResponse
from django.utils import timezone
from django.utils.text import slugify
from rest_framework import status, viewsets
from rest_framework.decorators import action
from rest_framework.generics import ListAPIView, RetrieveAPIView
from rest_framework.renderers import JSONRenderer
from rest_framework.response import Response
from rest_framework.views import APIView

from fetching.accounts import clamp_priority, clear_live_quarantine, live_state_map

from .permissions import IsStaff, IsStaffOrReadOnly

from config.pagination import (
    CreatedAtCursorPagination,
    FeedOffsetPagination,
    FetchRunCursorPagination,
    StandardCursorPagination,
)

from .analytics import normalize_handles

from .models import ExportJob, FetchRun, Search, SearchTweet, Tweet, TwitterUser, XSession
from .serializers import (
    AccountOpsSerializer,
    FetchRunDetailSerializer,
    FetchRunSerializer,
    SearchSerializer,
    SearchTweetSerializer,
    TweetSerializer,
)


def _normalize_handle(raw: str) -> str:
    return (raw or "").strip().lstrip("@").lower()


def with_feed_ts(qs):
    """Annotate the cursor-ordering field used by StandardCursorPagination.

    `created_at` is when X says the tweet was posted and is nullable when the
    timestamp will not parse; `ingested_at` is when we saw it and is never null.
    Coalescing gives a non-null, monotonic ordering key, so an undated tweet
    sorts by when it arrived instead of being fabricated a "now" timestamp (which
    pinned it to the top of the feed forever) or dropped from the feed entirely.
    """
    return qs.annotate(feed_ts=Coalesce("created_at", "ingested_at"))


# Tweet.type as the engine writes it (fetcher/processing.py), keyed by the
# lowercase name the console uses in its filter chips.
POST_TYPES = {"tweet": "Tweet", "reply": "Reply", "retweet": "Retweet", "quote": "Quote"}

# Sugar over since/until so the console can offer one-click windows.
FEED_WINDOWS = {"1h": 1, "6h": 6, "24h": 24, "7d": 168, "30d": 720}

# Calendar windows, resolved against the Tehran day the collector already counts
# in (fetcher/processing.TZ). "Today" used to be an alias for a rolling 24h,
# which meant that at 00:30 Tehran the feed labelled "Today" was almost entirely
# yesterday. These snap to real boundaries so the label is the truth.
FEED_CALENDAR_WINDOWS = ("today", "week", "month")


def _calendar_start(window: str):
    """Start of the current Tehran day/week/month, as a UTC-aware datetime."""
    local = timezone.now().astimezone(FEED_TZ)
    midnight = local.replace(hour=0, minute=0, second=0, microsecond=0)
    if window == "today":
        start = midnight
    elif window == "week":
        # Tehran / Iranian calendar week begins on Saturday (Shanbeh).
        # Python weekday(): Mon=0..Sat=5, Sun=6.
        # (weekday + 2) % 7 gives days since Saturday (Sat=0, Sun=1, ..., Fri=6).
        days_since_saturday = (midnight.weekday() + 2) % 7
        start = midnight - timedelta(days=days_since_saturday)
    else:
        start = midnight.replace(day=1)
    return start.astimezone(dt_timezone.utc)


def feed_queryset(params):
    """The tracked-account stream: everything the UserTweets collector captured.

    Saved searches are deliberately absent. They used to be unioned in here, so
    the feed was a blend of two collectors with different cadences, different
    retention and different meanings of "why is this post here" -- and a query
    the operator had merely saved silently rewrote everyone's feed. Search hits
    now live in their own tables and are read through /api/searches/{id}/results/.
    """
    tracked = list(
        TwitterUser.objects.filter(tracking=True).values_list("handle", "priority")
    )
    handle_to_priority = {handle: priority for handle, priority in tracked}
    # Posts whose account has since been untracked are still archived (Tweet has
    # no TTL) but used to be unreachable from every screen while still being
    # counted in "archive total". Default stays tracked-only so the feed keeps
    # meaning "the timelines you follow"; ?include_untracked=1 opens the rest.
    if str(params.get("include_untracked") or "") in {"1", "true", "yes"}:
        qs = Tweet.objects.all()
    else:
        qs = Tweet.objects.filter(account__in=list(handle_to_priority))
    # Repeatable ?account=, matching the analytics endpoints. params.get() would
    # silently keep only the last value, so a two-account selection returned one
    # account's posts.
    accounts = normalize_handles(
        params.getlist("account") if hasattr(params, "getlist") else [params.get("account")]
    )
    if accounts:
        qs = qs.filter(account__in=accounts)
    requested = [name.strip() for name in str(params.get("types") or "").lower().split(",")]
    types = [POST_TYPES[name] for name in requested if name in POST_TYPES]
    if types:
        qs = qs.filter(type__in=types)
    if str(params.get("has_media") or "") in {"1", "true", "yes"}:
        # extras["media"] is the normalized media list; a tweet without media
        # stores [] there, and rows predating extras store null.
        qs = qs.exclude(extras__media=[]).exclude(extras__media=None)
    window = str(params.get("window") or "").lower()
    if window in FEED_WINDOWS:
        qs = qs.filter(created_at__gte=timezone.now() - timedelta(hours=FEED_WINDOWS[window]))
    elif window in FEED_CALENDAR_WINDOWS:
        qs = qs.filter(created_at__gte=_calendar_start(window))
    tier = params.get("tier")
    if tier:
        try:
            priority = int(tier)
            tier_handles = [
                handle for handle, value in handle_to_priority.items() if value == priority
            ]
            qs = qs.filter(account__in=tier_handles)
        except ValueError:
            pass
    since = params.get("since")
    if since:
        qs = qs.filter(created_at__gte=since)
    until = params.get("until")
    if until:
        qs = qs.filter(created_at__lte=until)
    run_id = params.get("run_id")
    if run_id:
        run = FetchRun.objects.filter(run_id=run_id).first()
        if run is not None:
            qs = qs.filter(ingested_at__gte=run.started_at)
            if run.finished_at:
                qs = qs.filter(ingested_at__lte=run.finished_at)
    query = (params.get("q") or "").strip()
    if query:
        # Substring, not full-text. `to_tsvector` matching meant "the", "and" and
        # "a" returned an empty archive (they are English stop words) while
        # "bitco" could never find "Bitcoin", because a lexeme is not a prefix.
        # A search box should behave like Ctrl+F. Matching on text_clean rather
        # than text means a search for "R&D" is not defeated by the stored
        # "R&amp;D"; the trgm index from migration 0017 keeps it fast, and
        # icontains spells the same on SQLite so tests exercise real semantics.
        qs = qs.filter(text_clean__icontains=query)
    # No .distinct() any more: the union with search results was the only thing
    # that could produce a duplicate row, and distinct() over a deferred JSONB
    # payload is expensive on a table this size.
    qs = with_feed_ts(qs.select_related("author").defer("payload"))
    sort = str(params.get("sort") or "").lower()
    # Both ranked sorts order on a stored, indexed column and break ties on id.
    #
    # `top` used to annotate the engagement expression and sort on that, which
    # no index can serve -- over the All-time window the console offers, that
    # was a full scan and sort of the archive on every request. Tweet.engagement
    # is now a persisted generated column with its own index.
    #
    # The tiebreaker is id, not feed_ts: the index is (-engagement, -id), and a
    # middle key the index does not carry forces a sort of every tied group.
    # Ties here are tweets with identical scores, where recency-vs-id is not a
    # meaningful distinction anyway.
    if sort == "top":
        return qs.order_by("-engagement", "-id")
    if sort == "views":
        # Reach, asked as its own question rather than folded into engagement.
        return qs.order_by("-views", "-id")
    return qs.order_by("-feed_ts", "-id")


class FeedView(ListAPIView):
    """Latest tweets from tracked accounts. Saved searches have their own page."""

    serializer_class = TweetSerializer

    def get_queryset(self):
        return feed_queryset(self.request.query_params)

    @property
    def pagination_class(self):
        """Cursor paging for `latest`, offset paging for `top` and `views`.

        A cursor encodes the ordering field's value, so it needs a unique,
        monotonic column. Engagement and views are neither -- thousands of
        tweets share a score of 0. The time window on a ranked query bounds the
        result set, so offset paging is safe here in a way it would not be on
        the whole archive.
        """
        sort = str(self.request.query_params.get("sort") or "").lower()
        return FeedOffsetPagination if sort in {"top", "views"} else StandardCursorPagination


class AccountTimelineView(ListAPIView):
    """Chronological tweets for a single account handle."""

    serializer_class = TweetSerializer

    def get_queryset(self):
        handle = _normalize_handle(self.kwargs["handle"])
        return with_feed_ts(
            Tweet.objects.filter(account=handle).select_related("author").defer("payload")
        ).order_by("-feed_ts", "-id")


# Bounds for the two ways of looking past the tracked roster.
ACCOUNT_SEARCH_LIMIT = 50
ACCOUNT_BROWSE_LIMIT = 200


class AccountViewSet(viewsets.ModelViewSet):
    """Operator account list: tiers, tracking, quarantine, on-demand fetch."""

    serializer_class = AccountOpsSerializer
    # Reading the roster is fine for any signed-in user; changing tiers, tracking
    # or quarantine, and triggering a fetch all spend the shared X rate budget.
    permission_classes = [IsStaffOrReadOnly]
    lookup_field = "handle"
    pagination_class = None
    http_method_names = ["get", "post", "patch", "head", "options"]

    def get_queryset(self):
        """Tracked accounts by default; the rest only when explicitly asked for.

        TwitterUser holds every author the collector has ever parsed -- 2.4k rows,
        of which ~64 are tracked. Returning all of them unpaginated meant a 1.2MB
        response on every page load and a roster page rendering 2409 rows and
        7000 tier buttons, with no way to find an account except scrolling.

        Stays a plain list rather than a paginated envelope: the roster is small
        once it is actually the roster, and the feed's account picker consumes
        this endpoint too.
        """
        qs = TwitterUser.objects.all().order_by("priority", "handle")
        params = self.request.query_params
        search = (params.get("q") or "").strip().lstrip("@")
        tracking = str(params.get("tracking") or "").lower()
        if search:
            # Searching is how you find an untracked account to start tracking,
            # so it deliberately spans the whole table -- but bounded.
            qs = qs.filter(
                Q(handle__icontains=search) | Q(display_name__icontains=search)
            )
            if tracking in {"1", "true", "tracked"}:
                qs = qs.filter(tracking=True)
            return qs[:ACCOUNT_SEARCH_LIMIT]
        if tracking in {"all", "any"}:
            return qs[:ACCOUNT_BROWSE_LIMIT]
        if tracking in {"0", "false", "untracked"}:
            return qs.filter(tracking=False)[:ACCOUNT_BROWSE_LIMIT]
        return qs.filter(tracking=True)

    def get_serializer_context(self):
        context = super().get_serializer_context()
        context["live_state"] = live_state_map()
        return context

    def create(self, request, *args, **kwargs):
        handle = _normalize_handle(request.data.get("handle") or "")
        if not handle:
            return Response({"detail": "handle required"}, status=400)
        priority = clamp_priority(request.data.get("priority", 7))
        account, _created = TwitterUser.objects.update_or_create(
            handle=handle,
            defaults={
                "display_name": request.data.get("display_name") or handle,
                "tracking": True,
                "priority": priority,
                "quarantined": False,
                "quarantine_reason": "",
                "quarantined_at": None,
            },
        )
        from fetching.tasks import fetch_account_historical, fetch_account_live

        fetch_account_historical.delay(handle)
        fetch_account_live.delay(handle)
        return Response(
            AccountOpsSerializer(account, context=self.get_serializer_context()).data,
            status=status.HTTP_201_CREATED,
        )

    def partial_update(self, request, *args, **kwargs):
        account = self.get_object()
        fields = []
        if "tracking" in request.data:
            account.tracking = bool(request.data.get("tracking"))
            fields.append("tracking")
        if "priority" in request.data:
            account.priority = clamp_priority(request.data.get("priority"))
            fields.append("priority")
        if "display_name" in request.data:
            account.display_name = str(request.data.get("display_name") or "")
            fields.append("display_name")
        if request.data.get("quarantined") is False:
            account.quarantined = False
            account.quarantine_reason = ""
            account.quarantined_at = None
            fields.extend(["quarantined", "quarantine_reason", "quarantined_at"])
            clear_live_quarantine(account.handle)
        if fields:
            account.save(update_fields=list(dict.fromkeys(fields)))
        return Response(AccountOpsSerializer(account, context=self.get_serializer_context()).data)

    @action(detail=True, methods=["post"])
    def fetch(self, request, handle=None):
        from fetching.tasks import fetch_account_historical, fetch_account_live

        fetch_account_live.delay(handle)
        fetch_account_historical.delay(handle)
        return Response({"status": "queued"}, status=status.HTTP_202_ACCEPTED)


class FetchRunListView(ListAPIView):
    """Recent durable fetch lifecycle and failure-ledger records."""

    serializer_class = FetchRunSerializer
    pagination_class = FetchRunCursorPagination

    def get_queryset(self):
        queryset = FetchRun.objects.all()
        subsystem = self.request.query_params.get("subsystem")
        if subsystem:
            queryset = queryset.filter(subsystem=subsystem)
        return queryset.order_by("-started_at", "-id")


class FetchRunDetailView(RetrieveAPIView):
    serializer_class = FetchRunDetailSerializer
    lookup_field = "run_id"
    queryset = FetchRun.objects.all()


class CycleView(APIView):
    """Trigger one global live/historical/search cycle."""

    permission_classes = [IsStaff]

    def post(self, request):
        subsystem = str(request.data.get("subsystem") or "").lower()
        from fetching.tasks import backfill_historical_all, poll_live_all, repoll_searches

        tasks = {
            "live": poll_live_all,
            "historical": backfill_historical_all,
            "search": repoll_searches,
        }
        task = tasks.get(subsystem)
        if task is None:
            return Response({"detail": "subsystem must be live, historical, or search"}, status=400)
        task.delay()
        return Response({"status": "queued", "subsystem": subsystem}, status=status.HTTP_202_ACCEPTED)


class XSessionView(APIView):
    """Read safe session health or replace the active operator session."""

    # Staff-only including the read: session health names the X account the
    # shared session belongs to and when it was last refreshed.
    permission_classes = [IsStaff]

    def get(self, request):
        from fetching.session import session_health

        return Response(session_health())

    def post(self, request):
        from fetching.session import (
            normalize_session_source,
            validate_config_overrides,
            validate_session_payload,
        )

        # Accepts a whole exported config.json (api_cookies/api_auth) or the
        # session shape (cookies/headers); session-bound config keys are kept,
        # everything else falls back to the seed template.
        payload = normalize_session_source(request.data)
        try:
            cookies, headers = validate_session_payload(payload)
        except ValueError as exc:
            return Response({"detail": str(exc)}, status=400)
        overrides = validate_config_overrides(payload)
        defaults = {"cookies": cookies, "headers": headers, "active": True}
        if overrides:
            defaults["config_overrides"] = overrides
        session, _ = XSession.objects.update_or_create(name="default", defaults=defaults)
        XSession.objects.exclude(pk=session.pk).update(active=False)
        return Response({
            "status": "updated",
            "cookie_count": len(cookies),
            "header_count": len(headers),
            "override_keys": sorted(overrides),
        })


class SearchViewSet(viewsets.ModelViewSet):
    """A saved search as a manageable unit of work.

    Beyond list/create: edit, pause, run now, inspect the schedule and run
    history, and delete -- where delete really means the whole job behind the
    phrase goes away (see fetching.searches.teardown_search).
    """

    serializer_class = SearchSerializer
    # Anyone signed in may browse saved searches; every write here either spends
    # X quota or changes what the collector does, so all of them are staff. PATCH
    # and DELETE were previously blocked outright because the viewset predated
    # this gate and left them open to any authenticated user.
    permission_classes = [IsStaffOrReadOnly]
    http_method_names = ["get", "post", "patch", "delete", "head", "options"]
    # Searches paginate on their own non-null created_at; only the `results`
    # action returns tweets and needs the feed_ts-ordered paginator.
    pagination_class = CreatedAtCursorPagination

    def get_queryset(self):
        qs = Search.objects.annotate(
            hit_count_annotated=Count("hits", distinct=True),
            # Resolved here so a list of N searches costs one query rather than
            # N -- schedule_for takes the flag pre-computed for exactly this.
            is_running_annotated=Exists(
                FetchRun.objects.filter(search=OuterRef("pk"), status="running")
            ),
            # Which run is this query's newest, as a pk the serializer loads for
            # the whole page in one query. `last_run` was the one field on this
            # list still costing a query per row.
            last_run_pk=Subquery(
                FetchRun.objects.filter(search=OuterRef("pk"))
                .order_by("-started_at", "-id")
                .values("pk")[:1]
            ),
        )
        product = self.request.query_params.get("product")
        if product:
            qs = qs.filter(product__iexact=product)
        return qs.order_by("-created_at")

    def perform_create(self, serializer):
        raw_query = serializer.validated_data["raw_query"]
        name = serializer.validated_data.get("name") or raw_query[:60]
        base_slug = slugify(name, allow_unicode=True)[:180]
        if not base_slug:
            base_slug = f"search-{hashlib.sha256(raw_query.encode()).hexdigest()[:8]}"
        slug = base_slug
        product = serializer.validated_data.get("product") or "Top"
        counter = 1
        while Search.objects.filter(slug=slug, product=product).exists():
            slug = f"{base_slug[:170]}-{counter}"
            counter += 1
        search = serializer.save(
            name=name,
            slug=slug,
        )
        from fetching.tasks import run_search

        # Run immediately rather than waiting up to one dispatch interval: a
        # query you just wrote should start collecting while you are still
        # looking at it. The recurring schedule takes over from there.
        result = run_search.delay(search.id)
        Search.objects.filter(id=search.id).update(queued_task_id=str(result.id or ""))

    def perform_destroy(self, instance):
        from fetching.searches import teardown_search

        teardown_search(instance)

    def destroy(self, request, *args, **kwargs):
        from fetching.searches import teardown_search

        # Report what went, rather than a bare 204. Deleting a search removes
        # results and history an operator cannot get back, so the response says
        # exactly what it cost.
        return Response(teardown_search(self.get_object()))

    @action(detail=True, methods=["get"])
    def results(self, request, pk=None):
        search = self.get_object()
        # Newest first, not by SearchHit.rank. CursorPagination replaces the
        # queryset's ordering with its own (StandardCursorPagination.ordering),
        # so an order_by here would be silently ignored -- as it was before this
        # split, where the same dead `order_by("search_results__rank")` gave the
        # impression results came back in X's order and they never did.
        # A rank-ordered view needs offset paging, the way the feed's `top` sort
        # does. ponytail: not built until someone wants it.
        # No .distinct(): (search, search_tweet) is unique, so the join cannot
        # duplicate a row.
        tweets = with_feed_ts(
            SearchTweet.objects.filter(hits__search=search)
            .select_related("author")
            .defer("payload")
        )
        paginator = StandardCursorPagination()
        page = paginator.paginate_queryset(tweets, request, view=self)
        return paginator.get_paginated_response(SearchTweetSerializer(page, many=True).data)

    @action(detail=True, methods=["get"])
    def runs(self, request, pk=None):
        """This phrase's fetch history: what ran, how it ended, what it cost."""
        search = self.get_object()
        queryset = FetchRun.objects.filter(search=search).order_by("-started_at", "-id")
        paginator = FetchRunCursorPagination()
        page = paginator.paginate_queryset(queryset, request, view=self)
        return paginator.get_paginated_response(FetchRunSerializer(page, many=True).data)

    @action(detail=True, methods=["get"])
    def schedule(self, request, pk=None):
        """The recurring job behind this phrase, cheap enough to poll."""
        from fetching.searches import schedule_for

        return Response(schedule_for(self.get_object()))

    @action(detail=True, methods=["post"], permission_classes=[IsStaff])
    def pause(self, request, pk=None):
        """Stop or resume the schedule without losing the query or its results."""
        search = self.get_object()
        search.enabled = not search.enabled
        search.save(update_fields=["enabled"])
        from fetching.searches import schedule_for

        return Response(schedule_for(search))

    @action(detail=True, methods=["post"])
    def refresh(self, request, pk=None):
        search = self.get_object()
        from fetching.tasks import run_search

        result = run_search.delay(search.id)
        Search.objects.filter(id=search.id).update(queued_task_id=str(result.id or ""))
        return Response({"status": "queued"}, status=status.HTTP_202_ACCEPTED)


def _export_payload(job: ExportJob) -> dict:
    from fetching.exports import filename_for

    return {
        "id": job.id,
        "status": job.status,
        "format": job.fmt,
        "row_count": job.row_count,
        # True means the row ceiling was reached and this is a prefix of the
        # answer, not the answer. The console has to be able to say so.
        "truncated": job.truncated,
        "error": job.error,
        "filename": filename_for(job),
        "created_at": job.created_at,
        "finished_at": job.finished_at,
        "download_url": (
            f"/api/export/{job.id}/download/" if job.status == "completed" else None
        ),
    }


class ExportView(APIView):
    """Request a feed export, and list your own.

    POST queues the work and returns immediately. It used to stream the whole
    result set out of this view, which meant one download occupied a gunicorn
    worker for its full duration -- and with `--workers 2`, two concurrent
    full-archive exports left nothing to serve the console with. The queryset
    has no row ceiling of its own, so there was no size at which this stopped
    being possible.
    """

    renderer_classes = [JSONRenderer]
    throttle_scope = "exports"

    def perform_content_negotiation(self, request, force=False):
        return JSONRenderer(), "json"

    def get(self, request):
        """This user's recent exports, newest first."""
        jobs = ExportJob.objects.filter(requested_by=request.user)[:20]
        return Response({"results": [_export_payload(job) for job in jobs]})

    def post(self, request):
        fmt = str(
            request.data.get("format") or request.query_params.get("format") or "jsonl"
        ).lower()
        if fmt not in {"jsonl", "csv"}:
            return Response({"detail": "format must be jsonl or csv"}, status=400)
        # The filters are stored verbatim, as a query string, so the worker
        # rebuilds exactly the queryset the operator was looking at -- including
        # repeatable ?account=, which a dict would flatten to its last value.
        query = request.data.get("query")
        if query is None:
            query = request.query_params.urlencode()
        job = ExportJob.objects.create(
            token=secrets.token_urlsafe(32),
            fmt=fmt,
            params={"query": str(query)},
            requested_by=request.user,
        )
        from fetching.tasks import run_export

        run_export.delay(job.id)
        job.refresh_from_db()
        return Response(_export_payload(job), status=status.HTTP_202_ACCEPTED)


class ExportDetailView(APIView):
    """Poll one export's progress."""

    renderer_classes = [JSONRenderer]

    def get(self, request, pk):
        job = ExportJob.objects.filter(pk=pk, requested_by=request.user).first()
        if job is None:
            return Response({"detail": "not found"}, status=404)
        return Response(_export_payload(job))


class ExportDownloadView(APIView):
    """Serve a finished export.

    Through Django rather than nginx on purpose. An export is a bulk extract of
    the archive, unlike a single archived photo, and an unguessable URL is a
    bearer token: it survives in browser history, referrer headers and proxy
    logs long after the person who generated it stopped needing it.
    """

    renderer_classes = [JSONRenderer]

    def get(self, request, pk):
        from fetching.exports import filename_for

        job = ExportJob.objects.filter(pk=pk, requested_by=request.user).first()
        if job is None:
            return Response({"detail": "not found"}, status=404)
        if job.status != "completed" or not job.relative_path:
            return Response({"detail": f"export is {job.status}"}, status=409)
        path = Path(settings.MEDIA_ROOT) / job.relative_path
        if not path.is_file():
            # The TTL purge reached the file. Say so rather than 500ing on the
            # open, so the console can offer to run it again.
            return Response({"detail": "export has expired"}, status=410)
        return FileResponse(
            path.open("rb"),
            as_attachment=True,
            filename=filename_for(job),
            content_type="text/csv" if job.fmt == "csv" else "application/x-ndjson",
        )
