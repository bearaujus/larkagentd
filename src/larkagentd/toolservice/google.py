"""Bounded, read-only Sheets/Docs access using the configured service account.

Uses the existing cryptography and httpx dependencies for Google's OAuth JWT exchange.
No delegated user, credential discovery, arbitrary endpoint or upstream error text.
"""

import base64
import json
import re
import threading
import time
from urllib.parse import quote

import httpx
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding, rsa

from ..core.googlefeedback import configured_email
from ..core.googlefeedback import refusal as refusal
from ..core.googlelinks import parse_link

TOKEN_URL = "https://oauth2.googleapis.com/token"
SCOPES = " ".join(
    "https://www.googleapis.com/auth/" + scope
    for scope in ("spreadsheets.readonly", "documents.readonly")
)
MAX_BYTES = 2_000_000
MAX_TEXT = 8_000
CONFIG_ERROR = "GOOGLE_SERVICE_ACCOUNT_JSON must contain a valid service_account JSON with client_email and an RSA private_key."


class GoogleError(Exception):
    """Only fixed codes cross the reader boundary."""


def credentials(raw):
    try:
        data = json.loads(raw)
        email = configured_email(raw)
        if email is None:
            raise ValueError
        key = serialization.load_pem_private_key(data["private_key"].encode(), None)
        if not isinstance(key, rsa.RSAPrivateKey) or key.key_size < 2048:
            raise ValueError
        return email, key
    except Exception:
        raise ValueError(CONFIG_ERROR) from None


def encoded(value):
    return base64.urlsafe_b64encode(value).rstrip(b"=")


