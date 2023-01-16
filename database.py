from tinydb import TinyDB

# Create databases to keep track of completed processes
database = TinyDB("localStorage.json")
syncedEpisodesTable = database.table("SyncedEpisodes")
userMatchedShowsTable = database.table("TvTimeTraktUserMatched")
syncedMoviesTable = database.table("SyncedMovies")
userMatchedMoviesTable = database.table("TvTimeTraktUserMatchedMovies")
