#!/usr/bin/env python3
# ConTeXt Garden Wiki Mirror
# https://github.com/gucci-on-fleek/context-wiki-mirror
# SPDX-License-Identifier: MPL-2.0+ or GFDL-1.2+
# SPDX-FileCopyrightText: 2026 Max Chernoff

###############
### Imports ###
###############

from argparse import ArgumentDefaultsHelpFormatter, ArgumentParser
from asyncio import CancelledError, TaskGroup, get_running_loop
from asyncio import run as asyncio_run
from collections.abc import AsyncGenerator, Callable
from datetime import date
from pathlib import Path
from pprint import pp as pprint
from re import compile as regex_compile
from re import sub as regex_replace
from shutil import move as move_file
from shutil import rmtree as remove_tree
from sys import exit, stderr
from tomllib import load as toml_load
from traceback import print_exception
from types import CoroutineType
from typing import Any, Literal, NoReturn, NotRequired, TypedDict, cast
from urllib.parse import unquote as url_decode

from aiohttp import ClientSession, ClientTimeout, TCPConnector
from bs4 import BeautifulSoup, Comment
from jinja2 import Environment
from jinja2.environment import Template
from pyvips import Image as vips
from pyvips import enums as vips_enums


########################
### Type Definitions ###
########################

vips: Any

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


class PageInfoValues(TypedDict):
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


class PageLinksValuesLinks(TypedDict):
    ns: int
    title: str


class PageLinksValues(ListPagesValues):
    links: list[PageLinksValuesLinks]


#################
### Constants ###
#################

FAVICON_URL = Path("/favicon.ico")
HOME_URL = Path("/index.html")
HTML_HEADERS = regex_compile(r"h[1-6]")
MAX_CONNECTIONS = 8
MAX_EXCEPTIONS_FOR_SUCCESS = 100
MAX_HTML_HEADER_LEVEL = 6
MAX_IMAGE_DIMENSION = 1_000  # pixels
MIN_PAGES_FOR_SUCCESS = 1_000
SCRIPT_DIR = Path(__file__).resolve().parent
STATUS_ERROR = 1
STATUS_OK = 0
STYLE_URL = Path("/style.css")
TIMEOUT_SECONDS = 10 * 60
USER_AGENT = "context-wiki-mirror/0.1.0 (+https://github.com/gucci-on-fleek/context-wiki-mirror)"
WIKI_URL = "https://wiki.contextgarden.net/"


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


########################
### Global Variables ###
########################

# A mapping from page titles to their redirects
redirects: dict[str, str] = {}

# The number of supressed exceptions
suppressed_exceptions_count: int = 0

# Whether to print verbose output
verbose: bool = False

# The number of successfully processed pages
processed_pages_count: int = 0


############################
### Function Definitions ###
############################


def verbose_print(*args: Any, force: bool = False, **kwargs: Any) -> None:
    if verbose or force:
        print(*args, **kwargs, file=stderr)  # noqa: T201


def async_without_exceptions(
    func: Callable[..., CoroutineType[Any, Any, None]],
) -> Callable[..., CoroutineType[Any, Any, None]]:
    """A decorator to suppress exceptions in async functions."""

    async def wrapper(*args: Any, **kwargs: Any) -> None:
        try:
            await func(*args, **kwargs)
        except Exception as e:
            global suppressed_exceptions_count
            suppressed_exceptions_count += 1
            print_exception(e)

    return wrapper


def sync_without_exceptions(
    func: Callable[..., Any],
) -> Callable[..., Any]:
    """A decorator to suppress exceptions in sync functions."""

    def wrapper(*args: Any, **kwargs: Any) -> None:
        try:
            func(*args, **kwargs)
        except Exception as e:
            global suppressed_exceptions_count
            suppressed_exceptions_count += 1
            print_exception(e)

    return wrapper


@async_without_exceptions
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


def normalize_file_name(name: str) -> str:
    """Normalize a file name."""
    return url_decode(name).replace(" ", "_")


def list_pages(
    wiki: Wiki, redirects: Literal["no", "only"]
) -> AsyncGenerator[ListPagesValues]:
    """List all pages in the wiki."""

    response = wiki.api_query_list({
        "list": "allpages",
        "aplimit": "max",
        "apfilterredir": "nonredirects" if redirects == "no" else "redirects",
    })
    return cast(AsyncGenerator[ListPagesValues], response)


