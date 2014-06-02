"""Parser is a generator function.

Parser receives data with generator's send() method and sends data to
destination DataQueue. Parser receives ParserBuffer and DataQueue objects
as a parameters of the parser call, all subsequent send() calls should
send bytes objects. Parser sends parsed `term` to desitnation buffer with
DataQueue.feed_data() method. DataQueue object should implement two methods.
feed_data() - parser uses this method to send parsed protocol data.
feed_eof() - parser uses this method for indication of end of parsing stream.
To indicate end of incoming data stream EofStream exception should be sent
into parser. Parser could throw exceptions.

There are three stages:

 * Data flow chain:

    1. Application creates StreamParser object for storing incoming data.
    2. StreamParser creates ParserBuffer as internal data buffer.
    3. Application create parser and set it into stream buffer:

        parser = HttpRequestParser()
        data_queue = stream.set_parser(parser)

    3. At this stage StreamParser creates DataQueue object and passes it
       and internal buffer into parser as an arguments.

        def set_parser(self, parser):
            output = DataQueue()
            self.p = parser(output, self._input)
            return output

    4. Application waits data on output.read()

        while True:
             msg = yield form output.read()
             ...

 * Data flow:

    1. asyncio's transport reads data from socket and sends data to protocol
       with data_received() call.
    2. Protocol sends data to StreamParser with feed_data() call.
    3. StreamParser sends data into parser with generator's send() method.
    4. Parser processes incoming data and sends parsed data
       to DataQueue with feed_data()
    5. Application received parsed data from DataQueue.read()

 * Eof:

    1. StreamParser recevies eof with feed_eof() call.
    2. StreamParser throws EofStream exception into parser.
    3. Then it unsets parser.

_SocketSocketTransport ->
   -> "protocol" -> StreamParser -> "parser" -> DataQueue <- "application"

"""
__all__ = ['EofStream', 'StreamBuffer', 'StreamProtocol',
           'DataQueue', 'LinesParser', 'ChunksParser']

import asyncio
import asyncio.streams
import collections
import functools
import inspect

BUF_LIMIT = 2**14
DEFAULT_LIMIT = 2**16


class EofStream(Exception):
    """eof stream indication."""


