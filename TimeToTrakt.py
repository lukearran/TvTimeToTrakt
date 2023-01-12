#!/usr/bin/env python3
import csv
import json
import logging
import os
import re
import sys
import time
from dataclasses import dataclass
from datetime import datetime

import trakt.core
from tinydb import Query, TinyDB
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
DELAY_BETWEEN_EPISODES_IN_SECONDS = 1

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


def is_authenticated():
    with open("pytrakt.json") as f:
        data = json.load(f)
        days_before_expiration = (
                datetime.fromtimestamp(data["OAUTH_EXPIRES_AT"]) - datetime.now()
        ).days
        if days_before_expiration < 1:
            return False
        return True


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


def init_trakt_auth():
    if is_authenticated():
        return True
    # Set the method of authentication
    trakt.core.AUTH_METHOD = trakt.core.OAUTH_AUTH
    return init(
        config.trakt_username,
        store=True,
        client_id=config.client_id,
        client_secret=config.client_secret,
    )


# With a given title, check if it contains a year (e.g Doctor Who (2005))
# and then return this value, with the title and year removed to improve
# the accuracy of Trakt results.

@dataclass
class Title:
    full_title: str
    without_year: str
    year: int
    titleWithoutYear: str
    yearValue: int

    def __init__(self, title: str):
        try:
            # Use a regex expression to get the value within the brackets e.g. The Americans (2017)
            year_search = re.search(r"\(([A-Za-z0-9_]+)\)", title)
            year_value = year_search.group(1)
            # Then, get the title without the year value included
            title_value = title.split("(")[0].strip()
            # Put this together into an object
            self.full_title = title
            self.without_year = title_value
            self.titleWithoutYear = title_value
            self.year = int(year_value)
            self.yearValue = int(year_value)
        except Exception:
            # If the above failed, then the title doesn't include a year
            # so return the object as is.
            self.full_title = title
            self.without_year = title
            self.titleWithoutYear = title
            self.year = -1
            self.yearValue = -1


def get_year_from_title(title) -> Title:
    return Title(title)


# Shows in TV Time are often different to Trakt.TV - in order to improve results and automation,
# calculate how many words are in the title, and return true if more than 50% of the title is a match,
# It seems to improve automation, and reduce manual selection....


def check_title_name_match(tv_time_title, trakt_title):
    # If the name is a complete match, then don't bother comparing them!
    if tv_time_title == trakt_title:
        return True

    # Split the TvTime title
    tv_time_title_split = tv_time_title.split()

    # Create an array of words which are found in the Trakt title
    words_matched = []

    # Go through each word of the TV Time title, and check if it's in the Trakt title
    for word in tv_time_title_split:
        if word in trakt_title:
            words_matched.append(word)

    # Then calculate what percentage of words matched
    quotient = len(words_matched) / len(trakt_title.split())
    percentage = quotient * 100

    # If more than 50% of words in the TV Time title exist in the Trakt title,
    # then return the title as a possibility to use
    return percentage > 50


# Using TV Time data (Name of Show, Season No and Episode) - find the corresponding show
# in Trakt.TV either by automation, or asking the user to confirm.


