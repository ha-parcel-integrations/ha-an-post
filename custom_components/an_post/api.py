"""An Post account API client.

Two layers:

* :class:`AnPostAuth` — the Gigya (SAP Customer Data Cloud) session. Owns
  ``{UID, login_token}`` and mints a fresh, short-lived ``id_token`` on
  demand, re-logging in once when the session has expired. See
  ``carrier-research/api/an-post/login.md`` for the full flow.
* :class:`AnPostApiClient` — the ``my-deliveries-api`` calls the coordinator
  and the config flow actually use. It asks :class:`AnPostAuth` for a token
  and never touches Gigya directly.

Contract the config flow / coordinator / services rely on:

* ``async_login`` raises :class:`AnPostAuthError` **only** when the
  credentials were rejected, and :class:`AnPostApiError` for every other
  non-success — a 5xx outage must never push users into a reauth flow they
  cannot complete.
* ``async_get_parcels`` returns the account's parcels as a list of raw dicts.
* ``async_add_to_watchlist`` / ``async_remove_from_watchlist`` add or remove a
  bare tracking number from the account's server-side watchlist; An Post
  itself folds a watchlisted number into the next ``trackingitems`` response,
  so the coordinator needs no extra plumbing to pick it up.
* ``aiohttp.ClientError`` propagates untouched — ``DataUpdateCoordinator``
  already wraps it into ``UpdateFailed``.

The session is injected rather than created here, so the caller controls the
cookie jar (see ``__init__.py`` for why each entry needs its own).
"""
from __future__ import annotations

from typing import Any

import aiohttp

from .const import (
    GIGYA_API_KEY,
    GIGYA_BOOTSTRAP_URL,
    GIGYA_ERROR_INVALID_CREDENTIALS,
    GIGYA_ERROR_OK,
    GIGYA_ERROR_PERMISSION_DENIED,
    GIGYA_GET_JWT_URL,
    GIGYA_LOGIN_URL,
    TRACKING_ITEMS_URL,
    WATCHLIST_ITEM_URL,
)


class AnPostApiError(Exception):
    """Raised when an An Post API call fails for a non-auth reason."""

    def __init__(self, detail: str) -> None:
        """Store the detail that triggered the error."""
        super().__init__(f"An Post API request failed: {detail}")
        self.detail = detail


class AnPostAuthError(AnPostApiError):
    """Raised when An Post rejects the credentials or the session has died.

    Distinct from :class:`AnPostApiError` on purpose: only this one may
    trigger Home Assistant's reauth flow.
    """


async def _post_form(session: aiohttp.ClientSession, url: str, data: dict) -> dict:
    """POST a Gigya form request and return its JSON body.

    Gigya answers **every** call with HTTP 200 and reports the real outcome
    in the body's ``errorCode`` — callers branch on that, not on the HTTP
    status (confirmed in ``login.md``).
    """
    async with session.post(url, data=data) as response:
        try:
            payload = await response.json(content_type=None)
        except ValueError as err:
            raise AnPostApiError(f"unparseable body ({err})") from err
    if not isinstance(payload, dict):
        raise AnPostApiError("unexpected body (not a JSON object)")
    return payload


class AnPostAuth:
    """Owns the Gigya session and mints ``id_token``s on demand.

    ``UID`` and ``login_token`` come from ``accounts.login`` and live for the
    session's lifetime (``SessionExpirationInDays``); the ``id_token`` is
    short-lived and re-minted every poll via ``accounts.getJWT`` rather than
    cached. When ``getJWT`` reports the session has died
    (``errorCode 403007``), :meth:`async_id_token` re-runs the login once with
    the stored password before giving up.
    """

    def __init__(
        self, email: str, password: str, session: aiohttp.ClientSession
    ) -> None:
        """Initialise with credentials and a session."""
        self._email = email
        self._password = password
        self._session = session
        self.uid: str | None = None
        self._login_token: str | None = None
        self._gmid: str = ""

    async def async_login(self) -> None:
        """Bootstrap a ``gmid`` and exchange credentials for a session.

        Raises :class:`AnPostAuthError` on rejected credentials
        (``errorCode 403042``) and :class:`AnPostApiError` on any other
        non-success (outage, unexpected body).
        """
        self._gmid = await self._bootstrap()
        payload = await _post_form(
            self._session,
            GIGYA_LOGIN_URL,
            {
                "apiKey": GIGYA_API_KEY,
                "loginID": self._email,
                "password": self._password,
                "gmid": self._gmid,
                "include": "profile,data",
                "format": "json",
            },
        )
        error_code = payload.get("errorCode")
        if error_code == GIGYA_ERROR_INVALID_CREDENTIALS:
            raise AnPostAuthError(f"invalid credentials (errorCode {error_code})")
        if error_code != GIGYA_ERROR_OK:
            raise AnPostApiError(
                f"login failed (errorCode {error_code}): "
                f"{payload.get('errorMessage')}"
            )

        uid = payload.get("UID")
        login_token = (payload.get("sessionInfo") or {}).get("cookieValue")
        if not uid or not login_token:
            raise AnPostApiError("login response missing UID or login_token")
        self.uid = uid
        self._login_token = login_token

    async def async_id_token(self) -> str:
        """Return a fresh ``id_token``, logging in first if not yet authenticated.

        On a ``403007`` (session expired) the login is retried once with the
        stored password before raising.
        """
        if self.uid is None or self._login_token is None:
            await self.async_login()

        payload = await self._get_jwt()
        error_code = payload.get("errorCode")
        if error_code == GIGYA_ERROR_PERMISSION_DENIED:
            # The stored session has expired; re-authenticate once and retry.
            await self.async_login()
            payload = await self._get_jwt()
            error_code = payload.get("errorCode")

        if error_code != GIGYA_ERROR_OK:
            raise AnPostAuthError(f"getJWT failed (errorCode {error_code})")

        id_token = payload.get("id_token")
        if not id_token:
            raise AnPostApiError("getJWT response missing id_token")
        return id_token

    async def _bootstrap(self) -> str:
        """Fetch a ``gmid`` from ``accounts.webSdkBootstrap``.

        Best-effort: login has been observed to tolerate a missing gmid, so a
        response we cannot read a cookie from yields an empty string rather
        than failing setup outright.
        """
        params = {"apiKey": GIGYA_API_KEY, "format": "json"}
        async with self._session.get(GIGYA_BOOTSTRAP_URL, params=params) as response:
            cookie = response.cookies.get("gmid")
            # The body is informational only (``hasGmid``); a malformed one
            # must not block the flow that follows.
            try:
                await response.json(content_type=None)
            except ValueError:
                pass
        return cookie.value if cookie else ""

    async def _get_jwt(self) -> dict[str, Any]:
        return await _post_form(
            self._session,
            GIGYA_GET_JWT_URL,
            {
                "apiKey": GIGYA_API_KEY,
                "login_token": self._login_token,
                "UID": self.uid,
                "lang": "en",
                "gmid": self._gmid,
                "format": "json",
            },
        )


