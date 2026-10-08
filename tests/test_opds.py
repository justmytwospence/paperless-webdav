"""Tests for the OPDS catalog."""

import base64
import xml.etree.ElementTree as ET
from collections.abc import Iterator
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from paperless_webdav.opds import OpdsApp, OpdsDispatcher
from paperless_webdav.paperless_client import PaperlessDocument
from paperless_webdav.webdav_provider import PaperlessProvider, ShareResource

ATOM = "{http://www.w3.org/2005/Atom}"
TAGS = {"academic": 1, "philosophy": 2, "bayesian": 3, "paperwork": 9}


def _doc(doc_id: int, title: str, tags: list[int], added: str) -> PaperlessDocument:
    return PaperlessDocument(
        id=doc_id,
        title=title,
        original_file_name=f"{title}.pdf",
        created="1999-01-01T00:00:00Z",
        modified="2026-10-01T00:00:00Z",
        tags=tags,
        added=added,
    )


DOCS = [
    _doc(1, "Old", [1, 2], "2026-01-02T09:00:00-06:00"),
    _doc(2, "New: Café", [1, 2, 3], "2026-10-08T23:30:00-06:00"),
    _doc(3, "Loose", [1], "2026-05-05T12:00:00-06:00"),
]


class _Response:
    def __init__(self) -> None:
        self.status = ""
        self.headers: dict[str, str] = {}

    def __call__(self, status: str, headers: list[tuple[str, str]], exc_info: Any = None) -> None:
        self.status = status
        self.headers = dict(headers)


def _share() -> Any:
    share = MagicMock()
    share.name = "academic"
    share.include_tags = ["academic"]
    share.exclude_tags = []
    share.done_folder_enabled = False
    share.done_folder_name = "done"
    share.done_tag = None
    return share


@pytest.fixture
def app() -> Iterator[OpdsApp]:
    provider = PaperlessProvider(
        shares={"academic": _share()}, paperless_url="http://paperless.local", tag_folders=True
    )
    authenticator = MagicMock()

    def basic_auth_user(realm: str, user: str, password: str, environ: dict[str, Any]) -> Any:
        if (user, password) != ("me", "secret"):
            return False
        environ["paperless.token"] = "token"
        return user

    authenticator.basic_auth_user.side_effect = basic_auth_user
    client = MagicMock()
    with (
        patch.object(ShareResource, "_load_documents", return_value=DOCS),
        patch.object(ShareResource, "_get_tag_map", return_value=TAGS),
        patch.object(provider, "_create_client", return_value=client),
    ):
        opds = OpdsApp(provider, authenticator, page_size=2)
        opds.client = client  # type: ignore[attr-defined]
        yield opds


def _get(
    app: OpdsApp, path: str, query: str = "", method: str = "GET", auth: bool = True
) -> tuple[_Response, bytes]:
    environ: dict[str, Any] = {
        "REQUEST_METHOD": method,
        "PATH_INFO": path,
        "QUERY_STRING": query,
    }
    if auth:
        environ["HTTP_AUTHORIZATION"] = "Basic " + base64.b64encode(b"me:secret").decode()
    response = _Response()
    body = b"".join(app(environ, response))
    return response, body


def _entries(body: bytes) -> list[ET.Element]:
    return ET.fromstring(body).findall(f"{ATOM}entry")


def _titles(body: bytes) -> list[str]:
    return [e.findtext(f"{ATOM}title") or "" for e in _entries(body)]


def _link(element: ET.Element, rel: str) -> str | None:
    for link in element.findall(f"{ATOM}link"):
        if link.get("rel") == rel:
            return link.get("href")
    return None


class TestAuth:
    def test_requires_basic_auth(self, app: OpdsApp) -> None:
        response, _ = _get(app, "/opds/", auth=False)
        assert response.status.startswith("401")
        assert response.headers["WWW-Authenticate"].startswith("Basic")

    def test_rejects_wrong_password(self, app: OpdsApp) -> None:
        environ = {
            "REQUEST_METHOD": "GET",
            "PATH_INFO": "/opds/",
            "HTTP_AUTHORIZATION": "Basic " + base64.b64encode(b"me:nope").decode(),
        }
        response = _Response()
        list(app(environ, response))
        assert response.status.startswith("401")


class TestNavigation:
    def test_root_lists_shares(self, app: OpdsApp) -> None:
        response, body = _get(app, "/opds/")
        assert response.status == "200 OK"
        assert "kind=navigation" in response.headers["Content-Type"]
        assert _titles(body) == ["academic"]

    def test_share_lists_recent_then_tags_then_unsorted(self, app: OpdsApp) -> None:
        _, body = _get(app, "/opds/academic/")
        assert _titles(body) == ["Recently added", "bayesian", "philosophy", "Unsorted"]
        recent = _entries(body)[0]
        assert _link(recent, "http://opds-spec.org/sort/new") == "/opds/academic/recent"
        # the share's own tag is not offered as a feed
        assert "academic" not in _titles(body)
        assert _link(ET.fromstring(body), "search") == "/opds/academic/search?q={searchTerms}"

    def test_unknown_share_is_404(self, app: OpdsApp) -> None:
        response, _ = _get(app, "/opds/paperwork/recent")
        assert response.status.startswith("404")