def get_show_by_name(name, season_number, episode_number):
    # Parse the TV Show's name for year, if one is present in the string
    title_obj = get_year_from_title(name)

    # Create a boolean to indicate if the title contains a year,
    # this is used later on to improve the accuracy of picking
    # from search results
    does_title_include_year = title_obj.yearValue != -1

    # If the title contains a year, then replace the local variable with the stripped version
    if does_title_include_year:
        name = title_obj.titleWithoutYear

    # Request the Trakt API for search results, using the name
    tv_search = TVShow.search(name)

    # Create an array of shows which have been matched
    shows_with_same_name = []

    # Go through each result from the search
    for show in tv_search:
        # Check if the title is a match, based on our conditions (e.g over 50% of words match)
        if check_title_name_match(name, show.title):
            # If the title included the year of broadcast, then we can be more picky in the results
            # to look for a show with a broadcast year that matches
            if does_title_include_year:
                # If the show title is a 1:1 match, with the same broadcast year, then bingo!
                if (name == show.title) and (show.year == title_obj.yearValue):
                    # Clear previous results, and only use this one
                    shows_with_same_name = [show]
                    break

                # Otherwise, only add the show if the broadcast year matches
                if show.year == title_obj.yearValue:
                    shows_with_same_name.append(show)
            # If the program doesn't have the broadcast year, then add all the results
            else:
                shows_with_same_name.append(show)

    # Sweep through the results once more for 1:1 title name matches,
    # then if the list contains one entry with a 1:1 match, then clear the array
    # and only use this one!
    complete_match_names = []
    for nameFromSearch in shows_with_same_name:
        if nameFromSearch.title == name:
            complete_match_names.append(nameFromSearch)

    if len(complete_match_names) == 1:
        shows_with_same_name = complete_match_names

    # If the search contains multiple results, then we need to confirm with the user which show
    # the script should use, or access the local database to see if the user has already provided
    # a manual selection
    if len(shows_with_same_name) > 1:

        # Query the local database for existing selection
        user_matched_query = Query()
        query_result = userMatchedShowsTable.search(user_matched_query.ShowName == name)

        # If the local database already contains an entry for a manual selection
        # then don't bother prompting the user to select it again!
        if len(query_result) == 1:
            # Get the first result from the query
            first_match = query_result[0]
            # Get the value contains the selection index
            first_match_selected_index = int(first_match.get("UserSelectedIndex"))
            # Check if the user previously requested to skip the show
            skip_show = first_match.get("SkipShow")
            # If the user did not skip, but provided an index selection, get the
            # matching show
            if not skip_show:
                return shows_with_same_name[first_match_selected_index]
            # Otherwise, return None, which will trigger the script to skip
            # and move onto the next show
            else:
                return None
        # If the user has not provided a manual selection already in the process
        # then prompt the user to make a selection
        else:
            print(
                f"INFO - MANUAL INPUT REQUIRED: The TV Time data for Show '{name}' (Season {season_number},"
                f"Episode {episode_number}) has {len(shows_with_same_name)} matching Trakt shows with the same name.\a "
            )

            # Output each show for manual selection
            for idx, item in enumerate(shows_with_same_name):
                # Display the show's title, broadcast year, amount of seasons and a link to the Trakt page.
                # This will provide the user with enough information to make a selection.
                print(
                    f"    ({idx + 1}) {item.title} - {item.year} - {len(item.seasons)} "
                    f"Season(s) - More Info: https://trakt.tv/{item.ext}"
                )

            while True:
                try:
                    # Get the user's selection, either a numerical input, or a string 'SKIP' value
                    index_selected = input(
                        "Please make a selection from above (or enter SKIP):"
                    )

                    if index_selected != "SKIP":
                        # Since the value isn't 'skip', check that the result is numerical
                        index_selected = int(index_selected) - 1
                        # Exit the selection loop
                        break
                    # Otherwise, exit the loop
                    else:
                        break
                # Still allow the user to provide the exit input, and kill the program
                except KeyboardInterrupt:
                    sys.exit("Cancel requested...")
                # Otherwise, the user has entered an invalid value, warn the user to try again
                except Exception:
                    logging.error(
                        f"Sorry! Please select a value between 0 to {len(shows_with_same_name)}"
                    )

            # If the user entered 'SKIP', then exit from the loop with no selection, which
            # will trigger the program to move onto the next episode
            if index_selected == "SKIP":
                # Record that the user has skipped the TV Show for import, so that
                # manual input isn't required everytime
                userMatchedShowsTable.insert(
                    {"ShowName": name, "UserSelectedIndex": 0, "SkipShow": True}
                )

                return None
            # Otherwise, return the selection which the user made from the list
            else:
                selected_show = shows_with_same_name[int(index_selected)]

                userMatchedShowsTable.insert(
                    {
                        "ShowName": name,
                        "UserSelectedIndex": index_selected,
                        "SkipShow": False,
                    }
                )

                return selected_show

    else:
        if len(shows_with_same_name) > 0:
            # If the search returned only one result, then awesome!
            # Return the show, so the import automation can continue.
            return shows_with_same_name[0]
        else:
            return None


