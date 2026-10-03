"""/favicon.ico — every page's browser requests it unprompted, so it must be
served, unauthenticated, as a real multi-size icon (not a 404 per page load)."""
import struct


def test_favicon_served_without_auth(app):
    from fastapi.testclient import TestClient
    # a fresh client: no session cookie from the shared fixture
    with TestClient(app, raise_server_exceptions=False) as anon:
        r = anon.get("/favicon.ico")
    assert r.status_code == 200
    assert r.headers["content-type"] == "image/x-icon"
    # ICONDIR header: reserved 0, type 1 (icon), then the image count
    reserved, kind, count = struct.unpack("<HHH", r.content[:6])
    assert (reserved, kind) == (0, 1)
    # 16 and 32 are what tabs and bookmarks actually use; both must be there
    sizes = set()
    for i in range(count):
        w, h = r.content[6 + 16 * i], r.content[7 + 16 * i]
        sizes.add((w or 256, h or 256))
    assert {(16, 16), (32, 32)} <= sizes
