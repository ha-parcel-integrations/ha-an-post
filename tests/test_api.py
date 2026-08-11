"""Tests for the An Post API client — the Gigya auth chain and my-deliveries-api.

Uses a small ``FakeSession``/``FakeResponse`` pair rather than mock chains:
the auth flow makes several calls in sequence (bootstrap -> login -> getJWT),
and queuing canned responses per HTTP method keeps each test's intent visible.
"""
import json

import aiohttp
import pytest

from custom_components.an_post.api import (
    AnPostApiClient,
    AnPostApiError,
    AnPostAuth,
    AnPostAuthError,
)
from custom_components.an_post.const import (
    GIGYA_ERROR_INVALID_CREDENTIALS,
    GIGYA_ERROR_OK,
    GIGYA_ERROR_PERMISSION_DENIED,
)

from .payloads import active_sample

EMAIL = "user@example.test"
PASSWORD = "hunter2"


class _Cookie:
    def __init__(self, value: str) -> None:
        self.value = value


class FakeResponse:
    """Stand-in for an aiohttp response used as an async context manager."""

    def __init__(self, status: int = 200, body: object = None, cookies=None):
        self.status = status
        self._body = body
        self.cookies = cookies or {}

    async def json(self, content_type=None):
        if isinstance(self._body, str):
            raise json.JSONDecodeError("bad body", self._body, 0)
        return self._body

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc_info):
        return False


