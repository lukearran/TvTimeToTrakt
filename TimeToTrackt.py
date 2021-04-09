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

# Create a database to keep track of completed processes
database = TinyDB('localStorage.json')
syncedEpisodesTable = database.table('SyncedEpisodes')
userMatchedShowsTable = database.table('TvTimeTraktUserMatched')


class Expando(object):
    pass


def initTraktAuth():
    # Set the method of authentication
    trakt.core.AUTH_METHOD = trakt.core.OAUTH_AUTH
    return init(getConfiguration().TRAKT_USERNAME, store=True, client_id=getConfiguration().CLIENT_ID, client_secret=getConfiguration().CLIENT_SECRET)


def getConfiguration():
    configEx = Expando()

    with open('config.json') as f:
        data = json.load(f)

    configEx.TRAKT_USERNAME = data["TRAKT_USERNAME"]
    configEx.CLIENT_ID = data["CLIENT_ID"]
    configEx.CLIENT_SECRET = data["CLIENT_SECRET"]
    configEx.GDPR_WORKSPACE_PATH = data["GDPR_WORKSPACE_PATH"]

    return configEx


def getYearFromTitle(title):
    ex = Expando()

    try:
        # Get the year
        yearSearch = re.search(r"\(([A-Za-z0-9_]+)\)", title)
        yearValue = yearSearch.group(1)
        # Get the value outside the title
        titleValue = title.split('(')[0].strip()

        ex.titleWithoutYear = titleValue
        ex.yearValue = int(yearValue)
        return ex
    except:
        ex.titleWithoutYear = title
        ex.yearValue = -1
        return ex


def checkTitleNameMatch(tvTimeTitle, traktTitle):
    # If a name is simply a 1:1 match, then return true
    if tvTimeTitle == traktTitle:
        return True

    # Split the TvTime title
    tvTimeTitleSplit = tvTimeTitle.split()

    # Get all the words which were in the title
    wordsMatched = []

    # Go through each word of the title, and confirm if the is a match
    for word in tvTimeTitleSplit:
        if word in traktTitle:
            wordsMatched.append(word)

    # Calculate the accuracy of the match
    quotient = len(wordsMatched) / len(traktTitle.split())
    percentage = quotient * 100

    # If the title contains more than 50% of the words, then bring it up for use
    return percentage > 50


def getShowByName(name, seasonNo, episodeNo):
    # Get the 'year' from the title - if one is present
    titleObj = getYearFromTitle(name)

    # Title includes a year value, which helps with ensuring accuracy of pick
    doesTitleIncludeYear = titleObj.yearValue != -1

    # If the title included the year, then replace the name value
    # with the string without the year - which helps with ensuring
    # Trakt search accuracy
    if doesTitleIncludeYear:
        name = titleObj.titleWithoutYear

    # Search for a show with the name
    tvSearch = TVShow.search(name)

    showsWithSameName = []

    # Check if the search returned more than 1 result with the same name
    for show in tvSearch:
        if checkTitleNameMatch(name, show.title):
            # If the TV Time title included a year, then only add results with matching broadcast year
            if doesTitleIncludeYear == True:
                # If the show title is a 1:1 match, with the year, then don't bother with checking the rest -
                # since this will be a complete match
                if (name == show.title) and (show.year == titleObj.yearValue):
                    showsWithSameName = []
                    showsWithSameName.append(show)
                    break

                # Otherwise, check if the year is a match
                if show.year == titleObj.yearValue:
                    showsWithSameName.append(show)
            # Otherwise, just add all options
            else:
                showsWithSameName.append(show)

    #  Filter down the results further to results containing a 1:1 match on title
    completeMatchNames = []
    for nameFromSearch in showsWithSameName:
        if nameFromSearch.title == name:
            completeMatchNames.append(nameFromSearch)

    if (len(completeMatchNames) == 1):
        showsWithSameName = completeMatchNames

    # If the search contains more than one result with the same name, then confirm with user
    if len(showsWithSameName) > 1:

        # Check if the user has made a selection already
        userMatchedQuery = Query()
        queryResult = userMatchedShowsTable.search(
            userMatchedQuery.ShowName == name)

        # If the user has already made a selection for the show, then use the existing selection
        if len(queryResult) == 1:
            # Get the first row
            firstMatch = queryResult[0]
            firstMatchSelectedIndex = int(firstMatch.get('UserSelectedIndex'))
            skipShow = firstMatch.get('SkipShow')

            if skipShow == False:
                return showsWithSameName[firstMatchSelectedIndex]
            else:
                return None
        # Otherwise, ask the user which show they want to match with
        else:
            # Ask the user to pick
            print(
                f"MESSAGE: The TV Time Show '{name}' (Season {seasonNo}, Episode {episodeNo}) has {len(showsWithSameName)} matching Trakt shows with the same name.")

            for idx, item in enumerate(showsWithSameName):
                print(
                    f"    ({idx}) {item.title} - {item.year} - {len(item.seasons)} Season(s) - More Info: https://trakt.tv/{item.ext}")

            while(True):
                try:
                    indexSelected = (input(
                        f"Please make a selection from above (or enter SKIP):"))

                    # If the input was not skip, then validate the selection before ending loop
                    if indexSelected != 'SKIP':
                        int(indexSelected)
                        break
                    # Otherwise, exit with SKIP
                    else:
                        break
                except KeyboardInterrupt:
                    sys.exit("Cancel requested...")
                except:
                    print(
                        f"Sorry! Please select a value between 0 to {len(showsWithSameName)}")

            # If the user decides to skip the selection, return None
            if (indexSelected == 'SKIP'):
                userMatchedShowsTable.insert(
                    {'ShowName': name, 'UserSelectedIndex': 0, 'SkipShow': True})

                return None
            # Otherwise, return the selected show
            else:
                selectedShow = showsWithSameName[int(indexSelected)]

                userMatchedShowsTable.insert(
                    {'ShowName': name, 'UserSelectedIndex': indexSelected, 'SkipShow': False})

                return selectedShow

    else:
        return showsWithSameName[0]

