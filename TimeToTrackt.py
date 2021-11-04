# main.py
from logging import error
import sys
from trakt import init
from trakt.movies import get_recommended_movies
import trakt.core
import os
from trakt.tv import TVShow
import csv
from datetime import datetime
import time
from tinydb import TinyDB, Query
import json
import re
import sys

# Adjust this value to increase/decrease your requests between episodes.
# Make sure it's above 1 seconds to remain within the rate limit.
DELAY_BETWEEN_EPISODES_IN_SECONDS = 5

# Create a database to keep track of completed processes
database = TinyDB('localStorage.json')
syncedEpisodesTable = database.table('SyncedEpisodes')
userMatchedShowsTable = database.table('TvTimeTraktUserMatched')


class Expando(object):
    pass


def getConfiguration():
    configEx = Expando()

    with open('config.json') as f:
        data = json.load(f)

    configEx.TRAKT_USERNAME = data["TRAKT_USERNAME"]
    configEx.CLIENT_ID = data["CLIENT_ID"]
    configEx.CLIENT_SECRET = data["CLIENT_SECRET"]
    configEx.GDPR_WORKSPACE_PATH = data["GDPR_WORKSPACE_PATH"]

    CONFIG_SINGLETON = configEx

    return CONFIG_SINGLETON


config = getConfiguration()

# Return the path to the CSV file contain the watched episode data from TV Time


def getWatchedShowsPath():
    return config.GDPR_WORKSPACE_PATH + "/seen_episode.csv"


def getFollowedShowsPath():
    return config.GDPR_WORKSPACE_PATH + "/followed_tv_show.csv"


def initTraktAuth():
    # Set the method of authentication
    trakt.core.AUTH_METHOD = trakt.core.OAUTH_AUTH
    return init(config.TRAKT_USERNAME, store=True, client_id=config.CLIENT_ID, client_secret=config.CLIENT_SECRET)

# With a given title, check if it contains a year (e.g Doctor Who (2005))
# and then return this value, with the title and year removed to improve
# the accuracy of Trakt results.


def getYearFromTitle(title):
    ex = Expando()

    try:
        # Use a regex expression to get the value within the brackets e.g The Americans (2017)
        yearSearch = re.search(r"\(([A-Za-z0-9_]+)\)", title)
        yearValue = yearSearch.group(1)
        # Then, get the title without the year value included
        titleValue = title.split('(')[0].strip()
        # Put this together into an object
        ex.titleWithoutYear = titleValue
        ex.yearValue = int(yearValue)
        return ex
    except:
        # If the above failed, then the title doesn't include a year
        # so return the object as is.
        ex.titleWithoutYear = title
        ex.yearValue = -1
        return ex

# Shows in TV Time are often different to Trakt.TV - in order to improve results and automation,
# calculate how many words are in the title, and return true if more than 50% of the title is a match,
# It seems to improve automation, and reduce manual selection....


def checkTitleNameMatch(tvTimeTitle, traktTitle):
    # If the name is a complete match, then don't bother comparing them!
    if tvTimeTitle == traktTitle:
        return True

    # Split the TvTime title
    tvTimeTitleSplit = tvTimeTitle.split()

    # Create an array of words which are found in the Trakt title
    wordsMatched = []

    # Go through each word of the TV Time title, and check if it's in the Trakt title
    for word in tvTimeTitleSplit:
        if word in traktTitle:
            wordsMatched.append(word)

    # Then calculate what percentage of words matched
    quotient = len(wordsMatched) / len(traktTitle.split())
    percentage = quotient * 100

    # If more than 50% of words in the TV Time title exist in the Trakt title,
    # then return the title as a possibility to use
    return percentage > 50

# Using TV Time data (Name of Show, Season No and Episode) - find the corresponding show
# in Trakt.TV either by automation, or asking the user to confirm.


