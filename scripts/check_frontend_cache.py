"""Check nginx's cache contract: python3 scripts/check_frontend_cache.py BASE_URL."""
import re
import sys
from urllib.error import HTTPError
from urllib.request import urlopen


def response(base, path):
    try:
        result = urlopen(base + path, timeout=15)
    except HTTPError as error:
        result = error
    with result:
        return result.status, result.headers, result.read().decode()


def check(base):
    for path in ("/", "/login", "/signup", "/feed"):
        status, headers, body = response(base, path)
        assert status == 200, (path, status)
        assert headers.get_all("Cache-Control") == ["no-cache"], (path, headers)
    asset = re.search(r'src="(/assets/[^\"]+\.js)"', body)
    assert asset, "SPA shell must reference a built JavaScript asset"
    status, headers, _ = response(base, asset[1])
    assert status == 200, status
    assert headers.get_all("Cache-Control") == [
        "public, max-age=31536000, immutable"
    ], headers
    for path in ("/assets/cache-check-missing.js", "/media/cache-check-missing.jpg",
                 "/static/cache-check-missing.css", "/media/exports/cache-check.csv"):
        status, headers, _ = response(base, path)
        assert status == 404, (path, status)
        cache = headers.get("Cache-Control", "")
        assert "max-age=" not in cache and "immutable" not in cache, (path, cache)
    print("PASS: shell revalidation, one asset cache header, and four missing-file paths")


if __name__ == "__main__":
    check(sys.argv[1].rstrip("/"))
