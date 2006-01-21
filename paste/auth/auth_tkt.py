##########################################################################
#
# Copyright (c) 2005 Imaginary Landscape LLC and Contributors.
#
# Permission is hereby granted, free of charge, to any person obtaining
# a copy of this software and associated documentation files (the
# "Software"), to deal in the Software without restriction, including
# without limitation the rights to use, copy, modify, merge, publish,
# distribute, sublicense, and/or sell copies of the Software, and to
# permit persons to whom the Software is furnished to do so, subject to
# the following conditions:
#
# The above copyright notice and this permission notice shall be
# included in all copies or substantial portions of the Software.
#
# THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND,
# EXPRESS OR IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF
# MERCHANTABILITY, FITNESS FOR A PARTICULAR PURPOSE AND
# NONINFRINGEMENT. IN NO EVENT SHALL THE AUTHORS OR COPYRIGHT HOLDERS BE
# LIABLE FOR ANY CLAIM, DAMAGES OR OTHER LIABILITY, WHETHER IN AN ACTION
# OF CONTRACT, TORT OR OTHERWISE, ARISING FROM, OUT OF OR IN CONNECTION
# WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN THE SOFTWARE.
##########################################################################
"""
Implementation of cookie signing as done in `mod_auth_tkt
<http://www.openfusion.com.au/labs/mod_auth_tkt/>`_.

mod_auth_tkt is an Apache module that looks for these signed cookies
and sets ``REMOTE_USER``, ``REMOTE_USER_TOKENS`` (a comma-separated
list of groups) and ``REMOTE_USER_DATA`` (arbitrary string data).

This module is an alternative to the ``paste.auth.cookie`` module;
it's primary benefit is compatibility with mod_auth_tkt, which in turn
makes it possible to use the same authentication process with
non-Python code run under Apache.
"""

import time as time_mod
import md5
import cgi
import Cookie
from paste import request

class AuthTicket(object):

    """
    This class represents an authentication token.  You must pass in
    the shared secret, the userid, and the IP address.  Optionally you
    can include tokens (a list of strings, representing role names),
    'user_data', which is arbitrary data available for your own use in
    later scripts.  Lastly, you can override the cookie name and
    timestamp.

    Once you provide all the arguments, use .cookie_value() to
    generate the appropriate authentication ticket.  .cookie()
    generates a Cookie object, the str() of which is the complete
    cookie header to be sent.

    CGI usage::

        token = auth_tkt.AuthTick('sharedsecret', 'username',
            os.environ['REMOTE_ADDR'], tokens=['admin'])
        print 'Status: 200 OK'
        print 'Content-type: text/html'
        print token.cookie()
        print
        ... redirect HTML ...

    Webware usage::

        token = auth_tkt.AuthTick('sharedsecret', 'username',
            self.request().environ()['REMOTE_ADDR'], tokens=['admin'])
        self.response().setCookie('auth_tkt', token.cookie_value())

    Be careful not to do an HTTP redirect after login; use meta
    refresh or Javascript -- some browsers have bugs where cookies
    aren't saved when set on a redirect.
    """

    def __init__(self, secret, userid, ip, tokens=(), user_data='',
                 time=None, cookie_name='auth_tkt',
                 secure=False):
        self.secret = secret
        self.userid = userid
        self.ip = ip
        self.tokens = ','.join(tokens)
        self.user_data = user_data
        if time is None:
            self.time = time_mod.time()
        else:
            self.time = time
        self.cookie_name = cookie_name
        self.secure = secure

    def digest(self):
        return calculate_digest(
            self.ip, self.time, self.secret, self.userid, self.tokens,
            self.user_data)

    def cookie_value(self):
        v = '%s%08x%s!' % (self.digest(), int(self.time), self.userid)
        if self.tokens:
            v += self.tokens + '!'
        v += self.user_data
        return v

    def cookie(self):
        c = Cookie.SimpleCookie()
        c[self.cookie_name] = self.cookie_value().encode('base64').strip().replace('\n', '')
        c[self.cookie_name]['path'] = '/'
        if self.secure:
            c[self.cookie_name]['secure'] = 'true'
        return c

class BadTicket(Exception):
    """
    Exception raised when a ticket can't be parsed.  If we get
    far enough to determine what the expected digest should have
    been, expected is set.  This should not be shown by default,
    but can be useful for debugging.
    """
    def __init__(self, msg, expected=None):
        self.expected = expected
        Exception.__init__(self, msg)

def parse_ticket(secret, ticket, ip):
    """
    Parse the ticket, returning (timestamp, userid, tokens, user_data).

    If the ticket cannot be parsed, ``BadTicket`` will be raised with
    an explanation.
    """
    ticket = ticket.strip('"')
    digest = ticket[:32]
    try:
        timestamp = int(ticket[32:40], 16)
    except ValueError, e:
        raise BadTicket('Timestamp is not a hex integer: %s' % e)
    try:
        userid, data = ticket[40:].split('!', 1)
    except ValueError:
        raise BadTicket('userid is not followed by !')
    if '!' in data:
        tokens, user_data = data.split('!', 1)
    else:
        # @@: Is this the right order?
        tokens = ''
        user_data = data
    
    expected = calculate_digest(ip, timestamp, secret,
                                userid, tokens, user_data)

    if expected != digest:
        raise BadTicket('Digest signature is not correct',
                        expected=(expected, digest))

    tokens = tokens.split(',')

    return (timestamp, userid, tokens, user_data)
    
