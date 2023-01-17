import json
import logging
import sys
import time
from abc import ABC, abstractmethod

import trakt.core
from tinydb import Query
from tinydb.table import Document

from database import syncedEpisodesTable, syncedMoviesTable
from searcher import TVShowSearcher, MovieSearcher, TraktTVShow, TraktMovie, TraktItem, TVTimeItem, TVTimeTVShow, \
    TVTimeMovie


class Processor(ABC):
    @abstractmethod
    def _get_synced_items(self, tv_time_item: TVTimeItem) -> list[Document]:
        pass

    @abstractmethod
    def _log_already_imported(self, tv_time_item: TVTimeItem, progress: str) -> None:
        pass

    @abstractmethod
    def _should_continue(self, tv_time_item: TVTimeItem) -> bool:
        pass

    @abstractmethod
    def _search(self, tv_time_item: TVTimeItem) -> TraktItem:
        pass

    @abstractmethod
    def _process(self, tv_time_item: TVTimeItem, trakt_item: TraktItem, progress: str) -> None:
        pass

    def process_item(self, tv_time_item: TVTimeItem, progress: str, delay: int = 1) -> None:
        # Query the local database for previous entries indicating that
        # the item has already been imported in the past. Which will
        # ease pressure on Trakt's API server during a retry of the import
        # process, and just save time overall without needing to create network requests.
        synced_episodes = self._get_synced_items(tv_time_item)
        if len(synced_episodes) != 0:
            self._log_already_imported(tv_time_item, progress)
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

            if not self._should_continue(tv_time_item):
                break

            try:
                # Sleep for a second between each process, before going onto the next watched item.
                # This is required to remain within the API rate limit, and use the API server fairly.
                # Other developers share the service, for free - so be considerate of your usage.
                time.sleep(delay)

                trakt_item = self._search(tv_time_item)
                if trakt_item is None:
                    break

                self._process(tv_time_item, trakt_item, progress)

                error_streak = 0
                break
            # Catch errors which occur because of an incorrect array index. This occurs when
            # an incorrect Trakt show has been selected, with season/episodes which don't match TV Time.
            # It can also occur due to a bug in Trakt Py, whereby some seasons contain an empty array of episodes.
            except IndexError:
                self._handle_index_error(tv_time_item, trakt_item, progress)
                break
            except trakt.core.errors.NotFoundException:
                self._handle_not_found_exception(tv_time_item, progress)
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
                    f"({progress}) - A JSON decode error occurred whilst processing {tv_time_item.name}"
                    " This might occur when the server is down and has produced"
                    " a HTML document instead of JSON. The script will wait 60 seconds before trying again."
                )

                time.sleep(60)
                error_streak += 1
            # Catch a CTRL + C keyboard input, and exits the program
            except KeyboardInterrupt:
                sys.exit("Cancel requested...")
            except Exception as e:
                logging.error(
                    f"Got unknown error {e},"
                    f" while processing {tv_time_item.name}"
                )
                error_streak += 1

    @abstractmethod
    def _handle_index_error(self, tv_time_item: TVTimeItem, trakt_item: TraktItem, progress: str) -> None:
        pass

    @abstractmethod
    def _handle_not_found_exception(self, tv_time_item: TVTimeItem, progress: str) -> None:
        pass


class TVShowProcessor(Processor):
    def __init__(self):
        super().__init__()

    def _get_synced_items(self, tv_time_show: TVTimeTVShow) -> list[Document]:
        episode_completed_query = Query()
        return syncedEpisodesTable.search(episode_completed_query.episodeId == tv_time_show.episode_id)

    def _log_already_imported(self, tv_time_show: TVTimeTVShow, progress: str) -> None:
        logging.info(
            f"({progress}) - Already imported,"
            f" skipping \'{tv_time_show.name}\' Season {tv_time_show.season_number} /"
            f" Episode {tv_time_show.episode_number}."
        )

    def _should_continue(self, tv_time_show: TVTimeTVShow) -> bool:
        return True

    def _search(self, tv_time_show: TVTimeTVShow) -> TraktTVShow:
        return TVShowSearcher(tv_time_show).search(tv_time_show.title)

    def _process(self, tv_time_show: TVTimeTVShow, trakt_show: TraktItem, progress: str) -> None:
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
        logging.info(
            f"'{tv_time_show.name} Season {tv_time_show.season_number},"
            f" Episode {tv_time_show.episode_number}' marked as seen"
        )

    def _handle_index_error(self, tv_time_show: TVTimeTVShow, trakt_show: TraktTVShow, progress: str) -> None:
        tv_show_slug = trakt_show.to_json()["shows"][0]["ids"]["ids"]["slug"]
        logging.warning(
            f"({progress}) - {tv_time_show.name} Season {tv_time_show.season_number},"
            f" Episode {tv_time_show.episode_number} does not exist in Trakt!"
            f" (https://trakt.tv/shows/{tv_show_slug}/seasons/{tv_time_show.season_number}/episodes/{tv_time_show.episode_number})"
        )

    def _handle_not_found_exception(self, tv_time_show: TVTimeTVShow, progress: str) -> None:
        logging.warning(
            f"({progress}) - {tv_time_show.name} Season {tv_time_show.season_number},"
            f" Episode {tv_time_show.episode_number} does not exist (search) in Trakt!"
        )


class MovieProcessor(Processor):
    def __init__(self, watched_list: list):
        super().__init__()
        self._watched_list = watched_list

    def _get_synced_items(self, tv_time_movie: TVTimeMovie) -> list[Document]:
        movie_query = Query()
        return syncedMoviesTable.search(
            (movie_query.movie_name == tv_time_movie.name) & (movie_query.type == "watched")
        )

    def _log_already_imported(self, tv_time_movie: TVTimeMovie, progress: str) -> None:
        logging.info(f"({progress}) - Already imported, skipping '{tv_time_movie.name}'.")

    def _should_continue(self, tv_time_movie: TVTimeMovie) -> bool:
        # If movie is watched but this is an entry for watchlist, then skip
        if tv_time_movie.name in self._watched_list and tv_time_movie.activity_type != "watch":
            logging.info(f"Skipping '{tv_time_movie.name}' to avoid redundant watchlist entry.")
            return False

        return True

    def _search(self, tv_time_movie: TVTimeMovie) -> TraktMovie:
        return MovieSearcher().search(tv_time_movie.title)

    def _process(self, tv_time_movie: TVTimeMovie, trakt_movie: TraktMovie, progress: str) -> None:
        logging.info(f"({progress}) - Processing '{tv_time_movie.name}'")

        watchlist_query = Query()
        movies_in_watchlist = syncedMoviesTable.search(
            (watchlist_query.movie_name == tv_time_movie.name) & (watchlist_query.type == "watchlist")
        )

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

    def _handle_index_error(self, tv_time_movie: TVTimeMovie, trakt_movie: TraktMovie, progress: str) -> None:
        movie_slug = trakt_movie.to_json()["movies"][0]["ids"]["ids"]["slug"]
        logging.warning(
            f"({progress}) - {tv_time_movie.name}"
            f" does not exist in Trakt! (https://trakt.tv/movies/{movie_slug}/)"
        )

    def _handle_not_found_exception(self, tv_time_movie: TVTimeMovie, progress: str) -> None:
        logging.warning(f"({progress}) - {tv_time_movie.name} does not exist (search) in Trakt!")
