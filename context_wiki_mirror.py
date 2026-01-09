#!/usr/bin/env python3
# ConTeXt Garden Wiki Mirror
# https://github.com/gucci-on-fleek/context-wiki-mirror
# SPDX-License-Identifier: MPL-2.0+
# SPDX-FileCopyrightText: 2025 Max Chernoff

###############
### Imports ###
###############

from argparse import ArgumentDefaultsHelpFormatter, ArgumentParser
from asyncio import run as asyncio_run
from collections.abc import AsyncGenerator
from pathlib import Path
from pprint import pp as pprint
from sys import exit
from tomllib import load as toml_load
from typing import Any, NoReturn, NotRequired, TypedDict, cast

from aiohttp import ClientSession


########################
### Type Definitions ###
########################

QueryListResponse = TypedDict(
    "QueryListResponse",
    {
        "continue": NotRequired[dict[str, str]],
        "query": dict[str, list[dict[str, Any]]],
    },
)


class PageResponse(TypedDict):
    pageid: int
    ns: int
    title: str


#################
### Constants ###
#################

STATUS_OK = 0
WIKI_URL = "https://wiki.contextgarden.net/"
USER_AGENT = "context-wiki-mirror/0.1.0 (+https://github.com/gucci-on-fleek/context-wiki-mirror)"


#########################
### Class Definitions ###
#########################


class Wiki:
    def __init__(self, base_url: str, username: str, password: str) -> None:
        self._base_url = base_url
        self._username = username
        self._password = password
        self._api_params = {
            "assertuser": username.split("@", maxsplit=1)[0],
            "format": "json",
            "formatversion": "2",
        }
        self._page_params = {
            "action": "render",
            "uselang": "en",
            "safemode": "1",
        }
        self._session: ClientSession | None = None

    async def api_query_list(
        self, params: dict[str, str]
    ) -> AsyncGenerator[dict[str, Any]]:
        """Get data from the MediaWiki API."""
        assert self._session is not None

        if "list" not in params:
            raise ValueError('The "list" parameter must be specified.')

        last_continue = {}
        while True:
            async with self._session.get(
                url="api.php",
                params={
                    "action": "query",
                    **params,
                    **last_continue,
                    **self._api_params,
                },
            ) as response:
                json = cast(QueryListResponse, await response.json())
                pprint(json)
                results = next(iter(json["query"].values()))
                last_continue = json.get("continue", None)
                for result in results:
                    yield result
                if last_continue is None:
                    break

    async def get_page_html(self, page_id: int) -> str:
        """Get the HTML content of a wiki page by its page ID."""
        assert self._session is not None

        async with self._session.get(
            url="index.php",
            params={
                **self._page_params,
                "curid": str(page_id),
            },
        ) as response:
            return await response.text()

    async def _login(self) -> None:
        """Log in to the wiki."""
        assert self._session is not None

        # Get a login token
        async with self._session.get(
            url="api.php",
            params={
                "format": "json",
                "action": "query",
                "meta": "tokens",
                "type": "login",
            },
        ) as response:
            json = await response.json()
            token = json["query"]["tokens"]["logintoken"]
            if not isinstance(token, str):
                raise ValueError("Invalid login token")

        # Log in
        async with self._session.post(
            url="api.php",
            data={
                "format": "json",
                "action": "login",
                "lgname": self._username,
                "lgpassword": self._password,
                "lgtoken": token,
            },
        ) as response:
            json = await response.json()
            pprint(json)
            result = json["login"]["result"]
            if result != "Success":
                raise ValueError(f"Login failed: {result}")

        if not any(
            "session" in cookie.key for cookie in self._session.cookie_jar
        ):
            raise ValueError("Login failed: No session cookie found")

    async def __aenter__(self) -> "Wiki":
        """Enter the async context manager."""

        # Create the session
        self._session = ClientSession(
            headers={"User-Agent": USER_AGENT},
            base_url=self._base_url,
            raise_for_status=True,
        )

        # Log in
        await self._login()
        return self

    async def __aexit__(self, *_) -> None:
        if self._session:
            await self._session.close()
            self._session = None


############################
### Function Definitions ###
############################


###################
### Entry Point ###
###################


async def async_main(username: str, password: str) -> None:
    async with Wiki(WIKI_URL, username, password) as wiki:
        async for page in wiki.api_query_list({
            "list": "allpages",
            "aplimit": "max",
            "apfilterredir": "nonredirects",
        }):
            page = cast(PageResponse, page)
            pprint(page)


def main() -> NoReturn:
    parser = ArgumentParser(
        description="Mirrors the ConTeXt Garden Wiki.",
        formatter_class=ArgumentDefaultsHelpFormatter,
        suggest_on_error=True,
    )

    parser.add_argument(
        "--credentials-file",
        type=Path,
        help="The path to process.",
        nargs="?",
        default=Path(Path(__file__).resolve().parents[0] / "credentials.toml"),
    )

    # Parse the arguments
    args = parser.parse_args()
    credentials_file: Path = args.credentials_file

    # Load credentials
    with credentials_file.open("rb") as f:
        credentials = toml_load(f)

    if not credentials.get("username") or not credentials.get("password"):
        parser.error(
            f'The credentials file "{credentials_file}" is missing a username or password.'
        )

    # Run the main async function
    asyncio_run(async_main(credentials["username"], credentials["password"]))

    exit(STATUS_OK)


if __name__ == "__main__":
    main()