# Since the Trakt.Py starts the indexing of seasons in the array from 0 (e.g Season 1 in Index 0), then
# subtract the TV Time numerical value by 1 so it starts from 0 as well. However, when a TV series includes
# a 'special' season, Trakt.Py will place this as the first season in the array - so, don't subtract, since
# this will match TV Time's existing value.


def parse_season_number(season_number, trakt_show_obj):
    # Parse the season number into a numerical value
    season_number = int(season_number)

    # Then get the Season Number from the first item in the array
    first_season_no = trakt_show_obj.seasons[0].number

    # If the season number is 0, then the Trakt show contains a "special" season
    if first_season_no == 0:
        # No need to modify the value, as the TV Time value will match Trakt
        return season_number
    # Otherwise, if the Trakt seasons start with no specials, then return the seasonNo,
    # but subtracted by one (e.g Season 1 in TV Time, will be 0)
    else:
        # Only subtract if the TV Time season number is greater than 0.
        if season_number != 0:
            return season_number - 1
        # Otherwise, the TV Time season is a special! Then you don't need to change the starting position
        else:
            return season_number


def process_watched_shows():
    # Total amount of rows in the CSV file
    error_streak = 0
    # Open the CSV file within the GDPR exported data
    with open(WATCHED_SHOWS_PATH, newline="") as csvfile:
        # Create the CSV reader, which will break up the fields using the delimiter ','
        shows_reader = csv.DictReader(csvfile, delimiter=",")
        # Get the total amount of rows in the CSV file,
        rows_total = len(list(shows_reader))
        # Move position to the beginning of the file
        csvfile.seek(0, 0)
        # Loop through each line/record of the CSV file
        # Ignore the header row
        next(shows_reader, None)
        for rowsCount, row in enumerate(shows_reader):
            # Get the name of the TV show
            tv_show_name = row["tv_show_name"]
            # Get the TV Time Episode id
            tv_show_episode_id = row["episode_id"]
            # Get the TV Time Season Number
            tv_show_season_no = row["episode_season_number"]
            # Get the TV Time Episode Number
            tv_show_episode_no = row["episode_number"]
            # Get the date which the show was marked 'watched' in TV Time
            tv_show_date_watched = row["updated_at"]
            # Parse the watched date value into a Python type
            tv_show_date_watched_converted = datetime.strptime(
                tv_show_date_watched, "%Y-%m-%d %H:%M:%S"
            )

            # Query the local database for previous entries indicating that
            # the episode has already been imported in the past. Which will
            # ease pressure on TV Time's API server during a retry of the import
            # process, and just save time overall without needing to create network requests
            episode_completed_query = Query()
            query_result = syncedEpisodesTable.search(
                episode_completed_query.episodeId == tv_show_episode_id
            )

            # If the query returned no results, then continue to import it into Trakt
            if len(query_result) == 0:
                # Create a repeating loop, which will break on success, but repeats on failures
                while True:
                    # If more than 10 errors occurred in one streak, whilst trying to import the episode
                    # then give up, and move onto the next episode, but warn the user.
                    if error_streak > 10:
                        logging.warning(
                            "An error occurred 10 times in a row... skipping episode..."
                        )
                        break
                    try:
                        # Sleep for a second between each process, before going onto the next watched episode.
                        # This is required to remain within the API rate limit, and use the API server fairly.
                        # Other developers share the service, for free - so be considerate of your usage.
                        time.sleep(DELAY_BETWEEN_EPISODES_IN_SECONDS)
                        # Search Trakt for the TV show matching TV Time's title value
                        trakt_show_obj = get_show_by_name(
                            tv_show_name, tv_show_season_no, tv_show_episode_no
                        )
                        # If the method returned 'None', then this is an indication to skip the episode, and
                        # move onto the next one
                        if trakt_show_obj is None:
                            break
                        # Show the progress of the import on-screen
                        logging.info(
                            f"({rowsCount + 1}/{rows_total}) - Processing '{tv_show_name}' Season {tv_show_season_no} /"
                            f"Episode {tv_show_episode_no}"
                        )
                        # Get the season from the Trakt API
                        season = trakt_show_obj.seasons[
                            parse_season_number(tv_show_season_no, trakt_show_obj)
                        ]
                        # Get the episode from the season
                        episode = season.episodes[int(tv_show_episode_no) - 1]
                        # Mark the episode as watched!
                        episode.mark_as_seen(tv_show_date_watched_converted)
                        # Add the episode to the local database as imported, so it can be skipped,
                        # if the process is repeated
                        syncedEpisodesTable.insert({"episodeId": tv_show_episode_id})
                        # Clear the error streak on completing the method without errors
                        error_streak = 0
                        break
                    # Catch errors which occur because of an incorrect array index. This occurs when
                    # an incorrect Trakt show has been selected, with season/episodes which don't match TV Time.
                    # It can also occur due to a bug in Trakt Py, whereby some seasons contain an empty array of episodes.
                    except IndexError:
                        tv_show_slug = trakt_show_obj.to_json()["shows"][0]["ids"]["ids"][
                            "slug"
                        ]
                        logging.warning(
                            f"({rowsCount}/{rows_total}) - {tv_show_name} Season {tv_show_season_no}, "
                            f"Episode {tv_show_episode_no} does not exist in Trakt! "
                            f"(https://trakt.tv/shows/{tv_show_slug}/seasons/{tv_show_season_no}/episodes/{tv_show_episode_no})"
                        )
                        break
                    # Catch any errors which are raised because a show could not be found in Trakt
                    except trakt.errors.NotFoundException:
                        logging.warning(
                            f"({rowsCount}/{rows_total}) - {tv_show_name} Season {tv_show_season_no}, "
                            f"Episode {tv_show_episode_no} does not exist (search) in Trakt!"
                        )
                        break
                    # Catch errors because of the program breaching the Trakt API rate limit
                    except trakt.errors.RateLimitException:
                        logging.warning(
                            "The program is running too quickly and has hit Trakt's API rate limit! Please increase the delay between "
                            + "episdoes via the variable 'DELAY_BETWEEN_EPISODES_IN_SECONDS'. The program will now wait 60 seconds before "
                            + "trying again."
                        )
                        time.sleep(60)

                        # Mark the exception in the error streak
                        error_streak += 1
                    # Catch a JSON decode error - this can be raised when the API server is down and produces a HTML page, instead of JSON
                    except json.decoder.JSONDecodeError:
                        logging.warning(
                            f"({rowsCount}/{rows_total}) - A JSON decode error occuring whilst processing {tv_show_name} "
                            + f"Season {tv_show_season_no}, Episode {tv_show_episode_no}! This might occur when the server is down and has produced "
                            + "a HTML document instead of JSON. The script will wait 60 seconds before trying again."
                        )

                        # Wait 60 seconds
                        time.sleep(60)

                        # Mark the exception in the error streak
                        error_streak += 1
                    # Catch a CTRL + C keyboard input, and exits the program
                    except KeyboardInterrupt:
                        sys.exit("Cancel requested...")
            # Skip the episode
            else:
                logging.info(
                    f"({rowsCount}/{rows_total}) - Already imported, skipping '{tv_show_name}' Season {tv_show_season_no} / Episode {tv_show_episode_no}."
                )