def getShowByName(name, seasonNo, episodeNo):
    # Parse the TV Show's name for year, if one is present in the string
    titleObj = getYearFromTitle(name)

    # Create a boolean to indicate if the title contains a year,
    # this is used later on to improve the accuracy of picking
    # from search results
    doesTitleIncludeYear = titleObj.yearValue != -1

    # If the title contains a year, then replace the local variable with the stripped version
    if doesTitleIncludeYear:
        name = titleObj.titleWithoutYear

    # Request the Trakt API for search results, using the name
    tvSearch = TVShow.search(name)

    # Create an array of shows which have been matched
    showsWithSameName = []

    # Go through each result from the search
    for show in tvSearch:
        # Check if the title is a match, based on our conditions (e.g over 50% of words match)
        if checkTitleNameMatch(name, show.title):
            # If the title included the year of broadcast, then we can be more picky in the results
            # to look for a show with a broadcast year that matches
            if doesTitleIncludeYear == True:
                # If the show title is a 1:1 match, with the same broadcast year, then bingo!
                if (name == show.title) and (show.year == titleObj.yearValue):
                    # Clear previous results, and only use this one
                    showsWithSameName = []
                    showsWithSameName.append(show)
                    break

                # Otherwise, only add the show if the broadcast year matches
                if show.year == titleObj.yearValue:
                    showsWithSameName.append(show)
            # If the program doesn't have the broadcast year, then add all the results
            else:
                showsWithSameName.append(show)

    # Sweep through the results once more for 1:1 title name matches,
    # then if the list contains one entry with a 1:1 match, then clear the array
    # and only use this one!
    completeMatchNames = []
    for nameFromSearch in showsWithSameName:
        if nameFromSearch.title == name:
            completeMatchNames.append(nameFromSearch)

    if (len(completeMatchNames) == 1):
        showsWithSameName = completeMatchNames

    # If the search contains multiple results, then we need to confirm with the user which show
    # the script should use, or access the local database to see if the user has already provided
    # a manual selection
    if len(showsWithSameName) > 1:

        # Query the local database for existing selection
        userMatchedQuery = Query()
        queryResult = userMatchedShowsTable.search(
            userMatchedQuery.ShowName == name)

        # If the local database already contains an entry for a manual selection
        # then don't bother prompting the user to select it again!
        if len(queryResult) == 1:
            # Get the first result from the query
            firstMatch = queryResult[0]
            # Get the value contains the selection index
            firstMatchSelectedIndex = int(firstMatch.get('UserSelectedIndex'))
            # Check if the user previously requested to skip the show
            skipShow = firstMatch.get('SkipShow')
            # If the user did not skip, but provided an index selection, get the
            # matching show
            if skipShow == False:
                return showsWithSameName[firstMatchSelectedIndex]
            # Otherwise, return None, which will trigger the script to skip
            # and move onto the next show
            else:
                return None
        # If the user has not provided a manual selection already in the process
        # then prompt the user to make a selection
        else:
            print(
                f"INFO - MANUAL INPUT REQUIRED: The TV Time data for Show '{name}' (Season {seasonNo}, Episode {episodeNo}) has {len(showsWithSameName)} matching Trakt shows with the same name.")

            # Output each show for manual selection
            for idx, item in enumerate(showsWithSameName):
                # Display the show's title, broadcast year, amount of seasons and a link to the Trakt page.
                # This will provide the user with enough information to make a selection.
                print(
                    f"    ({idx + 1}) {item.title} - {item.year} - {len(item.seasons)} Season(s) - More Info: https://trakt.tv/{item.ext}")

            while(True):
                try:
                    # Get the user's selection, either a numerical input, or a string 'SKIP' value
                    indexSelected = (input(
                        f"Please make a selection from above (or enter SKIP):"))

                    if indexSelected != 'SKIP':
                        # Since the value isn't 'skip', check that the result is numerical
                        indexSelected = int(indexSelected) - 1
                        # Exit the selection loop
                        break
                    # Otherwise, exit the loop
                    else:
                        break
                # Still allow the user to provide the exit input, and kill the program
                except KeyboardInterrupt:
                    sys.exit("Cancel requested...")
                # Otherwise, the user has entered an invalid value, warn the user to try again
                except:
                    print(
                        f"Sorry! Please select a value between 0 to {len(showsWithSameName)}")

            # If the user entered 'SKIP', then exit from the loop with no selection, which
            # will trigger the program to move onto the next episode
            if (indexSelected == 'SKIP'):
                # Record that the user has skipped the TV Show for import, so that
                # manual input isn't required everytime
                userMatchedShowsTable.insert(
                    {'ShowName': name, 'UserSelectedIndex': 0, 'SkipShow': True})

                return None
            # Otherwise, return the selection which the user made from the list
            else:
                selectedShow = showsWithSameName[int(indexSelected)]

                userMatchedShowsTable.insert(
                    {'ShowName': name, 'UserSelectedIndex': indexSelected, 'SkipShow': False})

                return selectedShow

    else:
        if (len(showsWithSameName) > 0):
            # If the search returned only one result, then awesome!
            # Return the show, so the import automation can continue.
            return showsWithSameName[0]
        else:
            return None