class GoogleReader:
    def __init__(self, raw, *, transport=None):
        self.email, self._key = credentials(raw)
        self._transport = transport
        self._lock = threading.Lock()
        self._token, self._expires = None, 0

    def _request(self, method, url, **kwargs):
        try:
            with httpx.Client(
                timeout=10,
                follow_redirects=False,
                trust_env=False,
                transport=self._transport,
            ) as client:
                with client.stream(method, url, **kwargs) as response:
                    content = bytearray()
                    deadline = time.monotonic() + 10
                    for chunk in response.iter_bytes(chunk_size=16_384):
                        content.extend(chunk)
                        if len(content) > MAX_BYTES:
                            raise GoogleError("result_too_large")
                        if time.monotonic() > deadline:
                            raise GoogleError("google_unavailable")
                    try:
                        data = json.loads(content)
                    except (ValueError, UnicodeError):
                        data = {}
                    if not isinstance(data, dict):
                        data = {}
                    status = response.status_code
                    if url == TOKEN_URL and 400 <= status < 500 and status != 429:
                        raise GoogleError("unauthorized")
                    if status == 403:
                        error = data.get("error")
                        error = error if isinstance(error, dict) else {}
                        reasons = {
                            item.get("reason")
                            for field in ("errors", "details")
                            for item in (error.get(field) or [])
                            if isinstance(item, dict)
                            and isinstance(item.get("reason"), str)
                        }
                        if reasons & {"SERVICE_DISABLED", "accessNotConfigured"}:
                            raise GoogleError("api_disabled")
                        if reasons & {
                            "rateLimitExceeded",
                            "userRateLimitExceeded",
                            "RATE_LIMIT_EXCEEDED",
                        }:
                            raise GoogleError("rate_limited")
                        raise GoogleError("forbidden")
                    if status >= 400 or 300 <= status < 400:
                        raise GoogleError(
                            "rate_limited"
                            if status == 429
                            else "google_unavailable"
                            if status >= 500
                            else "unauthorized"
                            if url == TOKEN_URL or status == 401
                            else "not_found"
                            if status == 404
                            else "bad_range"
                            if status == 400
                            else "google_unavailable"
                        )
                    if not data:
                        raise GoogleError("bad_response")
                    return data
        except httpx.HTTPError:
            raise GoogleError("google_unavailable") from None

    def _access_token(self):
        with self._lock:
            if self._token and time.time() < self._expires - 60:
                return self._token
            now = int(time.time())
            parts = [
                encoded(json.dumps(value).encode())
                for value in (
                    {"alg": "RS256", "typ": "JWT"},
                    {
                        "iss": self.email,
                        "scope": SCOPES,
                        "aud": TOKEN_URL,
                        "iat": now,
                        "exp": now + 3600,
                    },
                )
            ]
            message = b".".join(parts)
            signature = self._key.sign(message, padding.PKCS1v15(), hashes.SHA256())
            data = self._request(
                "POST",
                TOKEN_URL,
                data={
                    "grant_type": "urn:ietf:params:oauth:grant-type:jwt-bearer",
                    "assertion": (message + b"." + encoded(signature)).decode(),
                },
            )
            token, expires = data.get("access_token"), data.get("expires_in")
            if (
                not isinstance(token, str)
                or not re.fullmatch(r"[A-Za-z0-9._~+/-]{1,8192}=*", token)
                or not isinstance(expires, (int, float))
                or not 0 < expires <= 3600
            ):
                raise GoogleError("unauthorized")
            self._token, self._expires = token, time.time() + expires
            return token

    def _get(self, url, **params):
        token = self._access_token()
        try:
            return self._request(
                "GET", url, params=params, headers={"Authorization": "Bearer " + token}
            )
        except GoogleError as exc:
            if str(exc) == "unauthorized":
                with self._lock:
                    self._token, self._expires = None, 0
            raise

    def read(self, url, cell_range=None):
        parsed = parse_link(url)
        if not parsed:
            raise GoogleError("bad_link")
        kind, token, gid = parsed
        try:
            if kind == "spreadsheets":
                return self._sheet(token, gid, cell_range)
            if cell_range is not None:
                raise GoogleError("bad_range")
            data = self._get(
                f"https://docs.googleapis.com/v1/documents/{token}",
                includeTabsContent="true",
            )
            pieces = []

            def visit(value):
                if isinstance(value, dict):
                    if isinstance(value.get("textRun"), dict):
                        pieces.append(value["textRun"].get("content", ""))
                    if "inlineObjectElement" in value:
                        pieces.append("[image, not read]")
                    for key in (
                        "tabs",
                        "documentTab",
                        "childTabs",
                        "body",
                        "content",
                        "paragraph",
                        "elements",
                        "table",
                        "tableRows",
                        "tableCells",
                        "tableOfContents",
                    ):
                        if key in value:
                            visit(value[key])
                elif isinstance(value, list):
                    for item in value:
                        visit(item)

            visit(data)
            text = "".join(pieces)
            return {
                "kind": "document",
                "title": data.get("title", "")[:300],
                "text": text[:MAX_TEXT],
                "cut": len(text) > MAX_TEXT,
                "coverage": "Body text and tables from all tabs; images, headers, footers and comments are not read.",
            }
        except (KeyError, IndexError, TypeError, AttributeError, RecursionError):
            raise GoogleError("bad_response") from None

    def _sheet(self, token, gid, cell_range):
        base = f"https://sheets.googleapis.com/v4/spreadsheets/{token}"
        if cell_range is not None:
            # Require a finite rectangle, never a named range or whole rows/columns.
            match = re.fullmatch(
                r"(?:(?:'[^\n]{1,200}'|[^!'\n]{1,200})!)?([A-Za-z]{1,3})([1-9]\d{0,6}):([A-Za-z]{1,3})([1-9]\d{0,6})",
                cell_range,
            )
            if not match:
                raise GoogleError("bad_range")
            first, row1, last, row2 = match.groups()

            def column(value):
                result = 0
                for char in value.upper():
                    result = result * 26 + ord(char) - 64
                return result

            if not (
                0 <= column(last) - column(first) < 30
                and 0 <= int(row2) - int(row1) < 100
            ):
                raise GoogleError("bad_range")
        meta = self._get(
            base, fields="properties(title),sheets(properties(sheetId,title))"
        )
        sheets = [s["properties"] for s in meta["sheets"]]
        chosen = (
            next((s for s in sheets if str(s["sheetId"]) == gid), None)
            if gid
            else sheets[0]
        )
        if chosen is None:
            raise GoogleError("bad_range")
        if cell_range is None or "!" not in cell_range:
            title = chosen["title"].replace("'", "''")
            cell_range = f"'{title}'!" + (cell_range or "A1:AD100")
        data = self._get(
            base + "/values/" + quote(cell_range, safe=""), majorDimension="ROWS"
        )
        values = data.get("values", [])
        if (
            not isinstance(values, list)
            or len(values) > 100
            or any(not isinstance(row, list) or len(row) > 30 for row in values)
        ):
            raise GoogleError("bad_response")
        result = {
            "kind": "spreadsheet",
            "title": meta["properties"]["title"][:300],
            "sheets": sheets[:100],
            "range": data.get("range", cell_range),
            "values": values,
            "bounded": True,
        }
        if len(json.dumps(result)) > 55_000:
            raise GoogleError("result_too_large")
        return result