# Using TV Time data (Name of Movie) - find the corresponding movie
# in Trakt.TV either by automation, or asking the user to confirm.


def get_movie_by_name(name):
    # Parse the Movie's name for year, if one is present in the string
    title_obj = get_year_from_title(name)

    # Create a boolean to indicate if the title contains a year,
    # this is used later on to improve the accuracy of picking
    # from search results
    does_title_include_year = title_obj.yearValue != -1

    # If the title contains a year, then replace the local variable with the stripped version
    if does_title_include_year:
        name = title_obj.titleWithoutYear

    # Request the Trakt API for search results, using the name
    movie_search = Movie.search(name)

    # Create an array of movies which have been matched
    movies_with_same_name = []

    # Go through each result from the search
    for movie in movie_search:
        # Check if the title is a match, based on our conditions (e.g over 50% of words match)
        if check_title_name_match(name, movie.title):
            # If the title included the year of broadcast, then we can be more picky in the results
            # to look for a movie with a broadcast year that matches
            if does_title_include_year:
                # If the movie title is a 1:1 match, with the same broadcast year, then bingo!
                if (name == movie.title) and (movie.year == title_obj.yearValue):
                    # Clear previous results, and only use this one
                    movies_with_same_name = [movie]
                    break

                # Otherwise, only add the movie if the broadcast year matches
                if movie.year == title_obj.yearValue:
                    movies_with_same_name.append(movie)
            # If the program doesn't have the broadcast year, then add all the results
            else:
                movies_with_same_name.append(movie)

    # Sweep through the results once more for 1:1 title name matches,
    # then if the list contains one entry with a 1:1 match, then clear the array
    # and only use this one!
    complete_match_names = []
    for nameFromSearch in movies_with_same_name:
        if nameFromSearch.title == name:
            complete_match_names.append(nameFromSearch)

    if len(complete_match_names) == 1:
        movies_with_same_name = complete_match_names

    # If the search contains multiple results, then we need to confirm with the user which movie
    # the script should use, or access the local database to see if the user has already provided
    # a manual selection
    if len(movies_with_same_name) > 1:

        # Query the local database for existing selection
        user_matched_query = Query()
        query_result = userMatchedMoviesTable.search(user_matched_query.movie_name == name)

        # If the local database already contains an entry for a manual selection
        # then don't bother prompting the user to select it again!
        if len(query_result) == 1:
            # Get the first result from the query
            first_match = query_result[0]
            # Get the value contains the selection index
            first_match_selected_index = int(first_match.get("UserSelectedIndex"))
            # Check if the user previously requested to skip the movie
            skip_movie = first_match.get("SkipMovie")
            # If the user did not skip, but provided an index selection, get the
            # matching movie
            if not skip_movie:
                return movies_with_same_name[first_match_selected_index]
            # Otherwise, return None, which will trigger the script to skip
            # and move onto the next movie
            else:
                return None
        # If the user has not provided a manual selection already in the process
        # then prompt the user to make a selection
        else:
            print(
                f"INFO - MANUAL INPUT REQUIRED: The TV Time data for Movie '{name}' has {len(movies_with_same_name)} "
                f"matching Trakt movies with the same name.\a"
            )

            # Output each movie for manual selection
            for idx, item in enumerate(movies_with_same_name):
                # Display the movie's title, broadcast year, amount of seasons and a link to the Trakt page.
                # This will provide the user with enough information to make a selection.
                print(
                    f"    ({idx + 1}) {item.title} - {item.year} - More Info: https://trakt.tv/{item.ext}"
                )

            while True:
                try:
                    # Get the user's selection, either a numerical input, or a string 'SKIP' value
                    index_selected = input(
                        "Please make a selection from above (or enter SKIP):"
                    )

                    if index_selected != "SKIP":
                        # Since the value isn't 'skip', check that the result is numerical
                        index_selected = int(index_selected) - 1
                        # Exit the selection loop
                        break
                    # Otherwise, exit the loop
                    else:
                        break
                # Still allow the user to provide the exit input, and kill the program
                except KeyboardInterrupt:
                    sys.exit("Cancel requested...")
                # Otherwise, the user has entered an invalid value, warn the user to try again
                except Exception:
                    logging.error(
                        f"Sorry! Please select a value between 0 to {len(movies_with_same_name)}"
                    )

            # If the user entered 'SKIP', then exit from the loop with no selection, which
            # will trigger the program to move onto the next episode
            if index_selected == "SKIP":
                # Record that the user has skipped the Movie for import, so that
                # manual input isn't required everytime
                userMatchedMoviesTable.insert(
                    {"movie_name": name, "UserSelectedIndex": 0, "SkipMovie": True}
                )

                return None
            # Otherwise, return the selection which the user made from the list
            else:
                selected_movie = movies_with_same_name[int(index_selected)]

                userMatchedMoviesTable.insert(
                    {
                        "movie_name": name,
                        "UserSelectedIndex": index_selected,
                        "SkipMovie": False,
                    }
                )

                return selected_movie

    else:
        if len(movies_with_same_name) > 0:
            # If the search returned only one result, then awesome!
            # Return the movie, so the import automation can continue.
            return movies_with_same_name[0]
        else:
            return None


