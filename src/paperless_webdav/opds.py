# src/paperless_webdav/opds.py
"""OPDS 1.2 catalog of each share, served beside the WebDAV tree.

WebDAV presents a library as a filesystem, and that is the wrong model for an
e-reader: a document carrying three tags has to be three files (which the
reader then treats as three books, each with its own annotations), there is no
"newest first" a client is obliged to honour, and there is no search. OPDS is
the catalog format e-readers already speak (KOReader has a browser for it
built in), and it fits the data instead of fighting it:

    /opds/                               navigation: one entry per share
    /opds/{share}/                       navigation: Recently added, one entry
                                         per tag, Unsorted, plus a search link
    /opds/{share}/recent                 acquisition feed, newest first
    /opds/{share}/tag/{tag_id}           acquisition feed, newest first
    /opds/{share}/unsorted               acquisition feed, newest first
    /opds/{share}/search?q=...           Paperless full-text search, by relevance
    /opds/{share}/download/{id}/{n}.pdf  the document itself
    /opds/{share}/thumb/{id}             Paperless's thumbnail

A document is one entry with one download URL wherever it is listed, so the
reader downloads it once. Acquisition feeds are newest first with stable
download URLs, which is what KOReader's catalog "Sync" relies on to fetch only
what is new since its last run.

Membership is exactly the WebDAV tree's: every feed is a filter over
ShareResource's document list, so the share's include/exclude tags, the
tag-to-folder rules and the document-list cache all apply unchanged, and a
download or thumbnail is refused unless the document belongs to the share in
the URL. That last check is what lets a reverse proxy gate a share by path
prefix (e.g. a LAN-only /opds/paperwork).

Authentication is the same HTTP Basic check the WebDAV server uses.
"""

from __future__ import annotations

import base64
import binascii
import math
import xml.etree.ElementTree as ET
from collections.abc import Callable, Iterable, Iterator
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any
from urllib.parse import parse_qs, quote

from paperless_webdav.async_bridge import run_async
from paperless_webdav.logging import get_logger
from paperless_webdav.paperless_client import PaperlessDocument
from paperless_webdav.webdav_provider import (
    PaperlessProvider,
    ShareResource,
    sanitize_filename,
)

if TYPE_CHECKING:
    from paperless_webdav.models import Share
    from paperless_webdav.webdav_auth import PaperlessBasicAuthenticator

logger = get_logger(__name__)

OPDS_PREFIX = "/opds"
REALM = "Paperless OPDS"
PAGE_SIZE = 50
CHUNK_SIZE = 64 * 1024

ATOM_NS = "http://www.w3.org/2005/Atom"
DC_NS = "http://purl.org/dc/terms/"
OPDS_NS = "http://opds-spec.org/2010/catalog"

NAV_TYPE = "application/atom+xml;profile=opds-catalog;kind=navigation"
ACQ_TYPE = "application/atom+xml;profile=opds-catalog;kind=acquisition"
REL_ACQUISITION = "http://opds-spec.org/acquisition"
REL_THUMBNAIL = "http://opds-spec.org/image/thumbnail"
REL_NEW = "http://opds-spec.org/sort/new"

ET.register_namespace("", ATOM_NS)
ET.register_namespace("dc", DC_NS)
ET.register_namespace("opds", OPDS_NS)

StartResponse = Callable[..., Any]


class _NotFound(Exception):
    """Raised by a handler to answer 404."""


def _atom(tag: str) -> str:
    return f"{{{ATOM_NS}}}{tag}"


def _sub(parent: ET.Element, tag: str, text: str | None = None, **attrs: str) -> ET.Element:
    element = ET.SubElement(parent, tag, attrs)
    if text is not None:
        element.text = text
    return element


def _now() -> str:
    return datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def _newest_first(documents: Iterable[PaperlessDocument]) -> list[PaperlessDocument]:
    """Sort by date added, newest first; id breaks ties and covers old caches."""
    return sorted(documents, key=lambda d: (d.added or d.created, d.id), reverse=True)


def _download_name(doc: PaperlessDocument) -> str:
    return f"{sanitize_filename(doc.title) or f'document-{doc.id}'}.pdf"


