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
from pathlib import Path
from pprint import pp as pprint
from sys import exit
from tomllib import load as toml_load
from typing import NoReturn

from aiohttp import ClientSession


########################
### Type Definitions ###
########################


#################
### Constants ###
#################

STATUS_OK = 0


#########################
### Class Definitions ###
#########################


############################
### Function Definitions ###
############################


###################
### Entry Point ###
###################


async def async_main(username: str, password: str) -> None:
    pass


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
        default=Path(Path(__file__).resolve().parents[1] / "credentials.toml"),
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
