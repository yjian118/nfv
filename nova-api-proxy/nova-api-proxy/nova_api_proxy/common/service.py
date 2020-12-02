#
#    Licensed under the Apache License, Version 2.0 (the "License"); you may
#    not use this file except in compliance with the License. You may obtain
#    a copy of the License at
#
#         http://www.apache.org/licenses/LICENSE-2.0
#
#    Unless required by applicable law or agreed to in writing, software
#    distributed under the License is distributed on an "AS IS" BASIS, WITHOUT
#    WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied. See the
#    License for the specific language governing permissions and limitations
#    under the License.
#
# Copyright (c) 2015-2018 Wind River Systems, Inc.
#

import errno
import eventlet
import eventlet.greenio
import eventlet.wsgi
import os
import webob.dec
import webob.exc

from eventlet.green import socket
from eventlet.green import ssl

from oslo_config import cfg
from oslo_log import log as logging

from nova_api_proxy.common.exception import ProxyException


LOG = logging.getLogger(__name__)

URL_LENGTH_LIMIT = 50000


server_opts = [
    cfg.StrOpt('wsgi_log_format',
               default='%(client_ip)s "%(request_line)s" status: %('
                       'status_code)s len: %(body_length)s time: %(wall_'
                       'seconds).7f',
               help='A python format string that is used as the template to '
                    'generate log lines. The following values can be formatted'
                    ' into it: client_ip, date_time, request_line, status_code'
                    ', body_length, wall_seconds.'),
    cfg.StrOpt('ssl_ca_file',
               help="CA certificate file to use to verify "
                    "connecting clients"),
    cfg.StrOpt('ssl_cert_file',
               help="SSL certificate of API server"),
    cfg.StrOpt('ssl_key_file',
               help="SSL private key of API server"),
    cfg.IntOpt('tcp_keepidle',
               default=600,
               help="Sets the value of TCP_KEEPIDLE in seconds for each "
                    "server socket. Not supported on OS X."),
    cfg.IntOpt('pool_size',
               default=1000,
               help="Size of the pool of greenthreads used by wsgi"),
    cfg.IntOpt('max_header_line',
               default=16384,
               help="Maximum line size of message headers to be accepted. "
                    "max_header_line may need to be increased when using "
                    "large tokens (typically those generated by the "
                    "Keystone v3 API with big service catalogs)."),
    cfg.IntOpt('client_socket_timeout', default=900,
               help="Timeout for client connections' socket operations. "
                    "If an incoming connection is idle for this number of "
                    "seconds it will be closed. A value of '0' means "
                    "wait forever."),
]

CONF = cfg.CONF
CONF.register_opts(server_opts)


class Request(webob.Request):
    pass


class WritableLogger(object):
    """A thin wrapper that responds to `write` and logs."""

    def __init__(self, logger, level=logging.INFO):
        self.logger = logger
        self.level = level

    def write(self, msg):
        self.logger.debug(msg.rstrip())