def process_movies():
    # Total amount of rows which have been processed in the CSV file
    # Total amount of rows in the CSV file
    error_streak = 0
    # Open the CSV file within the GDPR exported data
    with open(MOVIES_PATH, newline="") as csvfile:
        # Create the CSV reader, which will break up the fields using the delimiter ','
        movie_reader_temp = csv.DictReader(csvfile, delimiter=",")
        movie_reader = filter(lambda p: "" != p["movie_name"], movie_reader_temp)
        # First, list all movies with watched type so that watchlist entry for them is not created
        watched_list = []
        for row in movie_reader:
            if row["type"] == "watch":
                watched_list.append(row["movie_name"])
        # Move position to the beginning of the file
        csvfile.seek(0, 0)
        # Get the total amount of rows in the CSV file,
        rows_total = len(list(movie_reader))
        # Move position to the beginning of the file
        csvfile.seek(0, 0)
        # Loop through each line/record of the CSV file
        # Ignore the header row
        next(movie_reader, None)
        for rows_count, row in enumerate(movie_reader):
            # Get the name of the Movie
            movie_name = row["movie_name"]
            # Get the date which the movie was marked 'watched' in TV Time
            activity_type = row["type"]
            movie_date_watched = row["updated_at"]
            # Parse the watched date value into a Python type
            movie_date_watched_converted = datetime.strptime(
                movie_date_watched, "%Y-%m-%d %H:%M:%S"
            )

            # Query the local database for previous entries indicating that
            # the episode has already been imported in the past. Which will
            # ease pressure on TV Time's API server during a retry of the import
            # process, and just save time overall without needing to create network requests
            movie_query = Query()
            query_result = syncedMoviesTable.search(
                (movie_query.movie_name == movie_name) & (movie_query.type == "watched")
            )

            watchlist_query = Query()
            query_result_watchlist = syncedMoviesTable.search(
                (watchlist_query.movie_name == movie_name)
                & (watchlist_query.type == "watchlist")
            )

            # If the query returned no results, then continue to import it into Trakt
            if len(query_result) == 0:
                # Create a repeating loop, which will break on success, but repeats on failures
                while True:
                    # If movie is watched but this is an entry for watchlist, then skip
                    if movie_name in watched_list and activity_type != "watch":
                        logging.info(
                            f"Skipping '{movie_name}' to avoid redundant watchlist entry."
                        )
                        break
                    # If more than 10 errors occurred in one streak, whilst trying to import the episode
                    # then give up, and move onto the next episode, but warn the user.
                    if error_streak > 10:
                        logging.warning(
                            "An error occurred 10 times in a row... skipping episode..."
                        )
                        break
                    try:
                        # Sleep for a second between each process, before going onto the next watched episode.
                        # This is required to remain within the API rate limit, and use the API server fairly.
                        # Other developers share the service, for free - so be considerate of your usage.
                        time.sleep(DELAY_BETWEEN_EPISODES_IN_SECONDS)
                        # Search Trakt for the Movie matching TV Time's title value
                        trakt_movie_obj = get_movie_by_name(movie_name)
                        # If the method returned 'None', then this is an indication to skip the episode, and
                        # move onto the next one
                        if trakt_movie_obj is None:
                            break
                        # Show the progress of the import on-screen
                        logging.info(
                            f"({rows_count + 1}/{rows_total}) - Processing '{movie_name}'"
                        )
                        if activity_type == "watch":
                            trakt_movie_obj.mark_as_seen(movie_date_watched_converted)
                            # Add the episode to the local database as imported, so it can be skipped,
                            # if the process is repeated
                            syncedMoviesTable.insert(
                                {"movie_name": movie_name, "type": "watched"}
                            )
                            logging.info(f"Marked as seen")
                        elif len(query_result_watchlist) == 0:
                            trakt_movie_obj.add_to_watchlist()
                            # Add the episode to the local database as imported, so it can be skipped,
                            # if the process is repeated
                            syncedMoviesTable.insert(
                                {"movie_name": movie_name, "type": "watchlist"}
                            )
                            logging.info(f"Added to watchlist")
                        else:
                            logging.warning(f"Already in watchlist")
                        # Clear the error streak on completing the method without errors
                        error_streak = 0
                        break
                    # Catch errors which occur because of an incorrect array index. This occurs when
                    # an incorrect Trakt movie has been selected, with season/episodes which don't match TV Time.
                    # It can also occur due to a bug in Trakt Py, whereby some seasons contain an empty array of episodes.
                    except IndexError:
                        movie_slug = trakt_movie_obj.to_json()["movies"][0]["ids"]["ids"][
                            "slug"
                        ]
                        logging.warning(
                            f"({rows_count}/{rows_total}) - {movie_name} "
                            f"does not exist in Trakt! (https://trakt.tv/movies/{movie_slug}/)"
                        )
                        break
                    # Catch any errors which are raised because a movie could not be found in Trakt
                    except trakt.errors.NotFoundException:
                        logging.warning(
                            f"({rows_count}/{rows_total}) - {movie_name} does not exist (search) in Trakt!"
                        )
                        break
                    # Catch errors because of the program breaching the Trakt API rate limit
                    except trakt.errors.RateLimitException:
                        logging.warning(
                            "The program is running too quickly and has hit Trakt's API rate limit! Please increase the delay between "
                            + "movies via the variable 'DELAY_BETWEEN_EPISODES_IN_SECONDS'. The program will now wait 60 seconds before "
                            + "trying again."
                        )
                        time.sleep(60)

                        # Mark the exception in the error streak
                        error_streak += 1
                    # Catch a JSON decode error - this can be raised when the API server is down and produces a HTML page, instead of JSON
                    except json.decoder.JSONDecodeError:
                        logging.warning(
                            f"({rows_count}/{rows_total}) - A JSON decode error occuring whilst processing {movie_name} "
                            + f" This might occur when the server is down and has produced "
                            + "a HTML document instead of JSON. The script will wait 60 seconds before trying again."
                        )

                        # Wait 60 seconds
                        time.sleep(60)

                        # Mark the exception in the error streak
                        error_streak += 1
                    # Catch a CTRL + C keyboard input, and exits the program
                    except KeyboardInterrupt:
                        sys.exit("Cancel requested...")

            # Skip the episode
            else:
                logging.info(
                    f"({rows_count}/{rows_total}) - Already imported, skipping '{movie_name}'."
                )


