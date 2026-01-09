#!/usr/bin/env python3
# ConTeXt Garden Wiki Mirror
# https://github.com/gucci-on-fleek/context-wiki-mirror
# SPDX-License-Identifier: MPL-2.0+
# SPDX-FileCopyrightText: 2026 Max Chernoff

###############
### Imports ###
###############

from argparse import ArgumentDefaultsHelpFormatter, ArgumentParser
from asyncio import TaskGroup
from asyncio import run as asyncio_run
from collections.abc import AsyncGenerator, Callable
from pathlib import Path
from pprint import pp as pprint
from re import sub as regex_replace
from sys import exit
from tomllib import load as toml_load
from traceback import print_exception
from types import CoroutineType
from typing import Any, NoReturn, NotRequired, TypedDict, cast

from aiohttp import ClientSession, ClientTimeout, TCPConnector
from bs4 import BeautifulSoup
from jinja2 import Environment
from jinja2.environment import Template


########################
### Type Definitions ###
########################

QueryResponse = TypedDict(
    "QueryResponse",
    {
        "continue": NotRequired[dict[str, str]],
        "query": dict[str, list[dict[str, Any]]],
    },
)


class ListPagesValues(TypedDict):
    pageid: int
    ns: int
    title: str


class QueryPagesValues(TypedDict):
    pageid: int
    ns: int
    title: str
    contentmodel: str
    pagelanguage: str
    pagelanguagehtmlcode: str
    pagelanguagedir: str
    touched: str
    lastrevid: int
    length: int
    fullurl: str
    editurl: str
    canonicalurl: str
    displaytitle: str


#################
### Constants ###
#################

STATUS_OK = 0
WIKI_URL = "https://wiki.contextgarden.net/"
USER_AGENT = "context-wiki-mirror/0.1.0 (+https://github.com/gucci-on-fleek/context-wiki-mirror)"
MAX_CONNECTIONS = 8
TIMEOUT_SECONDS = 30
SCRIPT_DIR = Path(__file__).resolve().parent


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
        # Create the session
        self._session = ClientSession(
            headers={"User-Agent": USER_AGENT},
            base_url=self._base_url,
            raise_for_status=True,
            connector=TCPConnector(limit_per_host=MAX_CONNECTIONS),
            timeout=ClientTimeout(total=TIMEOUT_SECONDS),
        )

        # Get function
        self.get = self._session.get

    async def api_query_list(
        self,
        params: dict[str, str],
    ) -> AsyncGenerator[dict[str, Any]]:
        """Get data from the MediaWiki API."""

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
                json = cast(QueryResponse, await response.json())
                results = next(iter(json["query"].values()))
                last_continue = json.get("continue", None)
                for result in results:
                    yield result
                if last_continue is None:
                    break

    async def api_query_property(
        self,
        params: dict[str, str],
    ) -> dict[str, Any]:
        """Get data from the MediaWiki API."""

        if "prop" not in params:
            raise ValueError('The "prop" parameter must be specified.')

        async with self._session.get(
            url="api.php",
            params={
                "action": "query",
                **params,
                **self._api_params,
            },
        ) as response:
            json = cast(QueryResponse, await response.json())
            return next(iter(json["query"].values()))[0]

    async def get_page_html(self, page_id: int) -> str:
        """Get the HTML content of a wiki page by its page ID."""
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
            result = json["login"]["result"]
            if result != "Success":
                raise ValueError(f"Login failed: {result}")

        if not any(
            "session" in cookie.key for cookie in self._session.cookie_jar
        ):
            raise ValueError("Login failed: No session cookie found")

    async def __aenter__(self) -> "Wiki":
        """Enter the async context manager."""
        await self._session.__aenter__()
        # Log in
        await self._login()
        return self

    async def __aexit__(self, *args) -> None:
        """Exit the async context manager."""
        await self._session.__aexit__(*args)


############################
### Function Definitions ###
############################


def without_exceptions(
    func: Callable[..., CoroutineType[Any, Any, None]],
) -> Callable[..., CoroutineType[Any, Any, None]]:
    """A decorator to suppress exceptions in async functions."""

    async def wrapper(*args: Any, **kwargs: Any) -> None:
        try:
            await func(*args, **kwargs)
        except Exception as e:
            print_exception(e)

    return wrapper


@without_exceptions
async def write_style(wiki: Wiki, output_path: Path) -> None:
    """Get the CSS style for the wiki pages."""

    # Paths
    template_path = SCRIPT_DIR / "style.template.css"
    output_file = output_path / "style.css"

    # Get the CSS from the wiki
    async with wiki.get(
        url="index.php",
        params={
            "title": "MediaWiki:Common.css",
            "action": "raw",
        },
    ) as response:
        wiki_css = await response.text()

    # Preprocess the CSS
    wiki_css = regex_replace(
        r"font-family\s*:\s*monospace\s*;?",
        "font-family: var(--monospace-font);",
        wiki_css,
    )
    wiki_css = regex_replace(
        r"font-family\s*:\s*sans-serif\s*;?",
        "",
        wiki_css,
    )
    wiki_css = regex_replace(
        r"font-size\s*:\s*10pt\s*;?",
        "",
        wiki_css,
    )

    # Process the template
    with template_path.open("r", encoding="utf-8") as f:
        template_content = f.read()

    env = Environment(
        variable_start_string="/*{{",
        variable_end_string="}}*/",
    )
    template = env.from_string(template_content)
    rendered_content = template.render({"wiki_css": wiki_css})

    # Write the output file
    with output_file.open("w", encoding="utf-8") as f:
        f.write(rendered_content)