class FakeSession:
    """Queues canned responses per HTTP method, popped in call order."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, str, dict]] = []
        self._queues: dict[str, list[FakeResponse]] = {
            "GET": [],
            "POST": [],
            "REQUEST": [],
        }

    def queue(self, method: str, response: FakeResponse) -> None:
        self._queues[method].append(response)

    def get(self, url, **kwargs):
        self.calls.append(("GET", url, kwargs))
        return self._queues["GET"].pop(0)

    def post(self, url, **kwargs):
        self.calls.append(("POST", url, kwargs))
        return self._queues["POST"].pop(0)

    def request(self, method, url, **kwargs):
        self.calls.append((method, url, kwargs))
        return self._queues["REQUEST"].pop(0)


def _bootstrap_ok(gmid: str = "gmid-value") -> FakeResponse:
    return FakeResponse(200, {"errorCode": 0, "hasGmid": "ver4"}, {"gmid": _Cookie(gmid)})


def _login_ok(uid: str = "uid-123", login_token: str = "login-token-abc") -> FakeResponse:
    return FakeResponse(
        200,
        {
            "errorCode": GIGYA_ERROR_OK,
            "UID": uid,
            "sessionInfo": {"cookieValue": login_token, "cookieName": "glt"},
        },
    )


def _jwt_ok(id_token: str = "id-token-xyz") -> FakeResponse:
    return FakeResponse(200, {"errorCode": GIGYA_ERROR_OK, "id_token": id_token})


def _auth(session: FakeSession) -> AnPostAuth:
    return AnPostAuth(EMAIL, PASSWORD, session)


def _client(session: FakeSession) -> AnPostApiClient:
    return AnPostApiClient(EMAIL, PASSWORD, session)


# ---------------------------------------------------------------------------
# AnPostAuth.async_login
# ---------------------------------------------------------------------------


async def test_login_sets_uid_and_captures_gmid_cookie():
    session = FakeSession()
    session.queue("GET", _bootstrap_ok("captured-gmid"))
    session.queue("POST", _login_ok(uid="uid-1", login_token="token-1"))

    auth = _auth(session)
    await auth.async_login()

    assert auth.uid == "uid-1"
    # The captured gmid is forwarded on the login POST, not just the bootstrap GET.
    login_call = session.calls[1]
    assert login_call[2]["data"]["gmid"] == "captured-gmid"


async def test_bootstrap_tolerates_a_missing_gmid_cookie():
    session = FakeSession()
    session.queue("GET", FakeResponse(200, {"errorCode": 0}))  # no gmid cookie
    session.queue("POST", _login_ok())

    auth = _auth(session)
    await auth.async_login()  # must not raise

    assert session.calls[1][2]["data"]["gmid"] == ""


async def test_login_raises_auth_error_on_invalid_credentials():
    session = FakeSession()
    session.queue("GET", _bootstrap_ok())
    session.queue(
        "POST",
        FakeResponse(200, {"errorCode": GIGYA_ERROR_INVALID_CREDENTIALS}),
    )

    with pytest.raises(AnPostAuthError):
        await _auth(session).async_login()


async def test_login_raises_api_error_on_other_gigya_error():
    """A non-credential Gigya error must not look like bad credentials."""
    session = FakeSession()
    session.queue("GET", _bootstrap_ok())
    session.queue("POST", FakeResponse(200, {"errorCode": 500001, "errorMessage": "boom"}))

    with pytest.raises(AnPostApiError) as err:
        await _auth(session).async_login()
    assert not isinstance(err.value, AnPostAuthError)


async def test_login_raises_on_missing_uid_or_token():
    session = FakeSession()
    session.queue("GET", _bootstrap_ok())
    session.queue("POST", FakeResponse(200, {"errorCode": 0, "UID": "uid-1"}))  # no sessionInfo

    with pytest.raises(AnPostApiError):
        await _auth(session).async_login()


async def test_login_raises_on_unparseable_body():
    session = FakeSession()
    session.queue("GET", _bootstrap_ok())
    session.queue("POST", FakeResponse(200, "not json"))

    with pytest.raises(AnPostApiError):
        await _auth(session).async_login()


async def test_login_raises_on_non_object_body():
    session = FakeSession()
    session.queue("GET", _bootstrap_ok())
    session.queue("POST", FakeResponse(200, ["nope"]))

    with pytest.raises(AnPostApiError):
        await _auth(session).async_login()


async def test_login_propagates_network_error():
    class _BrokenSession(FakeSession):
        def get(self, url, **kwargs):
            raise aiohttp.ClientError("boom")

    with pytest.raises(aiohttp.ClientError):
        await _auth(_BrokenSession()).async_login()


# ---------------------------------------------------------------------------
# AnPostAuth.async_id_token
# ---------------------------------------------------------------------------


async def test_id_token_logs_in_first_when_not_yet_authenticated():
    session = FakeSession()
    session.queue("GET", _bootstrap_ok())
    session.queue("POST", _login_ok())
    session.queue("POST", _jwt_ok("fresh-token"))

    token = await _auth(session).async_id_token()

    assert token == "fresh-token"


async def test_id_token_reauthenticates_once_on_expired_session():
    """errorCode 403007 from getJWT means the session died — retry once."""
    session = FakeSession()
    session.queue("GET", _bootstrap_ok())
    session.queue("POST", _login_ok())  # initial login (not yet authenticated)
    session.queue("POST", FakeResponse(200, {"errorCode": GIGYA_ERROR_PERMISSION_DENIED}))
    session.queue("GET", _bootstrap_ok())
    session.queue("POST", _login_ok())  # re-login after expiry
    session.queue("POST", _jwt_ok("token-after-relogin"))

    token = await _auth(session).async_id_token()

    assert token == "token-after-relogin"


async def test_id_token_raises_auth_error_when_relogin_also_fails():
    session = FakeSession()
    session.queue("GET", _bootstrap_ok())
    session.queue("POST", _login_ok())
    session.queue("POST", FakeResponse(200, {"errorCode": GIGYA_ERROR_PERMISSION_DENIED}))
    session.queue("GET", _bootstrap_ok())
    session.queue(
        "POST", FakeResponse(200, {"errorCode": GIGYA_ERROR_INVALID_CREDENTIALS})
    )

    with pytest.raises(AnPostAuthError):
        await _auth(session).async_id_token()


async def test_id_token_raises_on_missing_id_token():
    session = FakeSession()
    session.queue("GET", _bootstrap_ok())
    session.queue("POST", _login_ok())
    session.queue("POST", FakeResponse(200, {"errorCode": GIGYA_ERROR_OK}))

    with pytest.raises(AnPostApiError):
        await _auth(session).async_id_token()


# ---------------------------------------------------------------------------
# AnPostApiClient.async_login
# ---------------------------------------------------------------------------


async def test_client_login_returns_account_info():
    session = FakeSession()
    session.queue("GET", _bootstrap_ok())
    session.queue("POST", _login_ok(uid="uid-42"))

    account = await _client(session).async_login()

    assert account == {"uid": "uid-42", "email": EMAIL}


# ---------------------------------------------------------------------------
# AnPostApiClient.async_get_parcels
# ---------------------------------------------------------------------------


def _authed_session() -> FakeSession:
    """A session pre-loaded with a successful bootstrap/login/getJWT trio."""
    session = FakeSession()
    session.queue("GET", _bootstrap_ok())
    session.queue("POST", _login_ok())
    session.queue("POST", _jwt_ok())
    return session


async def test_get_parcels_returns_the_tracking_items_list():
    session = _authed_session()
    session.queue(
        "GET",
        FakeResponse(200, {"success": True, "errors": [], "trackingItems": [active_sample()]}),
    )

    parcels = await _client(session).async_get_parcels()

    assert len(parcels) == 1
    assert parcels[0]["trackingNumber"] == active_sample()["trackingNumber"]
    # The id_token goes on the Authorization header with no "Bearer" prefix.
    get_call = session.calls[-1]
    assert get_call[2]["headers"]["Authorization"] == "id-token-xyz"


async def test_get_parcels_skips_non_dict_entries():
    session = _authed_session()
    session.queue(
        "GET",
        FakeResponse(
            200, {"success": True, "errors": [], "trackingItems": [active_sample(), "junk"]}
        ),
    )
    assert len(await _client(session).async_get_parcels()) == 1


async def test_get_parcels_raises_on_success_false():
    session = _authed_session()
    session.queue(
        "GET",
        FakeResponse(
            200,
            {"success": False, "errors": [{"message": "no account"}], "trackingItems": []},
        ),
    )
    with pytest.raises(AnPostApiError, match="no account"):
        await _client(session).async_get_parcels()


@pytest.mark.parametrize("status", [401, 403])
async def test_get_parcels_raises_auth_error_on_expired_session(status):
    session = _authed_session()
    session.queue("GET", FakeResponse(status, {}))
    with pytest.raises(AnPostAuthError):
        await _client(session).async_get_parcels()


async def test_get_parcels_raises_on_error_status():
    session = _authed_session()
    session.queue("GET", FakeResponse(503, {}))
    with pytest.raises(AnPostApiError):
        await _client(session).async_get_parcels()


async def test_get_parcels_raises_on_unparseable_body():
    session = _authed_session()
    session.queue("GET", FakeResponse(200, "not json"))
    with pytest.raises(AnPostApiError):
        await _client(session).async_get_parcels()


async def test_get_parcels_raises_on_non_object_body():
    session = _authed_session()
    session.queue("GET", FakeResponse(200, ["nope"]))
    with pytest.raises(AnPostApiError):
        await _client(session).async_get_parcels()


async def test_get_parcels_raises_without_a_tracking_items_list():
    session = _authed_session()
    session.queue("GET", FakeResponse(200, {"success": True, "errors": [], "trackingItems": "nope"}))
    with pytest.raises(AnPostApiError):
        await _client(session).async_get_parcels()


# ---------------------------------------------------------------------------
# watchlist add / remove
# ---------------------------------------------------------------------------


async def test_add_to_watchlist_succeeds():
    session = _authed_session()
    session.queue("REQUEST", FakeResponse(201, None))

    await _client(session).async_add_to_watchlist("AB123456789IE")

    method, url, kwargs = session.calls[-1]
    assert method == "POST"
    assert "watchlist/AB123456789IE" in url
    assert kwargs["headers"]["Authorization"] == "id-token-xyz"


async def test_remove_from_watchlist_succeeds():
    session = _authed_session()
    session.queue("REQUEST", FakeResponse(204, None))

    await _client(session).async_remove_from_watchlist("AB123456789IE")

    method, _, _ = session.calls[-1]
    assert method == "DELETE"


@pytest.mark.parametrize("status", [401, 403])
async def test_watchlist_add_raises_auth_error(status):
    session = _authed_session()
    session.queue("REQUEST", FakeResponse(status, None))
    with pytest.raises(AnPostAuthError):
        await _client(session).async_add_to_watchlist("AB123456789IE")


async def test_watchlist_add_raises_api_error_on_other_status():
    session = _authed_session()
    session.queue("REQUEST", FakeResponse(500, None))
    with pytest.raises(AnPostApiError):
        await _client(session).async_add_to_watchlist("AB123456789IE")