class Server(object):
    """Server class to manage multiple WSGI sockets and applications."""

    def __init__(self, name, app, host='0.0.0.0', port=0,
                 protocol=eventlet.wsgi.HttpProtocol, use_ssl=False,
                 backlog=128, max_url_len=URL_LENGTH_LIMIT):
        """Initialize, but do not start, a WSGI server.

        :param app: The name of WSGI application.
        :param app: The WSGI application to serve.
        :param host: IP address to serve the application.
        :param port: Port number to server the application.
        :returns: None
        :raises:
        """
        eventlet.wsgi.MAX_HEADER_LINE = CONF.max_header_line
        self._server = None
        self.name = name
        self.app = app
        self._protocol = protocol
        self._pool = eventlet.greenpool.GreenPool(CONF.pool_size)
        self._use_ssl = use_ssl
        self._max_url_len = max_url_len
        self.client_socket_timeout = CONF.client_socket_timeout or None
        self._wsgi_logger = WritableLogger(LOG)

        if backlog < 1:
            raise ProxyException('The backlog must be more than 1')

        bind_addr = (host, port)
        try:
            info = socket.getaddrinfo(bind_addr[0],
                                      bind_addr[1],
                                      socket.AF_UNSPEC,
                                      socket.SOCK_STREAM)[0]
            family = info[0]
            bind_addr = info[-1]
        except Exception:
            family = socket.AF_INET

        self._socket = eventlet.listen(bind_addr, family, backlog=backlog)
        (self.host, self.port) = self._socket.getsockname()[0:2]
        LOG.info("%(name)s listening on %(host)s:%(port)s" % self.__dict__)

    def _setup_ssl(self):
        LOG.info("%(name)s setup ssl" % self.__dict__)
        try:
            ca_file = CONF.ssl_ca_file
            cert_file = CONF.ssl_cert_file
            key_file = CONF.ssl_key_file

            if cert_file and not os.path.exists(cert_file):
                raise RuntimeError("Unable to find cert_file : %s" % cert_file)

            if ca_file and not os.path.exists(ca_file):
                raise RuntimeError("Unable to find ca_file : %s" % ca_file)

            if key_file and not os.path.exists(key_file):
                raise RuntimeError("Unable to find key_file : %s" % key_file)

            if self._use_ssl and (not cert_file or not key_file):
                raise RuntimeError("When running server in SSL mode, "
                                   "you must specify both a cert_file and key_"
                                   "file option value in your configuration "
                                   "file")
            ssl_kwargs = {
                'server_side': True,
                'certfile': cert_file,
                'keyfile': key_file,
                'cert_reqs': ssl.CERT_NONE,
            }

            if CONF.ssl_ca_file:
                ssl_kwargs['ca_certs'] = ca_file
                ssl_kwargs['cert_reqs'] = ssl.CERT_REQUIRED

            self._socket = eventlet.wrap_ssl(self._socket, **ssl_kwargs)

        except socket.error:
            LOG.error("Failed to start %(name)s on %(host)s :%(port)s with SSL"
                      " support" % self.__dict__)

    def start(self):
        """Start serving a WSGI application.

        :returns: None
        """
        self._socket.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self._socket.setsockopt(socket.SOL_SOCKET, socket.SO_KEEPALIVE, 1)
        if hasattr(socket, 'TCP_KEEPIDLE'):
            self._socket.setsockopt(socket.IPPROTO_TCP, socket.TCP_KEEPIDLE,
                                    CONF.tcp_keepidle)

        if self._use_ssl:
            self._setup_ssl()

        try:
            eventlet.wsgi.server(self._socket, self.app,
                                 custom_pool=self._pool,
                                 url_length_limit=self._max_url_len,
                                 log=self._wsgi_logger,
                                 protocol=self._protocol,
                                 log_format=CONF.wsgi_log_format,
                                 debug=CONF.debug,
                                 socket_timeout=self.client_socket_timeout)
        except socket.error as err:
            if err[0] != errno.EINVAL:
                raise
        self._pool.waitall()

    def stop(self):
        """Stop this server.

        This is not a very nice action, as currently the method by which a
        server is stopped is by killing its eventlet.

        :returns: None

        """
        LOG.info("Stopping WSGI server.")
        # Resize pool to stop new requests from being processed
        self._pool.resize(0)


class Application(object):

    @classmethod
    def factory(cls, global_config, **local_config):
        """Used for paste app factories in paste.deploy config files.
       """
        return cls(**local_config)

    def __call__(self, environ, start_response):
        raise NotImplementedError('You must implement __call__')


class Middleware(Application):
    """
    Base WSGI middleware wrapper. These classes require an application to be
    initialized that will be called next.  By default the middleware will
    simply call its wrapped app, or you can override __call__ to customize its
    behavior.
    """
    @classmethod
    def factory(cls, global_config, **local_config):
        """Used for paste app factories in paste.deploy config files.

        Any local configuration (that is, values under the [filter:APPNAME]
        section of the paste config) will be passed into the `__init__` method
        as kwargs.
        """
        def _factory(app):
            return cls(app, global_config, **local_config)
        return _factory

    def __init__(self, application):
        self.application = application

    def process_request(self, req):
        """
        Called on each request.

        If this returns None, the next application down the stack will be
        executed. If it returns a response then that response will be returned
        and execution will stop here.

        """
        return None

    def process_response(self, response):
        """Do whatever you'd like to the response."""
        return response

    @webob.dec.wsgify
    def __call__(self, req):
        response = self.process_request(req)
        if response:
            return response
        response = req.get_response(self.application)
        return self.process_response(response)
