#!/usr/bin/env python
import praw
import os
import re
import time
import webbrowser
from threading import Thread
from http.server import HTTPServer, BaseHTTPRequestHandler
from urllib.parse import urlparse, parse_qs


# ### CONFIGURATION ### #
REFRESH_MARGIN = 60
REDIRECT_URL = "127.0.0.1"
REDIRECT_PORT = 65010
REDIRECT_PATH = "authorize_callback"
DEFAULT_CONFIG = os.path.join(os.path.dirname(os.path.abspath(__file__)), "oauth.txt")

CONFIGKEY_APP_KEY = "app_key"
CONFIGKEY_APP_SECRET = "app_secret"
CONFIGKEY_SCOPE = "scope"
CONFIGKEY_REFRESHABLE = "refreshable"
CONFIGKEY_TOKEN = "token"
CONFIGKEY_REFRESH_TOKEN = "refresh_token"
# ### END CONFIGURATION ### #


class OAuth2UtilRequestHandler(BaseHTTPRequestHandler):

	def do_GET(self):
		parsed_url = urlparse(self.path)

		if parsed_url[2] != "/" + REDIRECT_PATH:  # 2 = Path
			self.send_response(404)
			self.end_headers()
			return

		parsed_query = parse_qs(parsed_url[4])  # 4 = Query

		if "code" not in parsed_query:
			self.send_response(200)
			self.send_header("Content-Type", "text/plain")
			self.end_headers()

			self.wfile.write("No code found, try again!".encode("utf-8"))
			return

		self.server.oauth2util.response_code = parsed_query["code"][0]

		self.send_response(200)
		self.send_header("Content-Type", "text/plain")
		self.end_headers()

		self.wfile.write(
			"Thank you for using OAuth2Util. The authorization was successful, "
			"you can now close this window.".encode("utf-8"))


class OAuth2Util:

	def __init__(self, reddit, app_key=None, app_secret=None, scope=None,
				 refreshable=None, configfile=DEFAULT_CONFIG,
				 print_log=False):
		self.r = reddit
		self.valid_until = time.time()
		self.server = None
		
		self.configfile = configfile
		
		self.config = {}
		
		self._read_config(self.config, configfile)
		
		if app_key:
			self.config[CONFIGKEY_APP_KEY] = app_key
		
		if app_secret:
			self.config[CONFIGKEY_APP_SECRET] = app_secret
		
		if scope:
			self.config[CONFIGKEY_SCOPE] = set(scope)
		
		if refreshable:
			self.config[CONFIGKEY_REFRESHABLE] = refreshable

		self._print = print_log

		self._set_app_info()
		self.refresh()
		self.set_access_credentials()

	# ### LOAD SETTINGS ### #

	def _set_app_info(self):
		redirect_url = "http://{0}:{1}/{2}".format(REDIRECT_URL, REDIRECT_PORT,
												   REDIRECT_PATH)
		self.r.set_oauth_app_info(self.config[CONFIGKEY_APP_KEY], self.config[CONFIGKEY_APP_SECRET], redirect_url)
			
	def _read_config(self, config, configfile):
		try:
			with open(configfile) as f:
				lines = [x.strip() for x in f.readlines()]
			pat = re.compile(r"^(\w+)[\t ]*=[\t ]*(.+)$")
			for l in lines:
				m = pat.match(l)
				try:
					key = m.group(1)
					val = m.group(2)
				except AttributeError:
					continue
				if val=="True":val=True
				if val=="False":val=False
				if val=="None":val=None
				if key==CONFIGKEY_SCOPE:val=set(val.split(","))
				config[key] = val
			return config
		except OSError:
			if self._print:
				print("_read_config:", configfile, "not found.")
	
	def _change_value(self, file, key, value):
		try:
			with open(file) as f:
				lines = [x.strip() for x in f.readlines()]
		except OSError:
			if self._print:
				print("_change_value read:", file, "not found.")
			lines = []
		found = False
		for i in range(len(lines)):
			if lines[i].startswith(key):
				lines[i] = "{0}={1}".format(key, str(value))
				found = True
				break
		if not found:
			lines.append("{0}={1}".format(key, str(value)))
		try:
			with open(file, "w") as f:
				f.write("\n".join(lines))
		except OSError:
			if self._print:
				print("_change_value write:", file, "not found.")

	# ### SAVE SETTINGS ### #

	def _save_token(self):
		self._change_value(self.configfile, CONFIGKEY_TOKEN, self.config[CONFIGKEY_TOKEN])
		self._change_value(self.configfile, CONFIGKEY_REFRESH_TOKEN, self.config[CONFIGKEY_REFRESH_TOKEN])

	# ### REQUEST FIRST TOKEN ### #

	def _start_webserver(self):
		server_address = (REDIRECT_URL, REDIRECT_PORT)
		self.server = HTTPServer(server_address, OAuth2UtilRequestHandler)
		self.server.oauth2util = self
		self.response_code = None
		t = Thread(target=self.server.serve_forever)
		t.daemon = True
		t.start()

	def _wait_for_response(self):
		while not self.response_code:
			time.sleep(2)
		time.sleep(5)
		self.server.shutdown()

	def _get_new_access_information(self):
		try:
			url = self.r.get_authorize_url(
				"SomeRandomState", self.config[CONFIGKEY_SCOPE], self.config[CONFIGKEY_REFRESHABLE])
		except praw.errors.OAuthAppRequired:
			print(
				"Cannot obtain authorize url from praw. Please check your "
				"configuration files.")
			raise

		self._start_webserver()
		webbrowser.open(url)
		self._wait_for_response()

		try:
			access_information = self.r.get_access_information(
				self.response_code)
		except praw.errors.OAuthException:
			print("--------------------------------")
			print(
				"Can not authenticate, maybe the app infos (e.g. secret) "
				"are wrong.")
			print("--------------------------------")
			raise

		self.config[CONFIGKEY_TOKEN] = access_information["access_token"]
		self.config[CONFIGKEY_REFRESH_TOKEN] = access_information["refresh_token"]
		self.valid_until = time.time() + 3600
		self._save_token()

	# ### PUBLIC API ### #

	def toggle_print(self):
		self._print = not self._print
		if self._print:
			print('OAuth2Util printing on')

	def set_access_credentials(self):
		"""
		Set the token on the Reddit Object again
		"""
		try:
			self.r.set_access_credentials(self.config[CONFIGKEY_SCOPE], self.config[CONFIGKEY_TOKEN],
										  self.config[CONFIGKEY_REFRESH_TOKEN])
		except praw.errors.OAuthInvalidToken:
			if self._print:
				print("Request new Token")
			self._get_new_access_information()

	# ### REFRESH TOKEN ### #

	def refresh(self):
		"""
		Check if the token is still valid and requests a new if it is not
		valid anymore

		Call this method before a call to praw
		if there might have passed more than one hour
		"""
		if time.time() > self.valid_until - REFRESH_MARGIN:
			if self._print:
				print("Refresh Token")
			try:
				new_token = self.r.refresh_access_information(self.config[CONFIGKEY_REFRESH_TOKEN])
				self.config[CONFIGKEY_TOKEN] = new_token["access_token"]
				self.valid_until = time.time() + 3600
				self._save_token()
				self.set_access_credentials()
			except praw.errors.OAuthInvalidToken:
				if self._print:
					print("Request new Token")
				self._get_new_access_information()
