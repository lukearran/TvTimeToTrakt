#!/usr/bin/env python3
import csv
import json
import logging
import os
import re
import sys
import time
from abc import ABC, abstractmethod
from dataclasses import dataclass
from datetime import datetime
from typing import Optional, TypeVar, Union, Any

import trakt.core
from tinydb import Query, TinyDB
from tinydb.table import Table
from trakt import init
from trakt.movies import Movie
from trakt.tv import TVShow

# Setup logger
logging.basicConfig(
    format="%(asctime)s [%(levelname)7s] :: %(message)s",
    level=logging.INFO,
    datefmt="%Y-%m-%d %H:%M:%S",
)

# Adjust this value to increase/decrease your requests between episodes.
# Make to remain within the rate limit: https://trakt.docs.apiary.io/#introduction/rate-limiting
DELAY_BETWEEN_ITEMS_IN_SECONDS = 1

# Create databases to keep track of completed processes
database = TinyDB("localStorage.json")
syncedEpisodesTable = database.table("SyncedEpisodes")
userMatchedShowsTable = database.table("TvTimeTraktUserMatched")
syncedMoviesTable = database.table("SyncedMovies")
userMatchedMoviesTable = database.table("TvTimeTraktUserMatchedMovies")


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

WATCHED_SHOWS_PATH = config.gdpr_workspace_path + "/seen_episode.csv"
FOLLOWED_SHOWS_PATH = config.gdpr_workspace_path + "/followed_tv_show.csv"
MOVIES_PATH = config.gdpr_workspace_path + "/tracking-prod-records.csv"


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


TraktTVShow = TypeVar("TraktTVShow")
TraktMovie = TypeVar("TraktMovie")

SearchResult = Union[TraktTVShow, TraktMovie]


@dataclass
class Title:
    name: str
    without_year: str
    year: Optional[int]

    def __init__(self, title: str):
        """
        Parse the title's name for year.
        :param title:
        """
        try:
            self.name = title
            # Use a regex expression to get the value within the brackets e.g. The Americans (2017)
            year_search = re.search(r"\(([A-Za-z0-9_]+)\)", title)
            self.year = int(year_search.group(1))
            # Then, get the title without the year value included
            self.without_year = title.split("(")[0].strip()
        except Exception:
            # If the above failed, then the title doesn't include a year
            # so create the value with "defaults"
            self.name = title
            self.without_year = title
            self.year = None

    def items_with_same_name(self, items: list[SearchResult]) -> list[SearchResult]:
        with_same_name = []

        for item in items:
            if self.matches(item.title):
                # If the title included the year of broadcast, then we can be more picky in the results
                # to look for an item with a broadcast year that matches
                if self.year:
                    # If the item title is a 1:1 match, with the same broadcast year, then bingo!
                    if (self.name == item.title) and (item.year == self.year):
                        # Clear previous results, and only use this one
                        with_same_name = [item]
                        break

                    # Otherwise, only add the item if the broadcast year matches
                    if item.year == self.year:
                        with_same_name.append(item)
                # If the item doesn't have the broadcast year, then add all the results
                else:
                    with_same_name.append(item)

        return with_same_name

    def matches(self, other: str) -> bool:
        """
        Shows in TV Time are often different to Trakt.TV - in order to improve results and automation,
        calculate how many words are in the title, and return true if more than 50% of the title is a match,
        It seems to improve automation, and reduce manual selection...
        """

        # If the name is a complete match, then don't bother comparing them!
        if self.name == other:
            return True

        # Go through each word of the TV Time title, and check if it's in the Trakt title
        words_matched = [word for word in self.name.split() if word in other]

        # Then calculate what percentage of words matched
        quotient = len(words_matched) / len(other.split())
        percentage = quotient * 100

        # If more than 50% of words in the TV Time title exist in the Trakt title,
        # then return the title as a possibility to use
        return percentage > 50


class TVTimeItem:
    def __init__(self, name: str, updated_at: str):
        self.name = name
        # Get the date which the show was marked 'watched' in TV Time
        # and parse the watched date value into a Python object
        self.date_watched = datetime.strptime(
            updated_at, "%Y-%m-%d %H:%M:%S"
        )