def start():
    # Display a menu selection
    print(">> What do you want to do?")
    print("    1) Import Watch History for TV Shows from TV Time")
    print("    2) Import Watch Movies from TV Time")
    print("    3) Do both 1 and 2")
    print("    4) Exit")

    while True:
        try:
            menu_selection = input("Enter your menu selection: ")
            menu_selection = 3 if not menu_selection else int(menu_selection)
            break
        except ValueError:
            logging.warning("Invalid input. Please enter a numerical number.")
        # Check if the input is valid
    if not 1 <= menu_selection <= 4:
        logging.warning("Sorry - that's an unknown menu selection")
        exit()
    # Exit if the 4th option was chosen
    if menu_selection == 4:
        logging.info("Exiting as per user's selection.")
        exit()
    # Create the initial authentication with Trakt, before starting the process
    if init_trakt_auth():
        # Start the process which is required
        if menu_selection == 1:
            # Invoke the method which will import episodes which have been watched
            # from TV Time into Trakt
            logging.info("Processing watched shows.")
            process_watched_shows()
            # TODO: Add support for followed shows
        elif menu_selection == 2:
            # Invoke the method which will import movies which have been watched
            # from TV Time into Trakt
            logging.info("Processing movies.")
            process_movies()
        elif menu_selection == 3:
            # Invoke both the episodes and movies import methods
            logging.info("Processing both watched shows and movies.")
            process_watched_shows()
            process_movies()
    else:
        logging.error(
            "ERROR: Unable to complete authentication to Trakt - please try again."
        )


if __name__ == "__main__":
    # Check that the user has provided the GDPR path
    if os.path.isdir(config.gdpr_workspace_path):
        start()
    else:
        logging.error(
            "Oops! The TV Time GDPR folder '"
            + config.gdpr_workspace_path
            + "' does not exist on the local system. Please check it, and try again."
        )
