import json
from datetime import datetime, timedelta

import backoff
import requests
import singer
from singer import metrics
from ratelimit import limits, sleep_and_retry, RateLimitException
from requests.exceptions import ConnectionError

LOGGER = singer.get_logger()

class Server5xxError(Exception):
    pass

class Server429Error(Exception):
    pass

def log_backoff_attempt(details):
    LOGGER.info("Error detected communicating with Zoom, triggering backoff: %d try",
                details.get("tries"))

class ZoomClient(object):
    BASE_URL = 'https://api.zoom.us/v2/'

    def __init__(self, config, config_path):
        self.__user_agent = config.get('user_agent')
        self.__session = requests.Session()
        self.__config_path = config_path
        self.__access_token = None
        self.__refresh_token = None
        self.__use_jwt = False
        self.__expires_at = None
        self.start_date = config.get('start_date')
        """
            Add the `sync_today` flag if you want to also ingest data as it's coming
            in for the current day. 
            
            NOTE: Since the Zoom API only takes dates (instead of timestamps) for the 
            date ranges when requesting recordings, the share_url for today is not stable. Therefore make sure to also use the url_to_recordings
            to map share_url to a stable uuid: https://github.com/Pathlight/tap-zoom/pull/5
        """
        self.sync_today = config.get('sync_today', False)

        jwt = config.get('jwt')
        refresh_token = config.get('refresh_token')
        if jwt:
            self.__access_token = jwt
            self.__use_jwt = True
        else:
            self.__client_id = config.get('client_id')
            self.__client_secret = config.get('client_secret')
            if refresh_token:
                self.__refresh_token = config.get('refresh_token')
            else:
                # For server-to-server oauth apps, there are no refresh
                # tokens. We use the persistent access token.
                # https://marketplace.zoom.us/docs/guides/build/server-to-server-oauth-app/
                self.__account_id = config.get('account_id')

    def __enter__(self):
        return self

    def __exit__(self, type, value, traceback):
        self.__session.close()

    def get_new_access_token(self):
        # This is for the oauth server-to-server access type. From the docs:
        # To get a new access token, your app should call the /oauth/token endpoint again with the account_credentials grant.
        if self.__refresh_token is not None:
            return

        data = self.request(
            'POST',
            url='https://zoom.us/oauth/token',
            auth=(self.__client_id, self.__client_secret),
            data={
                'grant_type': 'account_credentials',
                'account_id': self.__account_id
            })
        self.__access_token = data['access_token']
        self.__expires_at = datetime.utcnow() + \
            timedelta(seconds=data['expires_in'] - 10) # pad by 10 seconds for clock drift

        with open(self.__config_path) as file:
            config = json.load(file)
        config['access_token'] = data['access_token']
        with open(self.__config_path, 'w') as file:
            json.dump(config, file, indent=2)


    def refresh_access_token(self):
        data = self.request(
            'POST',
            url='https://zoom.us/oauth/token',
            auth=(self.__client_id, self.__client_secret),
            data={
                'refresh_token': self.__refresh_token,
                'grant_type': 'refresh_token'
            })
        self.__access_token = data['access_token']
        self.__refresh_token = data['refresh_token']

        self.__expires_at = datetime.utcnow() + \
            timedelta(seconds=data['expires_in'] - 10) # pad by 10 seconds for clock drift

        ## refresh_token changes every call to refresh
        with open(self.__config_path) as file:
            config = json.load(file)
        config['refresh_token'] = data['refresh_token']
        with open(self.__config_path, 'w') as file:
            json.dump(config, file, indent=2)

    def check_and_renew_access_token(self):
        uses_refresh_token = self.__use_jwt == False and self.__refresh_token is not None
        expired_token = self.__expires_at is None or self.__expires_at <= datetime.utcnow()

        if uses_refresh_token and (self.__access_token is None or expired_token):
            self.refresh_access_token()
        elif not uses_refresh_token and expired_token:
            self.get_new_access_token()


    @backoff.on_exception(backoff.expo,
                          (Server5xxError, RateLimitException, Server429Error, ConnectionError),
                          max_tries=8,
                          on_backoff=log_backoff_attempt,
                          factor=3)
    @limits(calls=300, period=60)
    def request(self,
                method,
                path=None,
                url=None,
                ignore_zoom_error_codes=[],
                ignore_http_error_codes=[],
                **kwargs):
        if url is None:
            self.check_and_renew_access_token()
        
        if url is None and path:
            url = '{}{}'.format(self.BASE_URL, path)

        if 'endpoint' in kwargs:
            endpoint = kwargs['endpoint']
            del kwargs['endpoint']
        else:
            endpoint = None

        if 'headers' not in kwargs:
            kwargs['headers'] = {}

        kwargs['headers']['Authorization'] = 'Bearer {}'.format(self.__access_token)

        if self.__user_agent:
            kwargs['headers']['User-Agent'] = self.__user_agent

        with metrics.http_request_timer(endpoint) as timer:
            response = self.__session.request(method, url, **kwargs)
            metrics_status_code = response.status_code
            if response.status_code in [400, 404] and response.status_code < 500:
                if response.status_code in ignore_http_error_codes or \
                    (response.status_code == 400 and response.json().get('code') in ignore_zoom_error_codes):
                    metrics_status_code = 200
                return None

            timer.tags[metrics.Tag.http_status_code] = metrics_status_code

        if response.status_code >= 500:
            raise Server5xxError()

        if response.status_code == 429:
            LOGGER.warn('Rate limit hit - 429')
            raise Server429Error(response.text)

        response.raise_for_status()

        return response.json()

    def get(self, path, **kwargs):
        return self.request('GET', path=path, **kwargs)