def calculate_digest(ip, timestamp, secret, userid, tokens, user_data):
    digest0 = md5.new(
        encode_ip_timestamp(ip, timestamp) + secret + userid + '\0'
        + tokens + '\0' + user_data).hexdigest()
    digest = md5.new(digest0 + secret).hexdigest()
    return digest

def encode_ip_timestamp(ip, timestamp):
    ip_chars = ''.join(map(chr, map(int, ip.split('.'))))
    t = int(timestamp)
    ts = ((t & 0xff000000) >> 24,
          (t & 0xff0000) >> 16,
          (t & 0xff00) >> 8,
          t & 0xff)
    ts_chars = ''.join(map(chr, ts))
    return ip_chars + ts_chars


class AuthTKTMiddleware(object):

    """
    Middleware that checks for signed cookies that match what
    `mod_auth_tkt <http://www.openfusion.com.au/labs/mod_auth_tkt/>`_
    looks for (if you have mod_auth_tkt installed, you don't need this
    middleware, since Apache will set the environmental variables for
    you).

    This also adds two functions to the request:

    ``environ['set_user'](username, tokens='', user_data='')``

        This sets a cookie that logs the user in.  ``tokens`` is a
        string (comma-separated groups) or a list of strings.
        ``user_data`` is a string for your own use.

    ``environ['logout_user']``()

        Logs out the user.
    """

    def __init__(self, app, secret, cookie_name='auth_tkt', secure=False,
                 include_ip=True):
        self.app = app
        self.secret = secret
        self.cookie_name = cookie_name
        self.secure = secure
        self.include_ip = include_ip

    def __call__(self, environ, start_response):
        cookies = request.get_cookies(environ)
        if cookies.has_key(self.cookie_name):
            cookie_value = cookies[self.cookie_name].value
        else:
            cookie_value = ''
        if cookie_value:
            if self.include_ip:
                remote_addr = environ['REMOTE_ADDR']
            else:
                # mod_auth_tkt uses this dummy value when IP is not
                # checked:
                remote_addr = '0.0.0.0'
            # @@: This should handle bad signatures better:
            # Also, timeouts should cause cookie refresh
            timestamp, userid, tokens, user_data = parse_ticket(
                secret, cookie, remote_addr)
            tokens = ','.join(tokens)
            environ['REMOTE_USER'] = userid
            if environ.get('REMOTE_USER_TOKENS'):
                # We want to add tokens/roles to what's there:
                tokens = environ['REMOTE_USER_TOKENS'] + ',' + tokens
            environ['REMOTE_USER_TOKENS'] = tokens
            environ['REMOTE_USER_DATA'] = user_data
            environ['AUTH_TYPE'] = 'cookie'
        set_cookies = []
        def set_user(userid, tokens='', user_data=''):
            set_cookies.extend(self.set_user_cookie(
                environ, userid, tokens, user_data))
        def logout_user():
            set_cookies.extend(self.logout_user_cookie(environ))
        environ['paste.auth_tkt.set_user'] = set_user
        environ['paste.auth_tkt.logout_user'] = logout_user
        def cookie_setting_start_response(status, headers, exc_info=None):
            headers.extend(set_cookies)
            return start_response(status, headers, exc_info)
        return self.app(environ, cookie_setting_start_response)

    def set_user_cookie(self, environ, userid, tokens, user_data):
        if not isinstance(tokens, basestring):
            tokens = ','.join(tokens)
        if self.include_ip:
            remote_addr = environ['REMOTE_ADDR']
        else:
            remote_addr = '0.0.0.0'
        ticket = AuthTicket(
            self.secret,
            userid,
            remote_addr,
            tokens=tokens,
            user_data=user_data,
            cookie_name=self.cookie_name,
            secure=self.secure)
        # @@: Should we set REMOTE_USER etc in the current
        # environment right now as well?
        cookies = [
            ('Set-Cookie', '%s=%s; Path=/' % (
            self.cookie_name, ticket.cookie_value()))]
        return cookies
    
    def logout_user_cookie(self, environ):
        cur_domain = environ.get('HTTP_HOST', environ.get('SERVER_NAME'))
        wild_domain = '.' + cur_domain
        cookies = [
            ('Set-Cookie', '%s=""; Path=/' % self.cookie_name),
            ('Set-Cookie', '%s=""; Path=/; Domain=%s' %
             (self.cookie_name, cur_domain)),
            ('Set-Cookie', '%s=""; Path=/; Domain=%s' %
             (self.cookie_name, wild_domain)),
            ]
        return cookies

def make_auth_tkt_middleware(
    app,
    global_conf,
    secret=None,
    cookie_name='auth_tkt',
    secure=False,
    include_ip=True):
    """
    Creates the `AuthTKTMiddleware
    <class-paste.auth.auth_tkt.AuthTKTMiddleware.html>`_.

    ``secret`` is requird, but can be set globally or locally.
    """
    from paste.deploy.converters import asbool
    secure = asbool(secure)
    include_ip = asbool(include_ip)
    if secret is None:
        secret = global_conf.get('secret')
    if not secret:
        raise ValueError(
            "You must provide a 'secret' (in global or local configuration)")
    return AuthTKTMiddleware(
        app, secret, cookie_name, secure, include_ip)