class TVTimeTVShow(TVTimeItem):
    def __init__(self, row: Any):
        super().__init__(row["tv_show_name"], row["updated_at"])
        self.episode_id = row["episode_id"]
        self.season_number = row["episode_season_number"]
        self.episode_number = row["episode_number"]

    def parse_season_number(self, trakt_show: TraktTVShow) -> int:
        """
        Since the Trakt.Py starts the indexing of seasons in the array from 0 (e.g. Season 1 in Index 0), then
        subtract the TV Time numerical value by 1, so it starts from 0 as well. However, when a TV series includes
        a 'special' season, Trakt.Py will place this as the first season in the array - so, don't subtract, since
        this will match TV Time's existing value.
        """

        season_number = int(self.season_number)
        # Gen get the Season Number from the first item in the array
        first_season_no = trakt_show.seasons[0].number

        # If the season number is 0, then the Trakt show contains a "special" season
        if first_season_no == 0:
            # No need to modify the value, as the TV Time value will match Trakt
            return season_number
        # Otherwise, if the Trakt seasons start with no specials, then return the seasonNo,
        # but subtracted by one (e.g. Season 1 in TV Time, will be 0)
        else:
            # Only subtract if the TV Time season number is greater than 0.
            if season_number != 0:
                return season_number - 1
            # Otherwise, the TV Time season is a special! Then you don't need to change the starting position
            else:
                return season_number


class TVTimeMovie(TVTimeItem):
    def __init__(self, row: Any):
        super().__init__(row["movie_name"], row["updated_at"])
        self.activity_type = row["type"]


class Searcher(ABC):
    def __init__(self, user_matched_table: Table):
        self.name = ""
        self.items_with_same_name = None
        self._user_matched_table = user_matched_table

    def search(self, title: Title) -> Optional[SearchResult]:
        self.name = title.name
        # If the title contains a year, then replace the local variable with the stripped version.
        if title.year:
            self.name = title.without_year
        self.items_with_same_name = title.items_with_same_name(self.search_trakt(self.name))

        single_result = self._check_single_result()
        if single_result:
            return single_result

        # If the search contains multiple results, then we need to confirm with the user which show
        # the script should use, or access the local database to see if the user has already provided
        # a manual selection

        should_return, query_result = self._search_local()
        if should_return:
            return query_result
        # If the user has not provided a manual selection already in the process
        # then prompt the user to make a selection
        else:
            self._handle_multiple_manually()

    @abstractmethod
    def search_trakt(self, name: str) -> list[SearchResult]:
        pass

    @abstractmethod
    def _print_manual_selection(self):
        pass

    def _search_local(self) -> tuple[bool, SearchResult]:
        user_matched_query = Query()
        query_result = self._user_matched_table.search(user_matched_query.Name == self.name)
        # If the local database already contains an entry for a manual selection
        # then don't bother prompting the user to select it again!
        if len(query_result) == 1:
            first_match = query_result[0]
            first_match_selected_index = int(first_match.get("UserSelectedIndex"))
            skip_show = first_match.get("Skip")
            if not skip_show:
                return True, self.items_with_same_name[first_match_selected_index]
            else:
                return True, None
        else:
            return False, None

    def _handle_multiple_manually(self) -> Optional[SearchResult]:
        self._print_manual_selection()
        while True:
            try:
                # Get the user's selection, either a numerical input, or a string 'SKIP' value
                index_selected = input(
                    "Please make a selection from above (or enter SKIP):"
                )

                if index_selected == "SKIP":
                    break

                index_selected = int(index_selected) - 1
                break
            except KeyboardInterrupt:
                sys.exit("Cancel requested...")
            except Exception:
                logging.error(f"Sorry! Please select a value between 0 to {len(self.items_with_same_name)}")

        # If the user entered 'SKIP', then exit from the loop with no selection, which
        # will trigger the program to move onto the next episode
        if index_selected == "SKIP":
            # Record that the user has skipped the TV Show for import, so that
            # manual input isn't required everytime
            self._user_matched_table.insert(
                {"Name": self.name, "UserSelectedIndex": 0, "Skip": True}
            )
            return None
        else:
            selected_show = self.items_with_same_name[int(index_selected)]

            self._user_matched_table.insert(
                {
                    "Name": self.name,
                    "UserSelectedIndex": index_selected,
                    "Skip": False,
                }
            )

            return selected_show

    def _check_single_result(self) -> Optional[SearchResult]:
        complete_match_names = [name_from_search for name_from_search in self.items_with_same_name if
                                name_from_search.title == self.name]
        if len(complete_match_names) == 1:
            return complete_match_names[0]
        elif len(self.items_with_same_name) == 1:
            return self.items_with_same_name[0]
        elif len(self.items_with_same_name) < 1:
            return None