# Confirm if the season has a "special" season starting at 0, if not, then subtract the seasonNo by 1


def parseSeasonNo(seasonNo, traktShowObj):
    seasonNo = int(seasonNo)

    # Get the first season number in the array
    firstSeasonNo = traktShowObj.seasons[0].number

    # If the season number is 0, then the show contains a "special" season
    if firstSeasonNo == 0:
        # Return the Season Number, as is
        return seasonNo
    else:
        # Otherwise, if the seasons start from 0, without any specials, then return the seasonNo,
        # but subtracted by one (unless it is a special season in TV Time)
        if seasonNo != 0:
            return seasonNo - 1
        else:
            return seasonNo


def getWatchedShowsPath():
    return getConfiguration().GDPR_WORKSPACE_PATH + "/seen_episode.csv"


def processWatchedShows():
    # Keep a count of rows processed etc
    rowsCount = 0
    rowsTotal = 0
    errorStreak = 0
    # Quickly sweep through the file to get the row count
    with open(getWatchedShowsPath()) as f:
        rowsTotal = sum(1 for line in f)

    with open(getWatchedShowsPath(), newline='') as csvfile:
        showsReader = csv.reader(csvfile, delimiter=',')

        for row in showsReader:
            rowsCount += 1
            # Get the values from the CSV record
            tvShowName = row[4]

            # Skip first row
            if tvShowName != "tv_show_name":
                tvShowEpisodeId = row[1]
                tvShowSeasonNo = row[7]
                tvShowEpisodeNo = row[8]
                tvShowDateWatched = row[5]
                tvShowDateWatchedConverted = datetime.strptime(
                    tvShowDateWatched, '%Y-%m-%d %H:%M:%S')

                # Query the database to check if it's already been processed
                episodeCompletedQuery = Query()
                queryResult = syncedEpisodesTable.search(
                    episodeCompletedQuery.episodeId == tvShowEpisodeId)

                if len(queryResult) == 0:
                    while True:
                        if (errorStreak > 10):
                            print(
                                f"An error occurred 10 times in a row... skipping episode...")
                            break

                        try:
                            # Sleep for a second between each process, before adding the next watched episode,
                            # this ensures that the program is within the rate limit of 1 per second.
                            time.sleep(5)
                            # Output to console
                            print(f"({rowsCount}/{rowsTotal}) Processing Show '" + tvShowName +
                                  "' on Season " + tvShowSeasonNo + " - Episode " + tvShowEpisodeNo)
                            # Get the Trakt version of the show
                            traktShowObj = getShowByName(
                                tvShowName, tvShowSeasonNo, tvShowEpisodeNo)
                            # Skip the episode, if no show was selected
                            if traktShowObj == None:
                                break
                            # Add the show to the user's library
                            traktShowObj.add_to_library()
                            # Get the season
                            season = traktShowObj.seasons[parseSeasonNo(
                                tvShowSeasonNo, traktShowObj)]
                            episode = season.episodes[int(tvShowEpisodeNo) - 1]
                            # Mark the episode as watched
                            episode.mark_as_seen(tvShowDateWatchedConverted)
                            # Add the show to the tracker as completed
                            syncedEpisodesTable.insert(
                                {'episodeId': tvShowEpisodeId})
                            # Once the episode has been marked watched, then break out of the loop
                            errorStreak = 0
                            break
                        except IndexError:
                            print("Oops! '" + tvShowName +
                                  "' on Season " + tvShowSeasonNo + " - Episode " + tvShowEpisodeNo + " is not within range of show array!")
                            break
                        except trakt.errors.NotFoundException:
                            print("Show '" + tvShowName +
                                  "' on Season " + tvShowSeasonNo + " - Episode " + tvShowEpisodeNo + " does not exist!")
                            break
                        except trakt.errors.RateLimitException:
                            print(
                                "Oops! You have hit the rate limit! The program will now pause for 1 minute...")
                            time.sleep(60)
                            errorStreak += 1
                        except json.decoder.JSONDecodeError:
                            print(
                                f"Oh, oh! A JSON Decode error occurred - maybe a dodgy response from the server? Waiting 60 seconds before resuming")
                            time.sleep(60)
                            errorStreak += 1
                        except KeyboardInterrupt:
                            sys.exit("Cancel requested...")
                else:
                    print(f"({rowsCount}/{rowsTotal}) Skipping '" + tvShowName +
                          "' on Season " + tvShowSeasonNo + " - Episode " + tvShowEpisodeNo + ". It's already been imported!")


def start():
    if initTraktAuth():
        # Start processing the TV shows
        processWatchedShows()
    else:
        print("Unable to authenticate with Trakt!")


if __name__ == "__main__":
    if os.path.isdir(getConfiguration().GDPR_WORKSPACE_PATH):
        start()
    else:
        print("Oops! The TV Time GDPR folder '" + getConfiguration().GDPR_WORKSPACE_PATH +
              "' does not exist on the local system. Please check it, and try again.")