def _content_disposition(filename: str) -> str:
    """Content-Disposition that survives a non-ASCII title.

    WSGI headers must be latin-1, so the plain filename= carries the
    percent-encoded UTF-8 (KOReader URL-decodes it) and filename* carries the
    RFC 5987 form standard clients prefer.
    """
    encoded = quote(filename, safe=" ")
    return f"attachment; filename=\"{encoded}\"; filename*=UTF-8''{quote(filename)}"


class OpdsApp:
    """WSGI application serving the OPDS catalog for every share."""

    def __init__(
        self,
        provider: PaperlessProvider,
        authenticator: PaperlessBasicAuthenticator,
        page_size: int = PAGE_SIZE,
    ) -> None:
        self._provider = provider
        self._authenticator = authenticator
        self._page_size = page_size

    # -- WSGI entry --------------------------------------------------------

    def __call__(self, environ: dict[str, Any], start_response: StartResponse) -> Iterable[bytes]:
        method = environ.get("REQUEST_METHOD", "GET")
        if method not in ("GET", "HEAD"):
            return self._plain(start_response, "405 Method Not Allowed", "GET or HEAD only")

        if not self._authenticate(environ):
            start_response(
                "401 Unauthorized",
                [
                    ("WWW-Authenticate", f'Basic realm="{REALM}"'),
                    ("Content-Type", "text/plain; charset=utf-8"),
                ],
            )
            return [b"Authentication required"]

        # ShareResource is a wsgidav resource and reads its provider from the
        # environ, which wsgidav would normally have set; this request never
        # passes through wsgidav.
        environ["wsgidav.provider"] = self._provider
        path = environ.get("PATH_INFO", "")
        parts = [p for p in path[len(OPDS_PREFIX) :].split("/") if p]
        query = parse_qs(environ.get("QUERY_STRING", ""))
        try:
            return self._route(environ, start_response, parts, query, head=method == "HEAD")
        except _NotFound:
            return self._plain(start_response, "404 Not Found", "Not found")
        except Exception as exc:
            logger.error(
                "opds_request_failed",
                path=path,
                error=str(exc),
                error_type=type(exc).__name__,
            )
            return self._plain(start_response, "502 Bad Gateway", "Paperless request failed")

    def _authenticate(self, environ: dict[str, Any]) -> bool:
        header = environ.get("HTTP_AUTHORIZATION", "")
        scheme, _, encoded = header.partition(" ")
        if scheme.lower() != "basic" or not encoded:
            return False
        try:
            username, _, password = base64.b64decode(encoded.strip()).decode().partition(":")
        except (binascii.Error, UnicodeDecodeError):
            return False
        if not username:
            return False
        return bool(self._authenticator.basic_auth_user(REALM, username, password, environ))

    def _route(
        self,
        environ: dict[str, Any],
        start_response: StartResponse,
        parts: list[str],
        query: dict[str, list[str]],
        head: bool,
    ) -> Iterable[bytes]:
        if not parts:
            return self._xml(start_response, self._root_feed(), NAV_TYPE, head)

        share = self._provider._get_shares().get(parts[0])
        if share is None:
            raise _NotFound
        resource = ShareResource(f"/{share.name}", environ, self._provider, share)
        page = self._page(query)
        rest = parts[1:]

        if not rest:
            return self._xml(start_response, self._share_feed(resource), NAV_TYPE, head)
        if rest == ["recent"]:
            docs = _newest_first(resource._get_documents(prefetch_sizes=False))
            feed = self._acquisition_feed(
                resource, "recent", "Recently added", docs, page, path="recent"
            )
            return self._xml(start_response, feed, ACQ_TYPE, head)
        if rest == ["unsorted"]:
            docs = _newest_first(resource._untagged_documents())
            feed = self._acquisition_feed(
                resource, "unsorted", "Unsorted", docs, page, path="unsorted"
            )
            return self._xml(start_response, feed, ACQ_TYPE, head)
        if len(rest) == 2 and rest[0] == "tag" and rest[1].isdigit():
            tag_id = int(rest[1])
            names = {tid: name for name, tid in resource._topic_tag_ids().items()}
            if tag_id not in names:
                raise _NotFound
            docs = _newest_first(
                d for d in resource._get_documents(prefetch_sizes=False) if tag_id in d.tags
            )
            feed = self._acquisition_feed(
                resource, f"tag:{tag_id}", names[tag_id], docs, page, path=f"tag/{tag_id}"
            )
            return self._xml(start_response, feed, ACQ_TYPE, head)
        if rest == ["search"]:
            terms = (query.get("q") or [""])[0].strip()
            feed = self._search_feed(resource, terms, page)
            return self._xml(start_response, feed, ACQ_TYPE, head)
        if len(rest) == 3 and rest[0] == "download" and rest[1].isdigit():
            doc = self._member(resource, int(rest[1]))
            return self._download(environ, start_response, doc, head)
        if len(rest) == 2 and rest[0] == "thumb" and rest[1].isdigit():
            doc = self._member(resource, int(rest[1]))
            return self._proxy(
                environ,
                start_response,
                f"/api/documents/{doc.id}/thumb/",
                head,
                extra_headers=[("Cache-Control", "private, max-age=86400")],
            )
        raise _NotFound

    # -- feeds -------------------------------------------------------------

    def _new_feed(self, feed_id: str, title: str, self_href: str, kind: str) -> ET.Element:
        feed = ET.Element(_atom("feed"))
        _sub(feed, _atom("id"), feed_id)
        _sub(feed, _atom("title"), title)
        _sub(feed, _atom("updated"), _now())
        _sub(feed, _atom("link"), rel="self", href=self_href, type=kind)
        _sub(feed, _atom("link"), rel="start", href=f"{OPDS_PREFIX}/", type=NAV_TYPE)
        return feed

    def _nav_entry(
        self,
        feed: ET.Element,
        entry_id: str,
        title: str,
        href: str,
        kind: str,
        rel: str,
        summary: str | None = None,
    ) -> None:
        entry = _sub(feed, _atom("entry"))
        _sub(entry, _atom("title"), title)
        _sub(entry, _atom("id"), entry_id)
        _sub(entry, _atom("updated"), _now())
        if summary:
            _sub(entry, _atom("content"), summary, type="text")
        _sub(entry, _atom("link"), rel=rel, href=href, type=kind)

    def _root_feed(self) -> ET.Element:
        feed = self._new_feed("urn:paperless-webdav:opds", "Paperless", f"{OPDS_PREFIX}/", NAV_TYPE)
        for name in sorted(self._provider._get_shares()):
            self._nav_entry(
                feed,
                f"urn:paperless-webdav:opds:{name}",
                name,
                f"{OPDS_PREFIX}/{quote(name)}/",
                NAV_TYPE,
                "subsection",
            )
        return feed

    def _share_feed(self, resource: ShareResource) -> ET.Element:
        share: Share = resource._share
        base = f"{OPDS_PREFIX}/{quote(share.name)}"
        feed = self._new_feed(
            f"urn:paperless-webdav:opds:{share.name}", share.name, f"{base}/", NAV_TYPE
        )
        _sub(feed, _atom("link"), rel="up", href=f"{OPDS_PREFIX}/", type=NAV_TYPE)
        self._search_link(feed, base)

        documents = resource._get_documents(prefetch_sizes=False)
        self._nav_entry(
            feed,
            f"urn:paperless-webdav:opds:{share.name}:recent",
            "Recently added",
            f"{base}/recent",
            ACQ_TYPE,
            REL_NEW,
            summary=f"All {len(documents)} documents, newest first",
        )
        for name, tag_id in sorted(resource._topic_tag_ids().items()):
            count = sum(1 for d in documents if tag_id in d.tags)
            self._nav_entry(
                feed,
                f"urn:paperless-webdav:opds:{share.name}:tag:{tag_id}",
                name,
                f"{base}/tag/{tag_id}",
                ACQ_TYPE,
                "subsection",
                summary=f"{count} documents",
            )
        untagged = resource._untagged_documents()
        if untagged:
            self._nav_entry(
                feed,
                f"urn:paperless-webdav:opds:{share.name}:unsorted",
                "Unsorted",
                f"{base}/unsorted",
                ACQ_TYPE,
                "subsection",
                summary=f"{len(untagged)} documents with no topic tag",
            )
        return feed

    def _search_link(self, feed: ET.Element, base: str) -> None:
        # A direct template link (type atom+xml) rather than an OpenSearch
        # description document: KOReader accepts either, and this needs no
        # extra round trip or extra endpoint.
        _sub(
            feed,
            _atom("link"),
            rel="search",
            href=f"{base}/search?q={{searchTerms}}",
            type="application/atom+xml",
            title="Search full text",
        )

    def _acquisition_feed(
        self,
        resource: ShareResource,
        feed_key: str,
        title: str,
        documents: list[PaperlessDocument],
        page: int,
        path: str,
        total: int | None = None,
        extra_query: str = "",
    ) -> ET.Element:
        """One page of an acquisition feed.

        With `total` None, `documents` is the whole list and is sliced here;
        otherwise it is already the requested page (search) and `total` is the
        hit count the paging links are computed from.
        """
        share: Share = resource._share
        base = f"{OPDS_PREFIX}/{quote(share.name)}"
        if total is None:
            total = len(documents)
            start = (page - 1) * self._page_size
            documents = documents[start : start + self._page_size]
        pages = max(1, math.ceil(total / self._page_size))

        def href(p: int) -> str:
            params = [q for q in (extra_query, f"page={p}" if p > 1 else "") if q]
            return f"{base}/{path}" + (f"?{'&'.join(params)}" if params else "")

        feed = self._new_feed(
            f"urn:paperless-webdav:opds:{share.name}:{feed_key}", title, href(page), ACQ_TYPE
        )
        _sub(feed, _atom("link"), rel="up", href=f"{base}/", type=NAV_TYPE)
        self._search_link(feed, base)
        if page > 1:
            _sub(feed, _atom("link"), rel="first", href=href(1), type=ACQ_TYPE)
            _sub(feed, _atom("link"), rel="previous", href=href(page - 1), type=ACQ_TYPE)
        if page < pages:
            _sub(feed, _atom("link"), rel="next", href=href(page + 1), type=ACQ_TYPE)
            _sub(feed, _atom("link"), rel="last", href=href(pages), type=ACQ_TYPE)

        id_to_tag = {tid: name for name, tid in resource._topic_tag_ids().items()}
        for doc in documents:
            self._document_entry(feed, base, doc, id_to_tag)
        return feed

    def _document_entry(
        self, feed: ET.Element, base: str, doc: PaperlessDocument, id_to_tag: dict[int, str]
    ) -> None:
        entry = _sub(feed, _atom("entry"))
        _sub(entry, _atom("title"), doc.title)
        _sub(entry, _atom("id"), f"urn:paperless:document:{doc.id}")
        _sub(entry, _atom("updated"), doc.modified)
        if doc.added:
            _sub(entry, _atom("published"), doc.added)
        _sub(entry, f"{{{DC_NS}}}issued", doc.created[:10])
        tags = sorted(id_to_tag[t] for t in doc.tags if t in id_to_tag)
        for name in tags:
            _sub(entry, _atom("category"), term=name, label=name)
        added = (doc.added or doc.created)[:10]
        summary = f"Added {added}" + (f" \u00b7 {', '.join(tags)}" if tags else "")
        _sub(entry, _atom("summary"), summary, type="text")
        _sub(
            entry,
            _atom("link"),
            rel=REL_ACQUISITION,
            href=f"{base}/download/{doc.id}/{quote(_download_name(doc))}",
            type="application/pdf",
        )
        _sub(
            entry,
            _atom("link"),
            rel=REL_THUMBNAIL,
            href=f"{base}/thumb/{doc.id}",
            type="image/webp",
        )

    def _search_feed(self, resource: ShareResource, terms: str, page: int) -> ET.Element:
        share: Share = resource._share
        title = f"Search: {terms}" if terms else "Search"
        extra = f"q={quote(terms)}"
        if not terms:
            return self._acquisition_feed(
                resource, "search", title, [], page, path="search", total=0, extra_query=extra
            )

        client = self._provider._create_client(resource.environ)
        if client is None:
            raise RuntimeError("no Paperless token on an authenticated request")
        tag_map = resource._get_tag_map(client)
        include = resource._resolve_tag_ids_from_map(tag_map, list(share.include_tags))
        documents, total = run_async(
            client.search_documents(
                terms, include_tag_ids=include, page=page, page_size=self._page_size
            )
        )
        # The share's exclude/done rules are applied by intersecting with its
        # own membership rather than re-expressed as API filters, so search can
        # never surface a document the share does not list.
        members = {d.id for d in resource._get_documents(prefetch_sizes=False)}
        documents = [d for d in documents if d.id in members]
        return self._acquisition_feed(
            resource,
            "search",
            title,
            documents,
            page,
            path="search",
            total=total,
            extra_query=extra,
        )

    # -- binary responses --------------------------------------------------

    def _member(self, resource: ShareResource, doc_id: int) -> PaperlessDocument:
        for doc in resource._get_documents(prefetch_sizes=False):
            if doc.id == doc_id:
                return doc
        raise _NotFound

    def _download(
        self,
        environ: dict[str, Any],
        start_response: StartResponse,
        doc: PaperlessDocument,
        head: bool,
    ) -> Iterable[bytes]:
        headers = [("Content-Disposition", _content_disposition(_download_name(doc)))]
        if head:
            # KOReader HEADs the link to learn the server's filename; answer
            # without opening a stream to Paperless.
            start_response("200 OK", [("Content-Type", "application/pdf"), *headers])
            return []
        return self._proxy(
            environ,
            start_response,
            f"/api/documents/{doc.id}/download/",
            head=False,
            extra_headers=headers,
            content_type="application/pdf",
        )

    def _proxy(
        self,
        environ: dict[str, Any],
        start_response: StartResponse,
        endpoint: str,
        head: bool,
        extra_headers: list[tuple[str, str]] | None = None,
        content_type: str | None = None,
    ) -> Iterable[bytes]:
        client = self._provider._create_client(environ)
        if client is None:
            raise RuntimeError("no Paperless token on an authenticated request")
        stream = client.open_stream(endpoint)
        headers = [
            (
                "Content-Type",
                content_type or stream.headers.get("content-type", "application/octet-stream"),
            ),
            *(extra_headers or []),
        ]
        length = stream.headers.get("content-length")
        if length:
            headers.append(("Content-Length", length))
        start_response("200 OK", headers)
        if head:
            stream.close()
            return []
        return _StreamBody(stream)

    # -- helpers -----------------------------------------------------------

    def _page(self, query: dict[str, list[str]]) -> int:
        raw = (query.get("page") or ["1"])[0]
        return max(1, int(raw)) if raw.isdigit() else 1

    def _xml(
        self, start_response: StartResponse, feed: ET.Element, kind: str, head: bool
    ) -> Iterable[bytes]:
        body = ET.tostring(feed, encoding="utf-8", xml_declaration=True)
        start_response(
            "200 OK",
            [
                ("Content-Type", f"{kind};charset=utf-8"),
                ("Content-Length", str(len(body))),
                ("Cache-Control", "no-cache"),
            ],
        )
        return [] if head else [body]

    @staticmethod
    def _plain(start_response: StartResponse, status: str, text: str) -> Iterable[bytes]:
        body = text.encode()
        start_response(
            status,
            [("Content-Type", "text/plain; charset=utf-8"), ("Content-Length", str(len(body)))],
        )
        return [body]


class _StreamBody:
    """WSGI body over an upstream stream; the server's close() releases it."""

    def __init__(self, stream: Any) -> None:
        self._stream = stream

    def __iter__(self) -> Iterator[bytes]:
        while True:
            chunk = self._stream.read(CHUNK_SIZE)
            if not chunk:
                return
            yield chunk

    def close(self) -> None:
        self._stream.close()


class OpdsDispatcher:
    """Send /opds/... to the catalog and everything else to WebDAV.

    Both answer on the same port, so the catalog reuses the WebDAV server's
    provider, caches and authentication without a second listener -- and a
    share named "opds" is the one name this reserves.
    """

    def __init__(
        self, opds_app: OpdsApp, dav_app: Callable[[dict[str, Any], StartResponse], Any]
    ) -> None:
        self._opds = opds_app
        self._dav = dav_app

    def __call__(self, environ: dict[str, Any], start_response: StartResponse) -> Any:
        path = environ.get("PATH_INFO", "")
        if path == OPDS_PREFIX or path.startswith(f"{OPDS_PREFIX}/"):
            return self._opds(environ, start_response)
        return self._dav(environ, start_response)
