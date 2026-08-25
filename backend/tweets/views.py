"""Read APIs: feed, accounts, cycles, and searches."""
from __future__ import annotations

import csv
import json
from datetime import timedelta
from io import StringIO

from django.db import connection
from django.db.models.functions import Coalesce
from django.http import StreamingHttpResponse
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

from .analytics import engagement_expression, normalize_handles

from .models import FetchRun, Search, Tweet, TwitterUser, XSession
from .serializers import (
    AccountOpsSerializer,
    FetchRunDetailSerializer,
    FetchRunSerializer,
    SearchSerializer,
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


def feed_queryset(params):
    tracked = list(
        TwitterUser.objects.filter(tracking=True).values_list("handle", "priority")
    )
    handle_to_priority = {handle: priority for handle, priority in tracked}
    search_ids = Search.objects.filter(enabled=True).values_list("id", flat=True)
    qs = Tweet.objects.filter(account__in=list(handle_to_priority)) | Tweet.objects.filter(
        search_results__search_id__in=list(search_ids)
    )
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
        if connection.vendor == "postgresql":
            from django.contrib.postgres.search import SearchVector

            qs = qs.annotate(_search=SearchVector("text")).filter(_search=query)
        else:
            qs = qs.filter(text__icontains=query)
    qs = with_feed_ts(
        qs.distinct()
        .select_related("author")
        .prefetch_related("search_results__search")
        .defer("payload")
    )
    if str(params.get("sort") or "").lower() == "top":
        # Secondary key is feed_ts, not id: ties on engagement should break by
        # recency, and the offset paginator needs a fully deterministic order.
        return qs.annotate(engagement=engagement_expression()).order_by(
            "-engagement", "-feed_ts", "-id"
        )
    return qs.order_by("-feed_ts", "-id")


class FeedView(ListAPIView):
    """Merged latest tweets from tracked accounts + enabled searches."""

    serializer_class = TweetSerializer

    def get_queryset(self):
        return feed_queryset(self.request.query_params)

    @property
    def pagination_class(self):
        """Cursor paging for `latest`, offset paging for `top`.

        A cursor encodes the ordering field's value, so it needs a unique,
        monotonic column. Engagement is neither -- thousands of tweets share a
        score of 0. The time window on a `top` query bounds the result set, so
        offset paging is safe here in a way it would not be on the whole archive.
        """
        sort = str(self.request.query_params.get("sort") or "").lower()
        return FeedOffsetPagination if sort == "top" else StandardCursorPagination


class AccountTimelineView(ListAPIView):
    """Chronological tweets for a single account handle."""

    serializer_class = TweetSerializer

    def get_queryset(self):
        handle = _normalize_handle(self.kwargs["handle"])
        return with_feed_ts(
            Tweet.objects.filter(account=handle).select_related("author").defer("payload")
        ).order_by("-feed_ts", "-id")


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
        return TwitterUser.objects.all().order_by("priority", "handle")

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
    """List/create searches; creating one enqueues an on-demand fetch."""

    serializer_class = SearchSerializer
    # Anyone signed in may browse saved searches; creating one starts a browser
    # bootstrap against the shared session, so that is an operator action.
    permission_classes = [IsStaffOrReadOnly]
    # No PATCH: it let any authenticated user rewrite another search's raw_query
    # or set enabled=False (silently dropping it from repoll_searches and from
    # everyone's feed), and no UI ever called it. Edit via admin if needed.
    http_method_names = ["get", "post", "head", "options"]
    # Searches paginate on their own non-null created_at; only the `results`
    # action returns Tweets and needs the feed_ts-ordered paginator.
    pagination_class = CreatedAtCursorPagination

    def get_queryset(self):
        qs = Search.objects.all()
        product = self.request.query_params.get("product")
        if product:
            qs = qs.filter(product__iexact=product)
        return qs.order_by("-created_at")

    def perform_create(self, serializer):
        raw_query = serializer.validated_data["raw_query"]
        name = serializer.validated_data.get("name") or raw_query[:60]
        search = serializer.save(
            name=name,
            slug=slugify(name)[:200] or "search",
        )
        from fetching.tasks import run_search

        run_search.delay(search.id)

    @action(detail=True, methods=["get"])
    def results(self, request, pk=None):
        search = self.get_object()
        tweets = with_feed_ts(
            Tweet.objects.filter(search_results__search=search)
            .select_related("author")
            .defer("payload")
        ).order_by("search_results__rank", "-feed_ts")
        paginator = StandardCursorPagination()
        page = paginator.paginate_queryset(tweets, request, view=self)
        serializer = TweetSerializer(page, many=True)
        return paginator.get_paginated_response(serializer.data)

    @action(detail=True, methods=["post"])
    def refresh(self, request, pk=None):
        search = self.get_object()
        from fetching.tasks import run_search

        run_search.delay(search.id)
        return Response({"status": "queued"}, status=status.HTTP_202_ACCEPTED)


class ExportView(APIView):
    """Stream the current feed as JSONL or CSV."""

    renderer_classes = [JSONRenderer]

    def perform_content_negotiation(self, request, force=False):
        return JSONRenderer(), "json"

    def get(self, request):
        fmt = str(request.query_params.get("format") or "jsonl").lower()
        if fmt not in {"jsonl", "csv"}:
            return Response({"detail": "format must be jsonl or csv"}, status=400)
        qs = feed_queryset(request.query_params)

        def rows():
            if fmt == "csv":
                buffer = StringIO()
                writer = csv.writer(buffer)
                writer.writerow(["tweet_id", "account", "created_at", "likes", "retweets", "views", "text", "url"])
                yield buffer.getvalue()
                buffer.seek(0)
                buffer.truncate(0)
                for tweet in qs.iterator(chunk_size=200):
                    writer.writerow([
                        tweet.tweet_id,
                        tweet.account,
                        tweet.created_at.isoformat() if tweet.created_at else "",
                        tweet.likes,
                        tweet.retweets,
                        tweet.views,
                        tweet.text,
                        tweet.url,
                    ])
                    yield buffer.getvalue()
                    buffer.seek(0)
                    buffer.truncate(0)
                return
            for tweet in qs.iterator(chunk_size=200):
                yield json.dumps({
                    "tweet_id": tweet.tweet_id,
                    "account": tweet.account,
                    "created_at": tweet.created_at.isoformat() if tweet.created_at else None,
                    "likes": tweet.likes,
                    "retweets": tweet.retweets,
                    "views": tweet.views,
                    "text": tweet.text,
                    "url": tweet.url,
                }, ensure_ascii=False) + "\n"

        content_type = "text/csv" if fmt == "csv" else "application/x-ndjson"
        filename = f"tweets.{fmt}"
        response = StreamingHttpResponse(rows(), content_type=content_type)
        response["Content-Disposition"] = f'attachment; filename="{filename}"'
        return response
