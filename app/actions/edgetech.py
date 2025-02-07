#!/usr/bin/env python3

"""
get_database_dump.py provides a reference implementation of the OAuth2
Authorization Code Flow in order to connect to the fisheries database dump api.

get_database_dump.py is written for use with Python 2 or 3. To run, from the
command line, run

python get_database_dump.py

The script will provide a long URL. Copy and paste the entire URL into the
browser of your choice. It will proceed to have you login. After logging in, the
browser will redirect to a page that seems to be empty, this is the expected
behavior. Copy the full contents of the browsers address bar and paste it into
the console where the script is running. The script will proceed to download the
database dump.

Use -s dev | test to download dev or test environment databases respectively

After doing the OAuth2 flow, the temporary credentials are stored in token-{stage}.json.
As long as token-{stage}.json exists, you will not need to log in again.

NOTE this is setup for production token only
"""

from __future__ import print_function
from builtins import input
from builtins import bytes
from enum import Enum
import argparse

import logging

from urllib.request import urlopen, Request, HTTPErrorProcessor, build_opener
from urllib.parse import urlencode

import time
import json
import re
import gzip

logger = logging.getLogger(__name__)


class Stage(Enum):
    DEV = 'dev'
    TEST = 'test'
    PRODUCTION = 'production'

    def __str__(self):
        return self.name


class Configuration:
    authorizeUrl: str
    tokenUrl: str
    clientId: str
    redirectUrl = 'https://app.local'
    scope = 'offline_access database:dump openid profile email'
    apiBaseUrl: str
    v1Url: str
    databaseDumpUrl: str
    numGetRetry = 60

    def __init__(self, stage: Stage):
        # assert stage == "dev" or stage == "test" or stage == "prod", "invalid stage. Must be dev, test, or prod"
        if stage == Stage.DEV:
            self.authorizeUrl = 'https://trap-tracker-dev.auth0.com/authorize'
            self.tokenUrl = 'https://trap-tracker-dev.auth0.com/oauth/token'
            self.clientId = '3l0QJXQRqDOkWf7bfzMo2bChnowcUPLS'
            self.apiBaseUrl = 'https://api-dev.trap-tracker.com'
        elif stage == Stage.TEST:
            self.authorizeUrl = 'https://trap-tracker.auth0.com/authorize'
            self.tokenUrl = 'https://trap-tracker.auth0.com/oauth/token'
            self.clientId = 'b76t6LBIW23mMY2TlMSplgIKH9Cs2yns'
            self.apiBaseUrl = 'https://api-test.trap-tracker.com'
        elif stage == Stage.PRODUCTION:
            self.authorizeUrl = 'https://trap-tracker.auth0.com/authorize'
            self.tokenUrl = 'https://trap-tracker.auth0.com/oauth/token'
            self.clientId = 'b76t6LBIW23mMY2TlMSplgIKH9Cs2yns'
            self.apiBaseUrl = 'https://api.trap-tracker.com'
        self.v1Url = self.apiBaseUrl + '/v1'
        self.databaseDumpUrl = self.v1Url + '/database-dump/tasks'


class NoRedirection(HTTPErrorProcessor):
    def http_response(self, request, response):
        return response

    https_response = http_response


class EdgetechDownloadClient:

    def __init__(self, *, token=None):

        self._token = token

    def update_token(self, response, refresh_token=None):
        # token only includes 'expires_in' (relative time), convert to 'expires_at' (UTC absolute time)
        token = json.loads(response.read().decode('utf-8'))
        token['expires_at'] = time.time() + \
                              token['expires_in']

        if not 'refresh_token' in token and refresh_token:
            token['refresh_token'] = refresh_token

        self._token = token

        return self._token

    def get_token(self, config: Configuration):
        try:

            # if token is expired, refresh it
            now = time.time()
            if now >= self._token['expires_at']:
                refresh_params = {
                    'grant_type': 'refresh_token',
                    'refresh_token': self._token['refresh_token'],
                    'client_id': config.clientId,
                    'redirect_uri': config.redirectUrl,
                    'scope': config.scope
                }

                refresh_request = Request(config.tokenUrl, data=bytes(
                    urlencode(refresh_params), 'utf-8'))
                refresh_response = urlopen(refresh_request)

                self._token = self.update_token(
                    refresh_response, self._token['refresh_token'])

            return self._token

        except Exception:  # pylint: disable=broad-except

            # build full authorize url
            authorize_query = {
                'response_type': 'code',
                'client_id': config.clientId,
                'redirect_uri': config.redirectUrl,
                'audience': config.v1Url,
                'scope': config.scope}

            print(
                'Please go here and login, {}?{}'.format(config.authorizeUrl, urlencode(authorize_query)))

            authorize_response = input(
                'After login, copy browser url and paste here: ')

            # extract code from response
            match = re.search(r'code=([-a-zA-Z0-9]+)&?', authorize_response)
            assert match
            code = match.groups(0)[0]

            # post to token url with code to get token
            token_params = {
                'grant_type': 'authorization_code',
                'code': code,
                'client_id': config.clientId,
                'redirect_uri': config.redirectUrl,
                'scope': config.scope
            }

            token_request = Request(config.tokenUrl, data=bytes(
                urlencode(token_params), 'utf-8'))
            token_response = urlopen(token_request)

            token = self.update_token(token_response)
            return token

    def download_edgetech_data(self, config: Configuration, access_token=None):
        if not access_token:
            token = self.get_token(config)

            access_token = token['access_token']

        headers = {'Authorization': f'Bearer {access_token}'}
        opener = build_opener(NoRedirection)

        # Disable Certificate Verification
        import urllib
        from urllib.request import urlopen

        # Create a context and tailor it.
        ctx = urllib.request.ssl.create_default_context()
        ctx.check_hostname = False
        ctx.verify_mode = urllib.request.ssl.CERT_NONE

        # Use that context in requests
        with urlopen(config.databaseDumpUrl, context=ctx) as fi:
            # Read from fi
            print(fi.read())

        # perform a POST to initiate dump
        # POST returns immediatesly with a 303/location redirect
        start_dump_request = Request(config.databaseDumpUrl, headers=headers, data=b'')
        start_dump_response = opener.open(start_dump_request)
        assert start_dump_response.code == 303, "invalid response: {} ({})".format(
            start_dump_response.read(), start_dump_response.code)

        # poll the resulting location with a GET to retrieve the artifact
        get_dump_request = Request(config.apiBaseUrl + start_dump_response.headers['location'], headers=headers)

        data = None
        for x in range(config.numGetRetry):
            # poll with GET requests until 303 is returned (dump is done processing)
            logger.debug('Get Dump Attempt {}/{}: '.format(x + 1, config.numGetRetry))
            get_dump_response = opener.open(get_dump_request)

            if get_dump_response.code == 200:
                logger.info(get_dump_response.read().decode('utf-8'))
                time.sleep(1)

            elif get_dump_response.code == 303:
                logger.debug("success - downloading")

                # one last GET - to download file
                download_dump_request = Request(
                    get_dump_response.headers['location'])
                download_dump_response = opener.open(download_dump_request)

                # extract filename from Content-Disposition header
                match = re.search(
                    r'filename="(.*)"', download_dump_response.headers['content-disposition'])
                assert match
                fname = match.groups(0)[0]
                logger.info('Downloaded file: %s', fname)
                with gzip.open(download_dump_response) as fo:
                    data = json.load(fo)

                break

        return data