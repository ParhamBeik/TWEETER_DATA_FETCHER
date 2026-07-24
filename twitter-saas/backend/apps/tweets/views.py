"""Read APIs: feed, per-account timeline, and searches (Top/Latest)."""
from __future__ import annotations

from django.utils.text import slugify
from rest_framework import status, viewsets
from rest_framework.decorators import action
from rest_framework.generics import ListAPIView
from rest_framework.response import Response

from apps.accounts.models import Follow, SearchSubscription

from .models import Search, Tweet
from .serializers import SearchSerializer, TweetSerializer


class FeedView(ListAPIView):
    """Merged latest tweets from the user's followed accounts + subscribed searches."""

    serializer_class = TweetSerializer

    def get_queryset(self):
        user = self.request.user
        followed = Follow.objects.filter(user=user).values_list("account__handle", flat=True)
        sub_search_ids = SearchSubscription.objects.filter(user=user).values_list("search_id", flat=True)
        qs = Tweet.objects.filter(account__in=list(followed)) | Tweet.objects.filter(
            search_results__search_id__in=list(sub_search_ids)
        )
        return qs.distinct().order_by("-created_at", "-id")


class AccountTimelineView(ListAPIView):
    """Chronological tweets for a single account handle."""

    serializer_class = TweetSerializer

    def get_queryset(self):
        handle = self.kwargs["handle"].lstrip("@")
        return Tweet.objects.filter(account__iexact=handle).order_by("-created_at", "-id")


class SearchViewSet(viewsets.ModelViewSet):
    """List/create searches; creating one enqueues an on-demand fetch."""

    serializer_class = SearchSerializer

    def get_queryset(self):
        # Scope to the requesting user's own searches — no cross-user leak.
        qs = Search.objects.filter(owner=self.request.user)
        product = self.request.query_params.get("product")
        if product:
            qs = qs.filter(product__iexact=product)
        return qs.order_by("-created_at")

    def perform_create(self, serializer):
        raw_query = serializer.validated_data["raw_query"]
        name = serializer.validated_data.get("name") or raw_query[:60]
        # Pass the derived `name` through to save() — otherwise the model's
        # NOT NULL `name` column gets an empty string (the serializer's
        # allow_blank fallback never reaches the instance on its own).
        search = serializer.save(
            owner=self.request.user,
            name=name,
            slug=slugify(name)[:200] or "search",
        )
        SearchSubscription.objects.get_or_create(user=self.request.user, search=search)
        from apps.fetching.tasks import run_search

        run_search.delay(search.id)

    @action(detail=True, methods=["get"])
    def results(self, request, pk=None):
        search = self.get_object()
        tweets = Tweet.objects.filter(search_results__search=search).order_by(
            "search_results__rank", "-created_at"
        )
        page = self.paginate_queryset(tweets)
        serializer = TweetSerializer(page, many=True)
        return self.get_paginated_response(serializer.data)

    @action(detail=True, methods=["post"])
    def refresh(self, request, pk=None):
        search = self.get_object()
        from apps.fetching.tasks import run_search

        run_search.delay(search.id)
        return Response({"status": "queued"}, status=status.HTTP_202_ACCEPTED)
