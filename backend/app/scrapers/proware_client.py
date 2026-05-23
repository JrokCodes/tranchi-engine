"""
Shared httpx client for ProWare ASP.NET WebForms sites.

ProWare is an Ohio court management platform used by:
  - Cuyahoga County Probate Court (probate.py)
  - Cuyahoga County Sheriff (sheriff.py)

ASP.NET WebForms requires preserving __VIEWSTATE, __EVENTVALIDATION, and
__EVENTTARGET across POST-backs. This client manages that lifecycle so
individual scrapers don't need to handle it directly.

Rate limit: 1 req/sec floor enforced by _rate_limiter(). Probate ToS
prohibits automated data mining; we comply with the spirit by using delta
queries, generic UAs, and this mandatory pacing floor.

Usage:
    async with ProwareSession(base_url) as session:
        await session.accept_agreement(agree_button_id="btnAgree")
        state = await session.fetch_form_state("/search.aspx")
        rows = await session.post_back("search", args={"txtDate": "05/23/2026"}, viewstate=state)
"""
from __future__ import annotations

import asyncio
import logging
import re
import time
from dataclasses import dataclass, field
from typing import Any

import httpx
from bs4 import BeautifulSoup

from app.scrapers.user_agents import default_headers

logger = logging.getLogger(__name__)

_VIEWSTATE_FIELDS = ("__VIEWSTATE", "__VIEWSTATEGENERATOR", "__EVENTVALIDATION")


@dataclass
class FormState:
    """Snapshot of ASP.NET hidden form fields needed for a POST-back."""
    viewstate: str = ""
    viewstate_generator: str = ""
    event_validation: str = ""
    extra: dict[str, str] = field(default_factory=dict)

    def as_post_data(self) -> dict[str, str]:
        """Return all hidden fields as a flat dict for inclusion in POST body."""
        data: dict[str, str] = {
            "__VIEWSTATE": self.viewstate,
            "__VIEWSTATEGENERATOR": self.viewstate_generator,
            "__EVENTVALIDATION": self.event_validation,
        }
        data.update(self.extra)
        return data


class ProwareSession:
    """Stateful session for ProWare ASP.NET WebForms scrapers.

    Manages cookie jar, ViewState, and rate limiting across the lifetime
    of one scrape run. Use as an async context manager.
    """

    def __init__(self, base_url: str, *, rate_limit_sec: float = 1.0) -> None:
        self._base_url = base_url.rstrip("/")
        self._rate_limit_sec = rate_limit_sec
        self._last_request_at: float = 0.0
        self._client: httpx.AsyncClient | None = None

    async def __aenter__(self) -> "ProwareSession":
        self._client = httpx.AsyncClient(
            base_url=self._base_url,
            headers=default_headers(),
            follow_redirects=True,
            timeout=30.0,
        )
        return self

    async def __aexit__(self, *_: Any) -> None:
        if self._client:
            await self._client.aclose()
            self._client = None

    # ── Internal helpers ──────────────────────────────────────────────────────

    async def _rate_limiter(self) -> None:
        """Enforce 1 req/sec floor between requests."""
        elapsed = time.monotonic() - self._last_request_at
        if elapsed < self._rate_limit_sec:
            await asyncio.sleep(self._rate_limit_sec - elapsed)
        self._last_request_at = time.monotonic()

    def _assert_client(self) -> httpx.AsyncClient:
        if self._client is None:
            raise RuntimeError("ProwareSession must be used as an async context manager")
        return self._client

    @staticmethod
    def _extract_form_state(html: str) -> FormState:
        """Parse __VIEWSTATE and friends from an ASP.NET page."""
        soup = BeautifulSoup(html, "html.parser")
        state = FormState()
        for fname in _VIEWSTATE_FIELDS:
            tag = soup.find("input", {"name": fname})
            if tag and tag.get("value"):
                if fname == "__VIEWSTATE":
                    state.viewstate = tag["value"]
                elif fname == "__VIEWSTATEGENERATOR":
                    state.viewstate_generator = tag["value"]
                elif fname == "__EVENTVALIDATION":
                    state.event_validation = tag["value"]
        return state

    # ── Public API ────────────────────────────────────────────────────────────

    async def fetch_form_state(self, path: str) -> FormState:
        """GET a ProWare page and extract its form state.

        Args:
            path: URL path relative to base_url (e.g. "/Search.aspx").

        Returns:
            FormState with current __VIEWSTATE / __EVENTVALIDATION values.
        """
        client = self._assert_client()
        await self._rate_limiter()
        resp = await client.get(path)
        resp.raise_for_status()
        return self._extract_form_state(resp.text)

    async def accept_agreement(self, path: str, agree_button_id: str) -> FormState:
        """POST the Terms-of-Use agreement page to mint a session cookie.

        Many ProWare courts gate searches behind a ToS click-through.
        This method loads the agreement page, extracts its ViewState, then
        POSTs the agree button to obtain the authenticated session cookie.

        Args:
            path: URL path of the agreement page.
            agree_button_id: The name/id of the submit button (e.g. "btnAgree").

        Returns:
            FormState from the landing page after agreement.
        """
        client = self._assert_client()

        # GET the agreement page
        await self._rate_limiter()
        resp = await client.get(path)
        resp.raise_for_status()
        state = self._extract_form_state(resp.text)

        # POST the agreement
        post_data = state.as_post_data()
        post_data["__EVENTTARGET"] = ""
        post_data["__EVENTARGUMENT"] = ""
        post_data[agree_button_id] = "I Agree"

        await self._rate_limiter()
        resp = await client.post(path, data=post_data)
        resp.raise_for_status()

        logger.debug("ProWare agreement accepted at %s%s", self._base_url, path)
        return self._extract_form_state(resp.text)

    async def post_back(
        self,
        path: str,
        *,
        target: str = "",
        argument: str = "",
        extra_fields: dict[str, str] | None = None,
        viewstate: FormState | None = None,
    ) -> tuple[str, FormState]:
        """POST a WebForms event back to the server.

        Args:
            path: URL path to POST to.
            target: Value for __EVENTTARGET (the control that triggered the event).
            argument: Value for __EVENTARGUMENT.
            extra_fields: Additional form fields to include (e.g. search criteria).
            viewstate: FormState from the previous GET. If None, fetches fresh.

        Returns:
            (response_html, new_form_state) — html is the raw page content,
            new_form_state is the updated ViewState for any subsequent POST-backs.
        """
        client = self._assert_client()

        if viewstate is None:
            viewstate = await self.fetch_form_state(path)

        post_data = viewstate.as_post_data()
        post_data["__EVENTTARGET"] = target
        post_data["__EVENTARGUMENT"] = argument
        if extra_fields:
            post_data.update(extra_fields)

        await self._rate_limiter()
        resp = await client.post(path, data=post_data)
        resp.raise_for_status()

        new_state = self._extract_form_state(resp.text)
        return resp.text, new_state