class AnPostApiClient:
    """Client for the An Post consumer account (``my-deliveries-api``)."""

    def __init__(
        self, email: str, password: str, session: aiohttp.ClientSession
    ) -> None:
        """Initialise the client with credentials and a session."""
        self._email = email
        self._auth = AnPostAuth(email, password, session)
        self._session = session

    async def async_login(self) -> dict[str, Any]:
        """Authenticate and return a small account-info dict.

        The Gigya session itself (``UID``/``login_token``) is owned by
        :class:`AnPostAuth`; this only surfaces the identifiers callers store
        for display / diagnostics.
        """
        await self._auth.async_login()
        return {"uid": self._auth.uid, "email": self._email}

    async def async_get_parcels(self) -> list[dict[str, Any]]:
        """Return the account's parcels (the ``trackingitems`` inbox).

        The envelope is ``{success, errors: [...], trackingItems: [...]}``
        (confirmed live) — branch on ``success`` before trusting the list.
        """
        id_token = await self._auth.async_id_token()
        url = TRACKING_ITEMS_URL.format(uid=self._auth.uid)
        async with self._session.get(
            url, headers=_auth_header(id_token)
        ) as response:
            if response.status in (401, 403):
                # The session expired mid-poll; the coordinator turns this
                # into a reauth via ConfigEntryAuthFailed.
                raise AnPostAuthError(f"HTTP {response.status}")
            if response.status != 200:
                raise AnPostApiError(f"HTTP {response.status}")
            try:
                payload = await response.json(content_type=None)
            except ValueError as err:
                raise AnPostApiError(f"unparseable body ({err})") from err

        if not isinstance(payload, dict):
            raise AnPostApiError("unexpected body (not a JSON object)")
        if not payload.get("success"):
            errors = payload.get("errors") or []
            message = (
                errors[0].get("message")
                if errors and isinstance(errors[0], dict)
                else "unknown error"
            )
            raise AnPostApiError(f"trackingitems reported failure: {message}")

        items = payload.get("trackingItems")
        if not isinstance(items, list):
            raise AnPostApiError("unexpected body (no trackingItems list)")
        return [item for item in items if isinstance(item, dict)]

    async def async_add_to_watchlist(self, tracking_number: str) -> None:
        """Add a bare tracking number to the account's server-side watchlist."""
        await self._watchlist_request("POST", tracking_number)

    async def async_remove_from_watchlist(self, tracking_number: str) -> None:
        """Remove a tracking number from the account's watchlist."""
        await self._watchlist_request("DELETE", tracking_number)

    async def _watchlist_request(self, method: str, tracking_number: str) -> None:
        id_token = await self._auth.async_id_token()
        url = WATCHLIST_ITEM_URL.format(
            uid=self._auth.uid, tracking_number=tracking_number
        )
        async with self._session.request(
            method, url, headers=_auth_header(id_token)
        ) as response:
            if response.status in (401, 403):
                raise AnPostAuthError(f"HTTP {response.status}")
            if response.status not in (200, 201, 204):
                raise AnPostApiError(f"HTTP {response.status}")


def _auth_header(id_token: str) -> dict[str, str]:
    """Return the my-deliveries-api auth header (no "Bearer" prefix)."""
    return {"Authorization": id_token}