@async_without_exceptions
async def get_redirects(wiki: Wiki, page_id: int) -> None:
    """Get the redirects to a specific page."""

    verbose_print(
        f"Started redirects for <{WIKI_URL}index.php?curid={page_id}>"
    )

    response = cast(
        PageLinksValues,
        await wiki.api_query_property({
            "prop": "links",
            "pageids": str(page_id),
        }),
    )
    for link in response["links"]:
        redirects[normalize_file_name(response["title"])] = normalize_file_name(
            link["title"]
        )

    verbose_print(
        f"Finished redirects for <{WIKI_URL}index.php?curid={page_id}>"
    )


async def get_page_info(wiki: Wiki, page_id: int) -> PageInfoValues:
    """Get information about a specific page."""

    response = await wiki.api_query_property({
        "prop": "info",
        "pageids": str(page_id),
        "inprop": "url|displaytitle",
    })
    return cast(PageInfoValues, response)


def normalize_image_url(url: str) -> str:
    """Normalize an image URL."""
    url = regex_replace(
        r"(?<=/)([^/]+)/\d+px-\1", r"\1", normalize_file_name(url)
    )
    if not url.endswith(".ico"):
        url = regex_replace(r"\.[^.]{3,4}$", ".webp", url)
    return url


def make_url_relative(this_url: str, base_path: Path) -> str:
    """Make a URL relative to a base URL."""

    this_url = normalize_file_name(
        this_url.removeprefix(WIKI_URL)
    ).removeprefix("/")
    if this_url in redirects:
        this_url = redirects[this_url]

    this_path = Path("/" + this_url)
    if this_path == Path("/"):
        this_path /= "index"
    if not this_path.suffix:
        this_path = this_path.with_suffix(".html")
    return str(this_path.relative_to(base_path, walk_up=True)).removeprefix(
        "../"
    )


@async_without_exceptions
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
    output_file = output_path / normalize_image_url(url)
    output_file.parent.mkdir(parents=True, exist_ok=True)
    with output_file.open("wb") as f:
        f.write(image_data)

    # Compress the image
    await get_running_loop().run_in_executor(None, compress_image, output_file)


@sync_without_exceptions
def compress_image(path: Path) -> None:
    """Compress an image to fit within the maximum dimensions."""
    if path.suffix.lower() == ".ico":
        return

    image = vips.new_from_file(str(path), access="sequential")
    width = image.width
    height = image.height

    if width > MAX_IMAGE_DIMENSION or height > MAX_IMAGE_DIMENSION:
        scale = min(
            MAX_IMAGE_DIMENSION / width,
            MAX_IMAGE_DIMENSION / height,
        )
        image = image.resize(scale)

    image.webpsave(
        str(path),
        preset=vips_enums.ForeignWebpPreset.TEXT,
        strip=True,
        lossless=True,
        near_lossless=True,
        Q=20,
    )