class StreamBuffer(asyncio.StreamReader):
    """StreamBuffer manages incoming bytes stream and protocol parsers.

    set_parser() sets current parser, it creates DataQueue object
    and sends ParserBuffer and DataQueue into parser generator.

    unset_parser() sends EofStream into parser and then removes it.
    """

    def __init__(self, *, loop=None, buf=None,
                 paused=True, limit=DEFAULT_LIMIT):
        super().__init__(limit, loop)

        self._output = None
        self._parser = None
        self._stream_paused = paused

    @property
    def output(self):
        return self._output

    def pause_stream(self):
        self._stream_paused = True

    def resume_stream(self):
        if self._paused and self._buffer.size <= self._limit:
            self._paused = False
            self._transport.resume_reading()

        self._stream_paused = False
        if self._parser and self._buffer:
            self.feed_data(b'')

    def exception(self):
        return self._exception

    def set_exception(self, exc):
        self._exception = exc

        waiter = self._waiter
        if waiter is not None:
            self._waiter = None
            if not waiter.cancelled():
                waiter.set_exception(exc)

        if self._output is not None:
            self._output.set_exception(exc)
            self._output = None
            self._parser = None

    def feed_eof(self):
        self._eof = True

        waiter = self._waiter
        if waiter is not None:
            self._waiter = None
            if not waiter.cancelled():
                waiter.set_exception(EofStream())

    def feed_data(self, data):
        """send data to current parser or store in buffer."""
        assert not self._eof, 'feed_data after feed_eof'

        if data is None:
            return

        self._buffer.extend(data)

        if self._parser and not self._stream_paused:
            waiter = self._waiter
            if waiter is not None:
                self._waiter = None
                if not waiter.cancelled():
                    waiter.set_result(False)
                else:
                    self._output.feed_eof()
                    self._output = None
                    self._parser = None
            elif self._parser is not None and self._parser.done():
                self._output.feed_eof()
                self._output = None
                self._parser = None

        if (self._transport is not None and not self._paused and
                len(self._buffer) > 2*self._limit):
            try:
                self._transport.pause_reading()
            except NotImplementedError:
                # The transport can't be paused.
                # We'll just have to buffer all data.
                # Forget the transport so we don't keep trying.
                self._transport = None
            else:
                self._paused = True

    def set_parser(self, parser):
        """set parser to stream. return parser's DataQueue."""
        if self._parser:
            self.unset_parser()

        output = DataQueue(self, loop=self._loop)
        if self._exception:
            output.set_exception(self._exception)
            return output

        # init parser
        self._output = output
        self._parser = asyncio.async(parser(output, self), loop=self._loop)

        self._parser.add_done_callback(
            functools.partial(self._unset_parser, output=output))

        return output

    def _unset_parser(self, parser, output=None):
        if parser is self._parser:
            exc = parser.exception()
            if exc and output:
                output.set_exception(exc)
            self._parser = None
            self._output = None            

    def unset_parser(self):
        """unset parser, send eof to the parser and then remove it."""
        if self._parser is None:
            return

        waiter = self._waiter
        if waiter is not None:
            self._waiter = None
            if not waiter.cancelled():
                waiter.set_exception(EofStream())
        else:
            self._parser.cancel()

        self._parser = None

    def read(self, size):
        """read() reads specified amount of bytes."""

        while True:
            if len(self._buffer) >= size:
                data = bytes(self._buffer[:size])
                del self._buffer[:size]
                return data

            if self._eof:
                raise EofStream()

            self._waiter = self._create_waiter('read')
            try:
                yield from self._waiter
            finally:
                self._waiter = None

    def readsome(self, size=None):
        """readsome() reads size of less amount of bytes."""

        while True:
            if len(self._buffer) > 0:
                if size is not None:
                    data = bytes(self._buffer[:size])
                    del self._buffer[:size]
                else:
                    data = bytes(self._buffer)
                    self._buffer.clear()

                return data

            if self._eof:
                raise EofStream()

            self._waiter = self._create_waiter('readsome')
            try:
                yield from self._waiter
            finally:
                self._waiter = None

    def readuntil(self, stop, limit=None, exc=ValueError):
        assert isinstance(stop, bytes) and stop, \
            'bytes is required: {!r}'.format(stop)

        stop_len = len(stop)

        while True:
            pos = self._buffer.find(stop)
            if pos >= 0:
                size = pos + stop_len
                if limit is not None and size > limit:
                    raise exc('Line is too long.')

                data = bytes(self._buffer[:size])
                del self._buffer[:size]
                return data

            else:
                if limit is not None and len(self._buffer) > limit:
                    raise exc('Line is too long.')

            if self._eof:
                raise EofStream()

            self._waiter = self._create_waiter('readuntil')
            try:
                yield from self._waiter
            finally:
                self._waiter = None

    def wait(self, size):
        """wait() waits for specified amount of bytes
        then returns data without changing internal buffer."""

        while True:
            if len(self._buffer) >= size:
                return self[:size]

            if self._eof:
                raise EofStream()

            self._waiter = self._create_waiter('wait')
            try:
                yield from self._waiter
            finally:
                self._waiter = None

    def waituntil(self, stop, limit=None, exc=ValueError):
        """waituntil() reads until `stop` bytes sequence."""
        assert isinstance(stop, bytes) and stop, \
            'bytes is required: {!r}'.format(stop)

        stop_len = len(stop)

        while True:
            pos = self._buffer.find(stop)
            if pos >= 0:
                size = pos + stop_len
                if limit is not None and size > limit:
                    raise exc('Line is too long. %s' % self._buffer)

                return bytes(self._buffer[:size])
            else:
                if limit is not None and len(self._buffer) > limit:
                    raise exc('Line is too long. %s' % self._buffer)

            if self._eof:
                raise EofStream()

            self._waiter = self._create_waiter('waituntil')
            try:
                yield from self._waiter
            finally:
                self._waiter = None

    def skip(self, size):
        """skip() skips specified amount of bytes."""

        while len(self._buffer) < size:
            if self._eof:
                raise EofStream()

            self._waiter = self._create_waiter('skip')
            try:
                yield from self._waiter
            finally:
                self._waiter = None

        del self._buffer[:size]

    def skipuntil(self, stop):
        """skipuntil() reads until `stop` bytes sequence."""
        assert isinstance(stop, bytes) and stop, \
            'bytes is required: {!r}'.format(stop)

        stop_len = len(stop)

        while True:
            stop_line = self._buffer.find(stop)
            if stop_line >= 0:
                size = stop_line + stop_len
                del self._buffer[:size]
                return

            if self._eof:
                raise EofStream()

            self._waiter = self._create_waiter('skip')
            try:
                yield from self._waiter
            finally:
                self._waiter = None


