# TimeToTrakt
A quick Python script to import TV Time watched episode data into Trakt.TV - using data provided by Whip Media Company via a GDPR request.

# Issues
They'll be a few! If you come across anything then let me know in the 'Issue' section, and I'll provide help where possible.

# Notes
1. The script is using limited data provided from a GDPR request - so the accuracy isn't 100%. But you will be prompted to manually pick the Trakt show, when it can't be determined automatically.
2. A delay of 5 seconds is added between each episode to ensure fair use of Trakt's servers - especially with my import of 6,500 episodes.
3. Episodes which have been imported are be saved to `localStorage.json` - this will let you re-run and skip to episodes which have not been imported yet.

# Setup
## Get your Data
The TV Time API is request based only. In order to get access to your data, you will have to request it from TV Time's Support via a GDPR request - or just ask for it!

1. Copy the template provided by [www.datarequests.org](https://www.datarequests.org/blog/sample-letter-gdpr-access-request/) into an email.
2. Send it to support@tvtime.com
3. Wait a few working days to process the request
4. Extract the data somewhere safe.

## Register API Access at Trakt
1. Go to "Settings" under your profile
2. Select ["Your API Applications"](https://trakt.tv/oauth/applications)
3. Select "New Application"
4. Provide a random name into "Name"
5. Paste "urn:ietf:wg:oauth:2.0:oob" into "Redirect uri:"
6. Click "Save App"
7. Make note of your app

## Setup Script
Install the following frameworks via Pip:
1. trakt
2. tinydb

Then, execute the program using the `./python3 TimeToTrakt.py` and provide the requested information when prompted.