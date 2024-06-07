#!/usr/bin/env python3
import csv
import json
import logging
import os
from dataclasses import dataclass
from datetime import datetime

import trakt.core
from trakt import init

from processor import TVShowProcessor, MovieProcessor
from searcher import TVTimeTVShow, TVTimeMovie

# Setup logger
logging.basicConfig(
    format="%(asctime)s [%(levelname)s] :: %(message)s",
    encoding='utf-8',
    level=logging.INFO,
    datefmt="%Y-%m-%d %H:%M:%S",
#    datefmt="%x %X", #Uncomment for locale date and time, if preffered
)

# Adjust this value to increase/decrease your requests between episodes.
# Make to remain within the rate limit: https://trakt.docs.apiary.io/#introduction/rate-limiting
DELAY_BETWEEN_ITEMS_IN_SECONDS = 1


@dataclass
class Config:
    trakt_username: str
    client_id: str
    client_secret: str
    gdpr_workspace_path: str


def is_authenticated() -> bool:
    with open("pytrakt.json") as f:
        data = json.load(f)
        days_before_expiration = (
                datetime.fromtimestamp(data["OAUTH_EXPIRES_AT"]) - datetime.now()
        ).days
        return days_before_expiration >= 1


def get_configuration() -> Config:
    try:
        with open("config.json") as f:
            data = json.load(f)

        return Config(
            data["TRAKT_USERNAME"],
            data["CLIENT_ID"],
            data["CLIENT_SECRET"],
            data["GDPR_WORKSPACE_PATH"],
        )
    except FileNotFoundError:
        logging.info("config.json not found prompting user for input")
        return Config(
            input("Enter your Trakt.tv username: "),
            input("Enter you Client id: "),
            input("Enter your Client secret: "),
            input("Enter your GDPR workspace path: ")
        )


config = get_configuration()

WATCHED_SHOWS_PATH = config.gdpr_workspace_path + "/tracking-prod-records-v2.csv"
FOLLOWED_SHOWS_PATH = config.gdpr_workspace_path + "/followed_tv_show.csv"
SHOWS_AND_MOVIES_PATH = config.gdpr_workspace_path + "/tracking-prod-records.csv"

def init_trakt_auth() -> bool:
    if is_authenticated():
        return True
    trakt.core.AUTH_METHOD = trakt.core.OAUTH_AUTH
    return init(
        config.trakt_username,
        store=True,
        client_id=config.client_id,
        client_secret=config.client_secret,
    )


def process_watched_shows(path: str) -> None:
    with open(path, newline="", encoding="UTF-8") as csvfile:
        reader = csv.DictReader(csvfile, delimiter=",")
        total_rows = len(list(reader))
        csvfile.seek(0, 0)

        # Ignore the header row
        next(reader, None)
        for rows_count, row in enumerate(reader):
            if row["episode_number"] == "":  # if not an episode entry
                continue
            tv_time_show = TVTimeTVShow(row)
            TVShowProcessor().process_item(tv_time_show, "{:.2f}%".format(rows_count / total_rows * 100))

def process_watched_movies() -> None:
    with open(SHOWS_AND_MOVIES_PATH, newline="") as csvfile:
        reader = filter(lambda p: p["movie_name"] != "", csv.DictReader(csvfile, delimiter=","))
        watched_list = [row["movie_name"] for row in reader if row["type"] == "watch"]
        csvfile.seek(0, 0)
        total_rows = len(list(reader))
        csvfile.seek(0, 0)

        # Ignore the header row
        next(reader, None)
        for rows_count, row in enumerate(reader):
            movie = TVTimeMovie(row)
            MovieProcessor(watched_list).process_item(movie, "{:.2f}%".format(rows_count / total_rows * 100))


def menu_selection() -> int:
    # Display a menu selection
    print(">> What do you want to do?")
    print("    1) Import Watch History for TV Shows from TV Time")
    print("    2) Import Watched Movies from TV Time")
    print("    3) Do both 1 and 2 (default)")
    print("    4) Exit")

    while True:
        try:
            selection = input("Enter your menu selection: ")
            selection = 3 if not selection else int(selection)
            break
        except ValueError:
            logging.warning("Invalid input. Please enter a numerical number.")
    # Check if the input is valid
    if not 1 <= selection <= 4:
        logging.warning("Sorry - that's an unknown menu selection")
        exit()
    # Exit if the 4th option was chosen
    if selection == 4:
        logging.info("Exiting as per user's selection.")
        exit()

    return selection


def start():
    selection = menu_selection()

    # Create the initial authentication with Trakt, before starting the process
    if not init_trakt_auth():
        logging.error(
            "ERROR: Unable to complete authentication to Trakt - please try again."
        )

    if selection == 1:
        logging.info("Processing watched shows.")
        process_watched_shows(SHOWS_AND_MOVIES_PATH)
        process_watched_shows(WATCHED_SHOWS_PATH)
        # TODO: Add support for followed shows
    elif selection == 2:
        logging.info("Processing movies.")
        process_watched_movies()
    elif selection == 3:
        logging.info("Processing both watched shows and movies.")
        process_watched_shows(SHOWS_AND_MOVIES_PATH)
        process_watched_shows(WATCHED_SHOWS_PATH)
        process_watched_movies()


if __name__ == "__main__":
    # Check that the user has provided the GDPR path
    if os.path.isdir(config.gdpr_workspace_path):
        start()
    else:
        logging.error(
            f"Oops! The TV Time GDPR folder 'config.gdpr_workspace_path'"
            " does not exist on the local system. Please check it, and try again."
        )
