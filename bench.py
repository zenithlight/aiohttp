import asyncio
import json
import cProfile

import aiohttp
import aiohttp.server
from aiohttp import web


class TestHttpServer(aiohttp.server.ServerHttpProtocol):

    def handle_request(self, message, payload):
        response = aiohttp.Response(self.writer, 200, message.version)

        text = b"{'message':'Hello, World!'}"
        response.add_header('CONTENT-TYPE', 'text/plain')
        response.add_header('CONTENT-LENGTH', str(len(text)))
        response.send_headers()
        response.write(text)
        response.write_eof()

        self.keep_alive(response.keep_alive())
        yield from ()


def index(req):
    return web.Response(
        body=b"{'message':'Hello, World!'}")
    yield from ()


@asyncio.coroutine
def init(loop, useweb=True):
    if useweb:
        app = web.Application()
        app.router.add_route('GET', '/json', handler=index)
        handler = app.make_handler(
            access_log=None, keep_alive=0, keep_alive_on=True)
    else:
        handler = lambda: TestHttpServer(
            access_log=None, keep_alive=0, keep_alive_on=True)

    srv = yield from loop.create_server(handler, '0.0.0.0', 8080)
    print("Server started at http://127.0.0.1:8080")
    return srv, handler


loop = asyncio.get_event_loop()
srv, handler = loop.run_until_complete(init(loop, False))

def main():
    try:
        loop.run_forever()
    except KeyboardInterrupt:
        # loop.run_until_complete(handler.finish_connections())
        pass


def main_pr():
    pr = cProfile.Profile()
    try:
        pr.enable()
        loop.run_forever()
    except KeyboardInterrupt:
        pr.disable()
        #loop.run_until_complete(handler.finish_connections())

    pr.dump_stats('pr.stats')


main()