class TestAcquisition:
    def test_recent_is_newest_first_and_paged(self, app: OpdsApp) -> None:
        response, body = _get(app, "/opds/academic/recent")
        assert "kind=acquisition" in response.headers["Content-Type"]
        assert _titles(body) == ["New: Café", "Loose"]
        assert _link(ET.fromstring(body), "next") == "/opds/academic/recent?page=2"

        _, body = _get(app, "/opds/academic/recent", "page=2")
        assert _titles(body) == ["Old"]
        assert _link(ET.fromstring(body), "next") is None
        assert _link(ET.fromstring(body), "previous") == "/opds/academic/recent"

    def test_one_download_link_per_document_in_every_feed(self, app: OpdsApp) -> None:
        def href(path: str) -> str | None:
            _, body = _get(app, path)
            entry = next(e for e in _entries(body) if e.findtext(f"{ATOM}title") == "New: Café")
            return _link(entry, "http://opds-spec.org/acquisition")

        expected = "/opds/academic/download/2/New%20Caf%C3%A9.pdf"
        assert href("/opds/academic/recent") == expected
        assert href("/opds/academic/tag/3") == expected
        assert href("/opds/academic/tag/2") == expected

    def test_tag_feed_filters(self, app: OpdsApp) -> None:
        _, body = _get(app, "/opds/academic/tag/3")
        assert _titles(body) == ["New: Café"]

    def test_unsorted_holds_documents_without_topic_tags(self, app: OpdsApp) -> None:
        _, body = _get(app, "/opds/academic/unsorted")
        assert _titles(body) == ["Loose"]

    def test_share_tag_is_not_a_feed(self, app: OpdsApp) -> None:
        response, _ = _get(app, "/opds/academic/tag/1")
        assert response.status.startswith("404")

    def test_entry_carries_date_added_and_tags(self, app: OpdsApp) -> None:
        _, body = _get(app, "/opds/academic/tag/3")
        entry = _entries(body)[0]
        assert entry.findtext(f"{ATOM}summary") == "Added 2026-10-08 \u00b7 bayesian, philosophy"
        assert entry.findtext(f"{ATOM}published") == "2026-10-08T23:30:00-06:00"


class TestSearch:
    def test_search_is_scoped_to_the_share(self, app: OpdsApp) -> None:
        stranger = _doc(99, "Not in share", [9], "2026-10-09T00:00:00Z")
        app.client.search_documents = AsyncMock(  # type: ignore[attr-defined]
            return_value=([DOCS[0], stranger], 2)
        )
        _, body = _get(app, "/opds/academic/search", "q=priors")
        assert _titles(body) == ["Old"]
        call = app.client.search_documents.call_args  # type: ignore[attr-defined]
        assert call.args[0] == "priors"
        assert call.kwargs["include_tag_ids"] == [1]

    def test_empty_search_does_not_call_paperless(self, app: OpdsApp) -> None:
        app.client.search_documents = AsyncMock()  # type: ignore[attr-defined]
        response, body = _get(app, "/opds/academic/search", "q=")
        assert response.status == "200 OK"
        assert _titles(body) == []
        app.client.search_documents.assert_not_called()  # type: ignore[attr-defined]


class TestDownload:
    def test_head_names_the_file_without_touching_paperless(self, app: OpdsApp) -> None:
        response, body = _get(app, "/opds/academic/download/2/x.pdf", method="HEAD")
        assert response.status == "200 OK"
        assert body == b""
        assert 'filename="New Caf%C3%A9.pdf"' in response.headers["Content-Disposition"]
        app.client.open_stream.assert_not_called()  # type: ignore[attr-defined]

    def test_get_streams_with_upstream_length(self, app: OpdsApp) -> None:
        stream = MagicMock()
        stream.headers = {"content-length": "3", "content-type": "application/pdf"}
        stream.read.side_effect = [b"PDF", b""]
        app.client.open_stream.return_value = stream  # type: ignore[attr-defined]

        response, body = _get(app, "/opds/academic/download/2/x.pdf")
        assert body == b"PDF"
        assert response.headers["Content-Length"] == "3"
        app.client.open_stream.assert_called_with(  # type: ignore[attr-defined]
            "/api/documents/2/download/"
        )

    def test_document_outside_the_share_is_404(self, app: OpdsApp) -> None:
        response, _ = _get(app, "/opds/academic/download/99/x.pdf")
        assert response.status.startswith("404")
        response, _ = _get(app, "/opds/academic/thumb/99")
        assert response.status.startswith("404")

    def test_paperless_failure_is_502(self, app: OpdsApp) -> None:
        app.client.open_stream.side_effect = RuntimeError("down")  # type: ignore[attr-defined]
        # Patched because an earlier test elsewhere in the suite may have bound
        # structlog to a stdout pytest has since closed.
        with patch("paperless_webdav.opds.logger") as logger:
            response, _ = _get(app, "/opds/academic/download/2/x.pdf")
        assert response.status.startswith("502")
        logger.error.assert_called_once()


class TestDispatcher:
    def test_routes_by_prefix(self) -> None:
        opds = MagicMock(return_value=[b"opds"])
        dav = MagicMock(return_value=[b"dav"])
        dispatcher = OpdsDispatcher(opds, dav)
        assert dispatcher({"PATH_INFO": "/opds/academic/"}, MagicMock()) == [b"opds"]
        assert dispatcher({"PATH_INFO": "/opds"}, MagicMock()) == [b"opds"]
        assert dispatcher({"PATH_INFO": "/opdsx/a.pdf"}, MagicMock()) == [b"dav"]
        assert dispatcher({"PATH_INFO": "/academic/"}, MagicMock()) == [b"dav"]
