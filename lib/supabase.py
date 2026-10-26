"""Read-only Supabase (PostgREST) access.

This module can only issue GET requests. There is no insert, update, delete or
rpc method, and no method that accepts an HTTP verb from a caller - production
records are not this toolkit's to change.

Pagination uses the `Range` header with a preflight `Content-Range` count so a
caller always knows whether it has the whole set. Truncation is reported, never
hidden.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Iterable, Sequence
from urllib.parse import quote, urlencode

from . import status as status_mod
from .config import Config
from .http_client import HttpClient, HttpError, TransportError

DEFAULT_PAGE_SIZE = 1000
MAX_PAGE_SIZE = 1000


@dataclass
class Page:
    rows: list[dict[str, Any]]
    total: int | None
    offset: int
    limit: int
    complete: bool
    pages_fetched: int
    notes: list[str] = field(default_factory=list)


class SupabaseError(Exception):
    def __init__(self, message: str, *, category: str, detail: str | None = None) -> None:
        super().__init__(message)
        self.message = message
        self.category = category
        self.detail = detail


class SupabaseClient:
    def __init__(self, config: Config, http: HttpClient | None = None) -> None:
        self.config = config
        self.base_url = config.supabase_url()
        self.key_name = config.supabase_key_name()
        self._key = config.supabase_key()
        self.http = http or HttpClient(timeout_seconds=30.0, max_attempts=3)
        self.page_size = DEFAULT_PAGE_SIZE
        self.tables: list[str] = []

    # -- headers ----------------------------------------------------------

    def _headers(self, *, count: bool = False, extra: dict[str, str] | None = None) -> dict[str, str]:
        headers = {
            "apikey": self._key,
            "Authorization": f"Bearer {self._key}",
            "Accept": "application/json",
        }
        if count:
            headers["Prefer"] = "count=exact"
        if extra:
            headers.update(extra)
        return headers

    # -- requests ---------------------------------------------------------

    def _get(self, path: str, *, params: dict[str, str] | None = None, headers: dict[str, str] | None = None):
        query = f"?{urlencode(params)}" if params else ""
        url = f"{self.base_url}/rest/v1/{path}{query}"
        try:
            return self.http.get(url, headers=headers or self._headers())
        except HttpError as exc:
            raise SupabaseError(
                f"Supabase rejected the request for {path}: {exc.message}",
                category=_category_for_status(exc.status),
                detail=exc.detail,
            ) from None
        except TransportError as exc:
            raise SupabaseError(
                f"could not reach Supabase for {path}: {exc.message}",
                category=status_mod.PROVIDER_ERROR,
            ) from None

    # -- read-only API ----------------------------------------------------

    def describe_schema(self) -> dict[str, Any]:
        """The PostgREST OpenAPI document. Lets the toolkit see the real shape
        of every table before it queries one."""
        try:
            response = self.http.get(
                f"{self.base_url}/rest/v1/",
                headers=self._headers(),
            )
        except HttpError as exc:
            raise SupabaseError(
                f"Supabase schema request failed: {exc.message}",
                category=_category_for_status(exc.status),
                detail=exc.detail,
            ) from None
        except TransportError as exc:
            raise SupabaseError(
                f"could not reach Supabase: {exc.message}",
                category=status_mod.PROVIDER_ERROR,
            ) from None
        document = response.json() or {}
        definitions = document.get("definitions", {})
        self.tables = sorted(definitions)
        return {
            "tables": {
                name: sorted((definitions[name].get("properties") or {}).keys())
                for name in self.tables
            }
        }

    def table_columns(self, table: str) -> list[str]:
        schema = self.describe_schema()["tables"]
        if table not in schema:
            raise SupabaseError(
                f"table {table!r} is not exposed by PostgREST",
                category=status_mod.PROVIDER_ERROR,
            )
        return list(schema[table])

    def select(
        self,
        table: str,
        *,
        columns: Sequence[str],
        filters: dict[str, str] | None = None,
        order: str | None = None,
        limit: int | None = None,
        offset: int = 0,
        count: bool = False,
    ) -> Page:
        """One bounded page. `complete` is False when a limit truncated the set."""
        if not columns:
            raise SupabaseError(
                f"refusing to select from {table!r} without an explicit column list",
                category=status_mod.USAGE_ERROR,
            )
        params: dict[str, str] = {"select": ",".join(columns)}
        if filters:
            params.update(filters)
        if order:
            params["order"] = order
        if limit is not None:
            params["limit"] = str(min(limit, MAX_PAGE_SIZE))
            params["offset"] = str(offset)

        headers = self._headers(count=count)
        if limit is None:
            headers["Range-Unit"] = "items"
            headers["Range"] = f"{offset}-{(offset + self.page_size) - 1}"
            headers["Prefer"] = ("count=exact" if count else "count=estimated")

        response = self._get(table, params=params, headers=headers)
        rows = response.json() or []
        total = _parse_count(response.header("content-range"))
        if limit is None:
            expected = self.page_size
            returned = len(rows)
            complete = returned < expected
        else:
            returned = len(rows)
            complete = returned < min(limit, MAX_PAGE_SIZE)

        return Page(
            rows=list(rows),
            total=total,
            offset=offset,
            limit=min(limit, MAX_PAGE_SIZE) if limit is not None else self.page_size,
            complete=complete,
            pages_fetched=1,
        )

    def select_all(
        self,
        table: str,
        *,
        columns: Sequence[str],
        filters: dict[str, str] | None = None,
        order: str | None = None,
        max_rows: int | None = None,
        page_size: int | None = None,
    ) -> Page:
        """Page through a table until exhausted, with an explicit ceiling.

        If `max_rows` stops the walk, the returned Page has `complete=False` and
        a note saying exactly how many rows were read and why it stopped.
        """
        size = min(page_size or self.page_size, MAX_PAGE_SIZE)
        collected: list[dict[str, Any]] = []
        total: int | None = None
        offset = 0
        pages = 0
        notes: list[str] = []

        while True:
            page = self.select(
                table,
                columns=columns,
                filters=filters,
                order=order,
                limit=size,
                offset=offset,
                count=True,
            )
            pages += 1
            if total is None:
                total = page.total
            collected.extend(page.rows)

            if max_rows is not None and len(collected) >= max_rows:
                if len(collected) > max_rows:
                    del collected[max_rows:]
                if total is not None and len(collected) < total:
                    notes.append(
                        f"stopped at the --max-rows ceiling ({max_rows} of {total} rows)"
                    )
                    return Page(collected, total, 0, max_rows, False, pages, notes)
                return Page(collected, total, 0, max_rows, True, pages, notes)

            if len(page.rows) < size:
                break
            offset += size

        complete = total is None or len(collected) >= total
        if not complete and total is not None:
            notes.append(f"read {len(collected)} of {total} rows")
        return Page(collected, total, 0, len(collected), complete, pages, notes)

    def count(self, table: str, *, filters: dict[str, str] | None = None) -> int | None:
        """Exact row count without fetching rows."""
        params: dict[str, str] = {"select": "id", "limit": "1"}
        if filters:
            params.update(filters)
        response = self._get(table, params=params, headers=self._headers(count=True))
        return _parse_count(response.header("content-range"))

    def rpc_free_select(self, table: str, *, where: dict[str, str], columns: Sequence[str], limit: int = 1) -> list[dict[str, Any]]:
        """Small helper for existence checks."""
        return self.select(
            table, columns=columns, filters=where, limit=limit
        ).rows


def _parse_count(content_range: str | None) -> int | None:
    """`0-24/137` -> 137. `*/0` -> 0. Anything unparseable -> None."""
    if not content_range or "/" not in content_range:
        return None
    tail = content_range.rsplit("/", 1)[1].strip()
    if tail == "*":
        return None
    try:
        return int(tail)
    except ValueError:
        return None


def _category_for_status(http_status: int | None) -> str:
    if http_status in (401,):
        return status_mod.AUTHENTICATION_ERROR
    if http_status in (403,):
        return status_mod.PERMISSION_ERROR
    if http_status is None:
        return status_mod.PROVIDER_ERROR
    if 500 <= http_status < 600:
        return status_mod.PROVIDER_ERROR
    return status_mod.PROVIDER_ERROR


def eq(value: str) -> str:
    return f"eq.{value}"


def gte(value: str) -> str:
    return f"gte.{value}"


def lt(value: str) -> str:
    return f"lt.{value}"


def is_null() -> str:
    return "is.null"


def not_null() -> str:
    return "not.is.null"


def in_list(values: Iterable[str]) -> str:
    joined = ",".join(quote(str(v), safe="") for v in values)
    return f"in.({joined})"