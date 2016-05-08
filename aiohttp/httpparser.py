"""httptools integration"""
import re
import httptools
from multidict import CIMultiDict

from . import errors, hdrs
from .protocol import RawRequestMessage, RawResponseMessage, DeflateBuffer
from .protocol import HttpVersion

VERSRE = re.compile('(\d+).(\d+)')


class HttpParser:

    need_body = True

    def __init__(self, response=False, compression=True):
        self._proto = HttpProtocol()
        self._response = response
        if response:
            self._parser = httptools.HttpResponseParser(self._proto)
        else:
            self._parser = httptools.HttpRequestParser(self._proto)
        self._processed = 0
        self._compression = compression
        self._headers_completed = False

    @classmethod
    def messageParser(cls):
        return cls()

    def payloadParser(self, message):
        return self

    def __call__(self, out, buf):
        self._out = out
        self._buf = buf
        self._processed = 0

        # payload decompression wrapper
        if (self.need_body and self._headers_completed and
                self._compression and self._proto.encoding):
            self._out = DeflateBuffer(out, self._proto.encoding)

        return self

    def throw(self, exc):
        raise exc

    def send(self, data):
        if data:
            try:
                self._parser.feed_data(data)
            except (httptools.HttpParserInvalidStatusError,
                    httptools.HttpParserInvalidMethodError,
                    httptools.HttpParserInvalidURLError):
                raise errors.BadStatusLine()
            except httptools.HttpParserError:
                raise errors.BadHttpMessage('')
            except httptools.HttpParserUpgrade as err:
                offset = err.args[0]
                self._buf.extend(data[offset:])
                self._proto.message_completed = True

        self._processed += len(data)

        if not self._headers_completed:
            if self._proto.headers_completed:
                # version
                ver = self._parser.get_http_version()
                match = VERSRE.match(ver)
                if match is None:
                    raise errors.BadStatusLine()
                version = HttpVersion(int(match.group(1)), int(match.group(2)))

                close_conn = not self._parser.should_keep_alive()
                headers, raw_headers, encoding = (
                    self._proto.headers,
                    self._proto.raw_headers, self._proto.encoding)

                if self._response:
                    status = self._parser.get_status_code()
                    if status < 100 or status > 999:
                        raise errors.BadStatusLine('')
                    self._out.feed_data(
                        RawResponseMessage(
                            version, status, '',
                            headers, raw_headers, close_conn, encoding),
                        self._processed)
                else:
                    method = self._parser.get_method().decode(
                        'utf-8', 'surrogateescape').upper()
                    self._out.feed_data(
                        RawRequestMessage(
                            method, self._proto.url, version, headers,
                            raw_headers, close_conn, encoding),
                        self._processed)

                self._out.feed_eof()

                self._out = None
                self._buf = None
                self._processed = 0
                self._headers_completed = True
                raise StopIteration
        else:
            if self.need_body:
                for chunk in self._proto.body:
                    self._out.feed_data(chunk, len(chunk))
                self._proto.body.clear()

            if self._proto.message_completed:
                self._out.feed_eof()
                raise StopIteration

    def __next__(self):
        if self._proto.message_completed:
            self.send(b'')

        self.send(self._buf.drain())


class HttpRequestParser(HttpParser):

    def __init__(self, compression=True):
        super().__init__(False, compression)


class HttpResponseParser(HttpParser):

    def __init__(self, compression=True):
        super().__init__(True, compression)

    def payloadParser(self, message, readall=False, response_with_body=True):
        self.need_body = response_with_body
        return self


class HttpProtocol:

    def __init__(self):
        self.path = ''
        self.close_conn = None
        self.encoding = None
        self.headers = CIMultiDict()
        self.raw_headers = []
        self.body = []

        self.headers_completed = False
        self.message_completed = False

    def on_header(self, bname, bvalue):
        name = bname.decode('utf-8', 'surrogateescape')
        value = bvalue.decode('utf-8', 'surrogateescape')

        # keep-alive and encoding
        if name == hdrs.CONNECTION:
            v = value.lower()
            if v == 'close':
                self.close_conn = True
            elif v == 'keep-alive':
                self.close_conn = False
        elif name == hdrs.CONTENT_ENCODING:
            enc = value.lower()
            if enc in ('gzip', 'deflate'):
                self.encoding = enc

        self.headers.add(name, value)
        self.raw_headers.append((bname, bvalue))

    def on_headers_complete(self):
        self.headers_completed = True

    def on_url(self, url):
        self.url = url.decode('utf-8', 'surrogateescape')

    def on_body(self, body):
        self.body.append(body)

    def on_message_complete(self):
        self.message_completed = True

    def on_chunk_header(self):
        pass

    def on_chunk_complete(self):
        pass