class TVShowSearcher(Searcher):
    def __init__(self, tv_show: TVTimeTVShow):
        super().__init__(userMatchedShowsTable)
        self.tv_show = tv_show

    def search_trakt(self, name: str) -> list[SearchResult]:
        return TVShow.search(name)

    def _print_manual_selection(self) -> None:
        print(
            f"INFO - MANUAL INPUT REQUIRED: The TV Time data for Show '{self.name}'"
            f" (Season {self.tv_show.season_number}, Episode {self.tv_show.episode_number}) has"
            f" {len(self.items_with_same_name)} matching Trakt shows with the same name.\a "
        )

        for idx, item in enumerate(self.items_with_same_name):
            print(
                f"({idx + 1}) {item.title} - {item.year} - {len(item.seasons)} "
                f"Season(s) - More Info: https://trakt.tv/{item.ext}"
            )


class MovieSearcher(Searcher):
    def __init__(self):
        super().__init__(userMatchedMoviesTable)

    def search_trakt(self, name: str) -> list[SearchResult]:
        return Movie.search(name)

    def _print_manual_selection(self) -> None:
        print(
            f"INFO - MANUAL INPUT REQUIRED: The TV Time data for Movie '{self.name}'"
            f" has {len(self.items_with_same_name)}"
            f" matching Trakt movies with the same name.\a"
        )

        for idx, item in enumerate(self.items_with_same_name):
            print(f"({idx + 1}) {item.title} - {item.year} - More Info: https://trakt.tv/{item.ext}")


class Processor(ABC):
    @abstractmethod
    def process_item(self, tv_time_item: TVTimeItem, progress: float) -> None:
        pass