# Since the Trakt.Py starts the indexing of seasons in the array from 0 (e.g Season 1 in Index 0), then
# subtract the TV Time numerical value by 1 so it starts from 0 as well. However, when a TV series includes
# a 'special' season, Trakt.Py will place this as the first season in the array - so, don't subtract, since
# this will match TV Time's existing value.


def parseSeasonNo(seasonNo, traktShowObj):
    # Parse the season number into a numerical value
    seasonNo = int(seasonNo)

    # Then get the Season Number from the first item in the array
    firstSeasonNo = traktShowObj.seasons[0].number

    # If the season number is 0, then the Trakt show contains a "special" season
    if firstSeasonNo == 0:
        # No need to modify the value, as the TV Time value will match Trakt
        return seasonNo
    # Otherwise, if the Trakt seasons start with no specials, then return the seasonNo,
    # but subtracted by one (e.g Season 1 in TV Time, will be 0)
    else:
        # Only subtract is the TV Time season number is greater than 0.
        if seasonNo != 0:
            return seasonNo - 1
        # Otherwise, the TV Time season is a special! Then you don't need to change the starting position
        else:
            return seasonNo


def processWatchedShows():
    # Total amount of rows which have been processed in the CSV file
    rowsCount = 0
    # Total amount of rows in the CSV file
    rowsTotal = 0
    # Total amount of errors which have occurred in one streak
    errorStreak = 0

    # Get the total amount of rows in the CSV file,
    # which is helpful for keeping track of progress.
    # However, if you have a VERY large CSV file (e.g above 100,000 rows)
    # then it might be a good idea to remove this due to the performance
    # overhead.
    with open(getWatchedShowsPath()) as f:
        rowsTotal = sum(1 for line in f)

    # Open the CSV file within the GDPR exported data
    with open(getWatchedShowsPath(), newline='') as csvfile:
        # Create the CSV reader, which will break up the fields using the delimiter ','
        showsReader = csv.reader(csvfile, delimiter=',')

        # Loop through each line/record of the CSV file
        for row in showsReader:
            # Increment the row counter to keep track of progress completing the
            # records during the import process.
            rowsCount += 1
            # Get the name of the TV show
            tvShowName = row[6]

            # Ignore the header row
            if tvShowName != "tv_show_name":
                # Get the TV Time Episode Id
                tvShowEpisodeId = row[1]
                # Get the TV Time Season Number
                tvShowSeasonNo = row[7]
                # Get the TV Time Episode Number
                tvShowEpisodeNo = row[8]
                # Get the date which the show was marked 'watched' in TV Time
                tvShowDateWatched = row[5]
                # Parse the watched date value into a Python type
                tvShowDateWatchedConverted = datetime.strptime(
                    tvShowDateWatched, '%Y-%m-%d %H:%M:%S')

                # Query the local database for previous entries indicating that
                # the episode has already been imported in the past. Which will
                # ease pressure on TV Time's API server during a retry of the import
                # process, and just save time overall without needing to create network requests
                episodeCompletedQuery = Query()
                queryResult = syncedEpisodesTable.search(
                    episodeCompletedQuery.episodeId == tvShowEpisodeId)

                # If the query returned no results, then continue to import it into Trakt
                if len(queryResult) == 0:
                    # Create a repeating loop, which will break on success, but repeats on failures
                    while True:
                        # If more than 10 errors occurred in one streak, whilst trying to import the episode
                        # then give up, and move onto the next episode, but warn the user.
                        if (errorStreak > 10):
                            print(
                                f"WARNING: An error occurred 10 times in a row... skipping episode...")
                            break
                        try:
                            # Sleep for a second between each process, before going onto the next watched episode.
                            # This is required to remain within the API rate limit, and use the API server fairly.
                            # Other developers share the service, for free - so be considerate of your usage.
                            time.sleep(DELAY_BETWEEN_EPISODES_IN_SECONDS)
                            # Search Trakt for the TV show matching TV Time's title value
                            traktShowObj = getShowByName(
                                tvShowName, tvShowSeasonNo, tvShowEpisodeNo)
                            # If the method returned 'None', then this is an indication to skip the episode, and
                            # move onto the next one
                            if traktShowObj == None:
                                break
                            # Show the progress of the import on-screen
                            print(
                                f"({rowsCount}/{rowsTotal}) Processing Show {tvShowName} on Season {tvShowSeasonNo} - Episode {tvShowEpisodeNo}")
                            # Get the season from the Trakt API
                            season = traktShowObj.seasons[parseSeasonNo(
                                tvShowSeasonNo, traktShowObj)]
                            # Get the episode from the season
                            episode = season.episodes[int(tvShowEpisodeNo) - 1]
                            # Mark the episode as watched!
                            episode.mark_as_seen(tvShowDateWatchedConverted)
                            # Add the episode to the local database as imported, so it can be skipped,
                            # if the process is repeated
                            syncedEpisodesTable.insert(
                                {'episodeId': tvShowEpisodeId})
                            # Clear the error streak on completing the method without errors
                            errorStreak = 0
                            break
                        # Catch errors which occur because of an incorrect array index. This occurs when
                        # an incorrect Trakt show has been selected, with season/episodes which don't match TV Time.
                        # It can also occur due to a bug in Trakt Py, whereby some seasons contain an empty array of episodes.
                        except IndexError:
                            print(
                                f"({rowsCount}/{rowsTotal}) WARNING: {tvShowName} Season {tvShowSeasonNo}, Episode {tvShowEpisodeNo} does not exist (season/episode index) in Trakt!")
                            break
                        # Catch any errors which are raised because a show could not be found in Trakt
                        except trakt.errors.NotFoundException:
                            print(
                                f"({rowsCount}/{rowsTotal}) WARNING: {tvShowName} Season {tvShowSeasonNo}, Episode {tvShowEpisodeNo} does not exist (search) in Trakt!")
                            break
                        # Catch errors because of the program breaching the Trakt API rate limit
                        except trakt.errors.RateLimitException:
                            print(
                                "WARNING: The program is running too quickly and has hit Trakt's API rate limit! Please increase the delay between " +
                                "episdoes via the variable 'DELAY_BETWEEN_EPISODES_IN_SECONDS'. The program will now wait 60 seconds before " +
                                "trying again.")
                            time.sleep(60)

                            # Mark the exception in the error streak
                            errorStreak += 1
                        # Catch a JSON decode error - this can be raised when the API server is down and produces a HTML page, instead of JSON
                        except json.decoder.JSONDecodeError:
                            print(
                                f"({rowsCount}/{rowsTotal}) WARNING: A JSON decode error occuring whilst processing {tvShowName} " +
                                f"Season {tvShowSeasonNo}, Episode {tvShowEpisodeNo}! This might occur when the server is down and has produced " +
                                "a HTML document instead of JSON. The script will wait 60 seconds before trying again.")

                            # Wait 60 seconds
                            time.sleep(60)

                            # Mark the exception in the error streak
                            errorStreak += 1
                        # Catch a CTRL + C keyboard input, and exits the program
                        except KeyboardInterrupt:
                            sys.exit("Cancel requested...")
                # Skip the episode
                else:
                    print(
                        f"({rowsCount}/{rowsTotal}) Skipping '{tvShowName}' Season {tvShowSeasonNo} Episode {tvShowEpisodeNo}. It's already been imported.")


def start():
    # Create the initial authentication with Trakt, before starting the process
    if initTraktAuth():
        # Display a menu selection
        print(f">> What do you want to do?")
        print(f"    1) Import Watch History from TV Time")

        while True:
            try:
                menuSelection = int(input(f"Enter your menu selection: "))
                break
            except ValueError:
                print("Invalid input. Please enter a numerical number.")
        # Start the process which is required
        if menuSelection == 1:
            # Invoke the method which will import episodes which have been watched
            # from TV Time into Trakt
            processWatchedShows()
        else:
            print("Sorry - that's an unknown menu selection")
    else:
        print("ERROR: Unable to complete authentication to Trakt - please try again.")


if __name__ == "__main__":
    # Check that the user has created the config file
    if os.path.exists("config.json"):
        # Check that the user has provided the GDPR path
        if os.path.isdir(config.GDPR_WORKSPACE_PATH):
            start()
        else:
            print("Oops! The TV Time GDPR folder '" + config.GDPR_WORKSPACE_PATH +
                  "' does not exist on the local system. Please check it, and try again.")
    else:
        print(f"ERROR: The 'config.json' file cannot be found - have you created it yet?")
