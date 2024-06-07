import logging
import re
import sys
from abc import ABC, abstractmethod
from dataclasses import dataclass
from datetime import datetime
from typing import Optional, TypeVar, Union, Any

from tinydb import Query
from tinydb.table import Table
from trakt.movies import Movie
from trakt.tv import TVShow

from database import userMatchedShowsTable, userMatchedMoviesTable

TraktTVShow = TypeVar("TraktTVShow")
TraktMovie = TypeVar("TraktMovie")
TraktItem = Union[TraktTVShow, TraktMovie]


@dataclass
class Title:
    name: str
    without_year: str
    year: Optional[int]

    def __init__(self, title: str, year: Optional[int] = None):
        """
        Creates a Title object. If year is not passed, it tries to parse it from the title.
        """
        self.name = title
        if year is not None:
            self.without_year = title
            self.year = year
        else:
            try:
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

    def items_with_same_name(self, items: list[TraktItem]) -> list[TraktItem]:
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
        self.title = Title(name)
        # Get the date which the show was marked 'watched' in TV Time
        # and parse the watched date value into a Python object
        self.date_watched = datetime.strptime(
            updated_at, "%Y-%m-%d %H:%M:%S"
        )


class TVTimeTVShow(TVTimeItem):
    def __init__(self, row: Any):
        super().__init__(row["series_name"], row["created_at"])
        self.episode_id = row["episode_id"]
        self.season_number = row["season_number"]
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
        first_season_no = trakt_show.seasons[0].season

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

        # Release date is available for movies
        if row["release_date"][0:4] == "0000":  # some entries had a release date of 0000
            return

        release_date = datetime.strptime(
            row["release_date"], "%Y-%m-%d %H:%M:%S"
        )

        # Check that date is valid
        if release_date.year > 1800:
            self.title = Title(self.title.name, release_date.year)


class Searcher(ABC):
    def __init__(self, user_matched_table: Table):
        self.name = ""
        self.items_with_same_name: Optional[TraktItem] = None
        self._user_matched_table = user_matched_table

    def search(self, title: Title) -> Optional[TraktItem]:
        self.name = title.name
        # If the title contains a year, then replace the local variable with the stripped version.
        if title.year:
            self.name = title.without_year
        self.items_with_same_name = title.items_with_same_name(self.search_trakt(self.name))

        single_result = self._check_single_result()
        if single_result:
            return single_result
        elif len(self.items_with_same_name) < 1:
            return None

        # If the search contains multiple results, then we need to confirm with the user which show
        # the script should use, or access the local database to see if the user has already provided
        # a manual selection

        should_return, query_result = self._search_local()
        if should_return:
            return query_result
        # If the user has not provided a manual selection already in the process
        # then prompt the user to make a selection
        else:
            return self._handle_multiple_manually()

    @abstractmethod
    def search_trakt(self, name: str) -> list[TraktItem]:
        pass

    @abstractmethod
    def _print_manual_selection(self):
        pass

    def _search_local(self) -> tuple[bool, TraktItem]:
        user_matched_query = Query()
        query_result = self._user_matched_table.search(user_matched_query.Name == self.name)
        # If the local database already contains an entry for a manual selection
        # then don't bother prompting the user to select it again!
        if len(query_result) == 1:
            first_match = query_result[0]
            first_match_selected_index = int(first_match.get("UserSelectedIndex"))
            skip_show = first_match.get("Skip")
            if skip_show:
                return True, None
            else:
                return True, self.items_with_same_name[first_match_selected_index]
        else:
            return False, None

    def _handle_multiple_manually(self) -> Optional[TraktItem]:
        self._print_manual_selection()
        while True:
            try:
                # Get the user's selection, either a numerical input, or a string 'SKIP' value
                index_selected = input("Please make a selection from above (or enter SKIP): ")
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

    def _check_single_result(self) -> Optional[TraktItem]:
        complete_match_names = [name_from_search for name_from_search in self.items_with_same_name if
                                name_from_search.title == self.name]
        if len(complete_match_names) == 1:
            return complete_match_names[0]
        elif len(self.items_with_same_name) == 1:
            return self.items_with_same_name[0]


class TVShowSearcher(Searcher):
    def __init__(self, tv_show: TVTimeTVShow):
        super().__init__(userMatchedShowsTable)
        self.tv_show = tv_show

    def search_trakt(self, name: str) -> list[TraktItem]:
        return TVShow.search(name)

    def _print_manual_selection(self) -> None:
        print(
            f"INFO - MANUAL INPUT REQUIRED: The TV Time data for Show '{self.name}'"
            f" (Season {self.tv_show.season_number}, Episode {self.tv_show.episode_number}) has"
            f" {len(self.items_with_same_name)} matching Trakt shows with the same name.\a"
        )

        for idx, item in enumerate(self.items_with_same_name):
            print(
                f"({idx + 1}) {item.title} - {item.year} - {len(item.seasons)}"
                f" Season(s) - More Info: https://trakt.tv/{item.ext}"
            )


class MovieSearcher(Searcher):
    def __init__(self):
        super().__init__(userMatchedMoviesTable)

    def search_trakt(self, name: str) -> list[TraktItem]:
        return Movie.search(name)

    def _print_manual_selection(self) -> None:
        print(
            f"INFO - MANUAL INPUT REQUIRED: The TV Time data for Movie '{self.name}'"
            f" has {len(self.items_with_same_name)}"
            f" matching Trakt movies with the same name.\a"
        )

        for idx, item in enumerate(self.items_with_same_name):
            print(f"({idx + 1}) {item.title} - {item.year} - More Info: https://trakt.tv/{item.ext}")