class TVShowProcessor(Processor):
    def __init__(self):
        super().__init__()

    def process_item(self, tv_time_show: TVTimeTVShow, progress: float) -> None:
        # Query the local database for previous entries indicating that
        # the item has already been imported in the past. Which will
        # ease pressure on Trakt's API server during a retry of the import
        # process, and just save time overall without needing to create network requests.
        episode_completed_query = Query()
        synced_episodes = syncedEpisodesTable.search(episode_completed_query.episodeId == tv_time_show.episode_id)

        if len(synced_episodes) != 0:
            logging.info(
                f"({progress}) - Already imported,"
                f" skipping \'{tv_time_show.name}\' Season {tv_time_show.season_number} /"
                f" Episode {tv_time_show.episode_number}."
            )
            return

        # If the query returned no results, then continue to import it into Trakt
        # Create a repeating loop, which will break on success, but repeats on failures
        error_streak = 0
        while True:
            # If more than 10 errors occurred in one streak, whilst trying to import the item
            # then give up, and move onto the next item, but warn the user.
            if error_streak > 10:
                logging.warning("An error occurred 10 times in a row... skipping episode...")
                break
            try:
                # Sleep for a second between each process, before going onto the next watched item.
                # This is required to remain within the API rate limit, and use the API server fairly.
                # Other developers share the service, for free - so be considerate of your usage.
                time.sleep(DELAY_BETWEEN_ITEMS_IN_SECONDS)

                trakt_show = TVShowSearcher(tv_time_show).search(Title(tv_time_show.name))
                if not trakt_show:
                    break

                logging.info(
                    f"({progress}) - Processing '{tv_time_show.name}'"
                    f" Season {tv_time_show.season_number} /"
                    f" Episode {tv_time_show.episode_number}"
                )

                season = trakt_show.seasons[tv_time_show.parse_season_number(trakt_show)]
                episode = season.episodes[int(tv_time_show.episode_number) - 1]
                episode.mark_as_seen(tv_time_show.date_watched)
                # Add the episode to the local database as imported, so it can be skipped,
                # if the process is repeated
                syncedEpisodesTable.insert({"episodeId": tv_time_show.episode_id})
                logging.info(f"'{tv_time_show.name}' marked as seen")

                error_streak = 0
                break
            # Catch errors which occur because of an incorrect array index. This occurs when
            # an incorrect Trakt show has been selected, with season/episodes which don't match TV Time.
            # It can also occur due to a bug in Trakt Py, whereby some seasons contain an empty array of episodes.
            except IndexError:
                tv_show_slug = trakt_show.to_json()["shows"][0]["ids"]["ids"]["slug"]
                logging.warning(
                    f"({progress}) - {tv_time_show.name} Season {tv_time_show.season_number},"
                    f" Episode {tv_time_show.episode_number} does not exist in Trakt!"
                    f" (https://trakt.tv/shows/{tv_show_slug}/seasons/{tv_time_show.season_number}/episodes/{tv_time_show.episode_number})"
                )
                break
            except trakt.core.errors.NotFoundException:
                logging.warning(
                    f"({progress}) - {tv_time_show.name} Season {tv_time_show.season_number},"
                    f" Episode {tv_time_show.episode_number} does not exist (search) in Trakt!"
                )
                break
            except trakt.core.errors.RateLimitException:
                logging.warning(
                    "The program is running too quickly and has hit Trakt's API rate limit!"
                    " Please increase the delay between"
                    " movies via the variable 'DELAY_BETWEEN_EPISODES_IN_SECONDS'."
                    " The program will now wait 60 seconds before"
                    " trying again."
                )
                time.sleep(60)
                error_streak += 1
            # Catch a JSON decode error - this can be raised when the API server is down and produces an HTML page,
            # instead of JSON
            except json.decoder.JSONDecodeError:
                logging.warning(
                    f"({progress}) - A JSON decode error occurred whilst processing {tv_time_show.name}"
                    " This might occur when the server is down and has produced"
                    " a HTML document instead of JSON. The script will wait 60 seconds before trying again."
                )

                time.sleep(60)
                error_streak += 1
            # Catch a CTRL + C keyboard input, and exits the program
            except KeyboardInterrupt:
                sys.exit("Cancel requested...")