class StreamProtocol(asyncio.streams.FlowControlMixin, asyncio.Protocol):
    """Helper class to adapt between Protocol and StreamReader."""

    def __init__(self, *, loop=None, **kwargs):
        super().__init__(loop=loop)

        self.transport = None
        self.writer = None
        self.reader = StreamBuffer(loop=loop, **kwargs)

    def is_connected(self):
        return self.transport is not None

    def connection_made(self, transport):
        self.transport = transport
        self.reader.set_transport(transport)
        self.writer = asyncio.streams.StreamWriter(
            transport, self, self.reader, self._loop)

    def connection_lost(self, exc):
        self.transport = None

        if exc is None:
            self.reader.feed_eof()
        else:
            self.reader.set_exception(exc)

        super().connection_lost(exc)

    def data_received(self, data):
        self.reader.feed_data(data)

    def eof_received(self):
        self.reader.feed_eof()

    def _make_drain_waiter(self):
        if not self._paused:
            return ()
        waiter = self._drain_waiter
        if waiter is None or waiter.cancelled():
            waiter = asyncio.Future(loop=self._loop)
            self._drain_waiter = waiter
        return waiter


class DataQueue:
    """Base Parser class."""

    def __init__(self, stream, *, loop=None):
        self._stream = stream
        self._loop = loop
        self._buffer = collections.deque()
        self._eof = False
        self._waiter = None
        self._exception = None

    def at_eof(self):
        return self._eof

    def exception(self):
        return self._exception

    def set_exception(self, exc):
        self._exception = exc

        waiter = self._waiter
        if waiter is not None:
            self._waiter = None
            if not waiter.done():
                waiter.set_exception(exc)

    def feed_data(self, data):
        self._buffer.append(data)

        waiter = self._waiter
        if waiter is not None:
            self._waiter = None
            if not waiter.cancelled():
                waiter.set_result(True)

    def feed_eof(self):
        self._eof = True

        waiter = self._waiter
        if waiter is not None:
            self._waiter = None
            if not waiter.cancelled():
                waiter.set_result(False)

    @asyncio.coroutine
    def read(self):
        if self._exception is not None:
            raise self._exception

        self._stream.resume_stream()
        try:
            if not self._buffer and not self._eof:
                assert not self._waiter
                self._waiter = asyncio.Future(loop=self._loop)
                res = yield from self._waiter

            if self._buffer:
                return self._buffer.popleft()
            else:
                raise EofStream
        finally:
            self._stream.pause_stream()

    @asyncio.coroutine
    def readall(self):
        if self._exception is not None:
            raise self._exception

        result = []
        while True:            
            try:
                result.append((yield from self.read()))
            except EofStream:
                break

        return result


class LinesParser:
    """Lines parser.

    Lines parser splits a bytes stream into a chunks of data, each chunk ends
    with \\n symbol."""

    def __init__(self, limit=2**16, exc=ValueError):
        self._limit = limit
        self._exc = exc

    def __call__(self, out, buf):
        while True:
            out.feed_data(
                (yield from buf.readuntil(b'\n', self._limit, self._exc)))


class ChunksParser:
    """Chunks parser.

    Chunks parser splits a bytes stream into a specified
    size chunks of data."""

    def __init__(self, size=8196):
        self._size = size

    def __call__(self, out, buf):
        while True:
            out.feed_data((yield from buf.read(self._size)))