def list_pages(wiki: Wiki) -> AsyncGenerator[ListPagesValues]:
    """List all pages in the wiki."""

    response = wiki.api_query_list({
        "list": "allpages",
        "aplimit": "max",
        "apfilterredir": "nonredirects",
    })
    return cast(AsyncGenerator[ListPagesValues], response)


async def get_page_info(wiki: Wiki, page_id: int) -> QueryPagesValues:
    """Get information about a specific page."""

    response = await wiki.api_query_property({
        "prop": "info",
        "pageids": str(page_id),
        "inprop": "url|displaytitle",
    })
    return cast(QueryPagesValues, response)


@without_exceptions
async def download_image(
    wiki: Wiki,
    output_path: Path,
    url: str,
) -> None:
    """Download an image from the wiki."""

    # Get the image data
    async with wiki.get(url=url) as response:
        image_data = await response.read()

    # Write the image data to a file
    output_file = output_path / url
    output_file.parent.mkdir(parents=True, exist_ok=True)
    with output_file.open("wb") as f:
        f.write(image_data)


async def process_page(
    wiki: Wiki,
    output_path: Path,
    template: Template,
    task_group: TaskGroup,
    page_id: int,
) -> None:
    """Process a specific page."""

    # Run the network requests concurrently
    page_info = await get_page_info(wiki, page_id)
    page_html = await wiki.get_page_html(page_id)

    # Process the template
    rendered_content = template.render({
        "title": page_info["displaytitle"],
        "date": page_info["touched"].split("T", maxsplit=1)[0],
        "body": page_html,
        # Use an invalid scheme to avoid early link rewriting
        "canonical": page_info["canonicalurl"].replace("https", "invalid"),
    })

    # Parse the HTML
    parsed = BeautifulSoup(
        rendered_content, "lxml", preserve_whitespace_tags={"pre", "p", "code"}
    )

    # Remove duplicate h1 headers
    headers = parsed.find_all(name="h1")
    if len(headers) > 1:
        for header in headers[1:]:
            header.decompose()

    # Make all links point here
    for link in parsed.find_all(attrs={"href": True}):
        href = cast(str, link.get("href", ""))
        if href.startswith(WIKI_URL):
            link["href"] = "/" + href.removeprefix(WIKI_URL)
        elif href.startswith("invalid"):
            link["href"] = href.replace("invalid", "https")

    # Download images
    for img in parsed.find_all(name="img"):
        img_src = cast(str, img.get("src", ""))
        if img_src.startswith("/") and not img_src.startswith("//"):
            task_group.create_task(
                download_image(
                    wiki,
                    output_path,
                    img_src.lstrip("/"),
                )
            )

    # Format the HTML
    formatted = parsed.prettify(formatter="html5")

    # Write the output file
    output_file = output_path / page_info["title"]
    output_file.parent.mkdir(parents=True, exist_ok=True)

    with output_file.open("w", encoding="utf-8") as f:
        f.write(formatted)


###################
### Entry Point ###
###################


async def async_main(
    username: str,
    password: str,
    output_path: Path,
) -> None:
    """The main async function."""

    template_path = SCRIPT_DIR / "page.template.html"
    with template_path.open("r", encoding="utf-8") as f:
        template_content = f.read()
    env = Environment()
    template = env.from_string(template_content)

    async with (
        Wiki(WIKI_URL, username, password) as wiki,
        TaskGroup() as task_group,
    ):
        task_group.create_task(write_style(wiki, output_path))
        async for page in list_pages(wiki):
            if page["pageid"] == 6395:
                task_group.create_task(
                    process_page(
                        wiki=wiki,
                        output_path=output_path,
                        template=template,
                        task_group=task_group,
                        page_id=page["pageid"],
                    )
                )


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
        default=SCRIPT_DIR / "credentials.toml",
    )

    parser.add_argument(
        "output_path",
        type=Path,
        help="The output path to save the mirrored wiki.",
        nargs="?",
        default=SCRIPT_DIR / "mirror/",
    )

    # Parse the arguments
    args = parser.parse_args()
    credentials_file: Path = args.credentials_file
    output_path: Path = args.output_path

    # Load credentials
    with credentials_file.open("rb") as f:
        credentials = toml_load(f)

    if not credentials.get("username") or not credentials.get("password"):
        parser.error(
            f'The credentials file "{credentials_file}" is missing a username or password.'
        )

    # Ensure output path exists
    if not output_path.is_dir():
        raise NotADirectoryError(
            f'The output path "{output_path}" is not a directory.'
        )

    # Run the main async function
    asyncio_run(
        async_main(
            credentials["username"],
            credentials["password"],
            output_path,
        )
    )

    exit(STATUS_OK)


if __name__ == "__main__":
    main()