class MovieProcessor(Processor):
    def __init__(self, watched_list: list):
        super().__init__()
        self._watched_list = watched_list

    def process_item(self, tv_time_movie: TVTimeMovie, progress: float) -> None:
        # Query the local database for previous entries indicating that
        # the episode has already been imported in the past. Which will
        # ease pressure on Trakt's API server during a retry of the import
        # process, and just save time overall without needing to create network requests.
        movie_query = Query()
        synced_movies = syncedMoviesTable.search(
            (movie_query.movie_name == tv_time_movie.name) & (movie_query.type == "watched")
        )

        if len(synced_movies) != 0:
            logging.info(f"({progress}) - Already imported, skipping '{tv_time_movie.name}'.")
            return

        watchlist_query = Query()
        movies_in_watchlist = syncedMoviesTable.search(
            (watchlist_query.movie_name == tv_time_movie.name) & (watchlist_query.type == "watchlist")
        )

        # If the query returned no results, then continue to import it into Trakt
        # Create a repeating loop, which will break on success, but repeats on failures
        error_streak = 0
        while True:
            # If more than 10 errors occurred in one streak, whilst trying to import the item
            # then give up, and move onto the next item, but warn the user.
            if error_streak > 10:
                logging.warning("An error occurred 10 times in a row... skipping episode...")
                break
            # If movie is watched but this is an entry for watchlist, then skip
            if tv_time_movie.name in self._watched_list and tv_time_movie.activity_type != "watch":
                logging.info(f"Skipping '{tv_time_movie.name}' to avoid redundant watchlist entry.")
                break
            try:
                # Sleep for a second between each process, before going onto the next watched item.
                # This is required to remain within the API rate limit, and use the API server fairly.
                # Other developers share the service, for free - so be considerate of your usage.
                time.sleep(DELAY_BETWEEN_ITEMS_IN_SECONDS)
                trakt_movie = MovieSearcher().search(Title(tv_time_movie.name))
                if not trakt_movie:
                    break

                logging.info(f"({progress}) - Processing '{tv_time_movie.name}'")

                if tv_time_movie.activity_type == "watch":
                    trakt_movie.mark_as_seen(tv_time_movie.date_watched)
                    # Add the episode to the local database as imported, so it can be skipped,
                    # if the process is repeated
                    syncedMoviesTable.insert(
                        {"movie_name": tv_time_movie.name, "type": "watched"}
                    )
                    logging.info(f"'{tv_time_movie.name}' marked as seen")
                elif len(movies_in_watchlist) == 0:
                    trakt_movie.add_to_watchlist()
                    # Add the episode to the local database as imported, so it can be skipped,
                    # if the process is repeated
                    syncedMoviesTable.insert(
                        {"movie_name": tv_time_movie.name, "type": "watchlist"}
                    )
                    logging.info(f"'{tv_time_movie.name}' added to watchlist")
                else:
                    logging.warning(f"{tv_time_movie.name} already in watchlist")

                error_streak = 0
                break
            # Catch errors which occur because of an incorrect array index. This occurs when
            # an incorrect Trakt movie has been selected, with season/episodes which don't match TV Time.
            # It can also occur due to a bug in Trakt Py, whereby some seasons contain an empty array of episodes.
            except IndexError:
                movie_slug = trakt_movie.to_json()["movies"][0]["ids"]["ids"]["slug"]
                logging.warning(
                    f"({progress}) - {tv_time_movie.name}"
                    f" does not exist in Trakt! (https://trakt.tv/movies/{movie_slug}/)"
                )
                break
            except trakt.core.errors.NotFoundException:
                logging.warning(f"({progress}) - {tv_time_movie.name} does not exist (search) in Trakt!")
                break
            except trakt.core.errors.RateLimitException:
                logging.warning(
                    "The program is running too quickly and has hit Trakt's API rate limit!"
                    " Please increase the delay between"
                    " movies via the variable 'DELAY_BETWEEN_EPISODES_IN_SECONDS'."
                    " The program will now wait 60 seconds before"
                    " trying again."
                )
                time.sleep(60)
                error_streak += 1
            except json.decoder.JSONDecodeError:
                logging.warning(
                    f"({progress}) - A JSON decode error occurred whilst processing {tv_time_movie.name}"
                    " This might occur when the server is down and has produced"
                    " a HTML document instead of JSON. The script will wait 60 seconds before trying again."
                )

                time.sleep(60)
                error_streak += 1
            # Catch a CTRL + C keyboard input, and exits the program
            except KeyboardInterrupt:
                sys.exit("Cancel requested...")


def process_watched_shows() -> None:
    with open(WATCHED_SHOWS_PATH, newline="") as csvfile:
        reader = csv.DictReader(csvfile, delimiter=",")
        total_rows = len(list(reader))
        csvfile.seek(0, 0)

        # Ignore the header row
        next(reader, None)
        for rows_count, row in enumerate(reader):
            tv_time_show = TVTimeTVShow(row)
            TVShowProcessor().process_item(tv_time_show, rows_count / total_rows)


def process_watched_movies() -> None:
    with open(MOVIES_PATH, newline="") as csvfile:
        reader = filter(lambda p: p["movie_name"] != "", csv.DictReader(csvfile, delimeter=""))
        watched_list = [row["movie_name"] for row in reader if row["type"] == "watch"]
        csvfile.seek(0, 0)
        total_rows = len(list(reader))
        csvfile.seek(0, 0)

        # Ignore the header row
        next(reader, None)
        for rows_count, row in enumerate(reader):
            movie = TVTimeMovie(row)
            MovieProcessor(watched_list).process_item(movie, rows_count / total_rows)


def menu_selection() -> int:
    # Display a menu selection
    print(">> What do you want to do?")
    print("    1) Import Watch History for TV Shows from TV Time")
    print("    2) Import Watch Movies from TV Time")
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
        process_watched_shows()
        # TODO: Add support for followed shows
    elif selection == 2:
        logging.info("Processing movies.")
        process_watched_movies()
    elif selection == 3:
        logging.info("Processing both watched shows and movies.")
        process_watched_shows()
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
