from django.contrib import admin

from .models import (
    EndpointState,
    FetchRun,
    KeyValueState,
    RawPage,
    Search,
    SearchResult,
    Tweet,
    TwitterUser,
    XSession,
)


@admin.register(TwitterUser)
class TwitterUserAdmin(admin.ModelAdmin):
    list_display = ("handle", "display_name", "priority", "verified", "tracking", "quarantined", "created_at")
    list_filter = ("tracking", "priority", "verified", "quarantined")
    search_fields = ("handle", "rest_id", "display_name")


@admin.register(Tweet)
class TweetAdmin(admin.ModelAdmin):
    list_display = ("tweet_id", "account", "type", "created_at", "likes", "retweets")
    list_filter = ("type", "source_endpoint")
    search_fields = ("tweet_id", "account", "text")
    date_hierarchy = "created_at"


@admin.register(Search)
class SearchAdmin(admin.ModelAdmin):
    list_display = ("slug", "product", "enabled", "last_run_at")
    list_filter = ("product", "enabled")
    search_fields = ("slug", "name", "raw_query")


@admin.register(SearchResult)
class SearchResultAdmin(admin.ModelAdmin):
    list_display = ("search", "tweet", "rank")


@admin.register(XSession)
class XSessionAdmin(admin.ModelAdmin):
    list_display = ("name", "active", "updated_at")


admin.site.register(RawPage)
admin.site.register(EndpointState)
admin.site.register(KeyValueState)


@admin.register(FetchRun)
class FetchRunAdmin(admin.ModelAdmin):
    """Redacts log_excerpt, which the bare registration rendered in the clear.

    Runs written after the redact-on-persist change are already clean; this
    covers rows stored before it.
    """

    list_display = ("run_id", "subsystem", "target", "status", "started_at", "finished_at")
    list_filter = ("subsystem", "status")
    search_fields = ("run_id", "target")
    exclude = ("log_excerpt",)
    readonly_fields = ("redacted_log_excerpt",)

    @admin.display(description="Log excerpt (redacted)")
    def redacted_log_excerpt(self, obj):
        from apps.fetching.redaction import redact_text

        return redact_text(obj.log_excerpt)
