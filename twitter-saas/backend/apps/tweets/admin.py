from django.contrib import admin

from .models import (
    EndpointState,
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
    list_display = ("handle", "display_name", "rest_id", "tracking", "created_at")
    list_filter = ("tracking",)
    search_fields = ("handle", "rest_id", "display_name")


@admin.register(Tweet)
class TweetAdmin(admin.ModelAdmin):
    list_display = ("tweet_id", "account", "type", "created_at", "likes", "retweets")
    list_filter = ("type", "source_endpoint")
    search_fields = ("tweet_id", "account", "text")
    date_hierarchy = "created_at"


@admin.register(Search)
class SearchAdmin(admin.ModelAdmin):
    list_display = ("slug", "product", "enabled", "owner", "last_run_at")
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