@async_without_exceptions
async def process_page(  # noqa: PLR0912
    wiki: Wiki,
    output_path: Path,
    template: Template,
    task_group: TaskGroup,
    page_id: int,
) -> None:
    """Process a specific page."""

    verbose_print(f"Started processing <{WIKI_URL}index.php?curid={page_id}>")

    # Run the network requests concurrently
    page_info = await get_page_info(wiki, page_id)
    page_html = await wiki.get_page_html(page_id)

    # Get the page URL
    page_url = (
        Path("/")
        / normalize_file_name(page_info["title"].removeprefix(WIKI_URL))
    ).with_suffix(".html")

    # Process the template
    rendered_content = template.render({
        "title": page_info["displaytitle"],
        "modified_date": page_info["touched"].split("T", maxsplit=1)[0],
        "body": page_html,
        "style": make_url_relative(str(STYLE_URL), page_url),
        "favicon": make_url_relative(str(FAVICON_URL), page_url),
        "home": make_url_relative(str(HOME_URL), page_url),
        "mirror_date": date.today().isoformat(),
        # Use an invalid scheme to avoid early link rewriting
        "canonical": page_info["canonicalurl"].replace(
            "https://", "invalid://"
        ),
        "revision_url": f"{WIKI_URL}index.php?curid={page_id}&diff={page_info['lastrevid']}".replace(
            "https://", "invalid://"
        ),
    })

    # Parse the HTML
    parsed = BeautifulSoup(
        rendered_content,
        "lxml",
        preserve_whitespace_tags={"pre", "p", "code", "td"},
    )

    # Remove comments
    for comment in parsed.find_all(
        string=lambda text: isinstance(text, Comment)
    ):
        comment.extract()

    # Remove empty paragraphs
    for p in parsed.find_all(name="p"):
        if not p.text.strip() and not p.find(name=["img", "svg"]):
            p.decompose()

    for el in parsed.find_all(class_="mw-empty-elt"):
        el.decompose()

    # Fix the headers
    h1s = parsed.find_all(name="h1")
    if len(h1s) == 1:
        # All good
        pass
    elif len(h1s) == 2:  # noqa: PLR2004
        # Remove the second h1
        h1s[1].decompose()
    else:
        # Lower the header levels
        headers = parsed.find_all(name=HTML_HEADERS)
        for header in headers:
            if header.get("id") == "page-title":
                continue
            current_level = int(header.name[1])
            if current_level < MAX_HTML_HEADER_LEVEL:
                header.name = f"h{current_level + 1}"

    # Fix the links
    for link in parsed.find_all(attrs={"href": True}):
        href = cast(str, link.get("href", ""))
        if href.startswith("invalid://"):
            link["href"] = href.replace("invalid://", "https://")

        # Make the links relative
        elif href.startswith(WIKI_URL) or (
            href.startswith("/") and not href.startswith("//")
        ):
            link["href"] = make_url_relative(href, page_url)

    # Download images
    for img in parsed.find_all(name="img"):
        img_src = cast(str, img.get("src", ""))
        if img_src.startswith("/") and not img_src.startswith("//"):
            try:
                del img["srcset"]
            except AttributeError:
                pass
            img["src"] = make_url_relative(
                normalize_image_url(img_src), page_url
            )
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
    output_file = output_path / page_url.relative_to("/")
    output_file.parent.mkdir(parents=True, exist_ok=True)

    with output_file.open("w", encoding="utf-8") as f:
        f.write(formatted)

    # Done
    verbose_print(f"Finished processing <{WIKI_URL}index.php?curid={page_id}>")
    global processed_pages_count
    processed_pages_count += 1


###################
### Entry Point ###
###################


async def async_main(
    username: str,
    password: str,
    output_path: Path,
) -> None:
    """The main async function."""

    # Load the template
    template_path = SCRIPT_DIR / "page.template.html"
    with template_path.open("r", encoding="utf-8") as f:
        template_content = f.read()
    env = Environment()
    template = env.from_string(template_content)

    # Delete the old files
    for item in output_path.iterdir():
        if item.match(".git*") or item.match("README.md"):
            continue
        elif item.is_dir():
            remove_tree(item)
        else:
            item.unlink()

    async with Wiki(WIKI_URL, username, password) as wiki:
        async with TaskGroup() as task_group:
            # Download the style and favicon
            task_group.create_task(write_style(wiki, output_path))
            task_group.create_task(
                download_image(wiki, output_path, "favicon.ico")
            )

            # Get the mapping of redirects
            async for page in list_pages(wiki, redirects="only"):
                task_group.create_task(
                    get_redirects(
                        wiki,
                        page["pageid"],
                    )
                )

        # Download all pages
        async with TaskGroup() as task_group:
            async for page in list_pages(wiki, redirects="no"):
                task_group.create_task(
                    process_page(
                        wiki=wiki,
                        output_path=output_path,
                        template=template,
                        task_group=task_group,
                        page_id=page["pageid"],
                    )
                )

    # Move the Main Page to index.html
    move_file(
        output_path / "Main_Page.html",
        output_path / "index.html",
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
        "--verbose",
        action="store_true",
        help="Enable verbose output.",
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
    global verbose
    verbose = args.verbose

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
    try:
        asyncio_run(
            async_main(
                credentials["username"],
                credentials["password"],
                output_path,
            )
        )
    except (KeyboardInterrupt, CancelledError):
        verbose_print("Operation cancelled.", force=True)
        exit(STATUS_ERROR)

    if (
        processed_pages_count > MIN_PAGES_FOR_SUCCESS
        and suppressed_exceptions_count <= MAX_EXCEPTIONS_FOR_SUCCESS
    ):
        exit(STATUS_OK)
    else:
        exit(STATUS_ERROR)


if __name__ == "__main__":
    main()
