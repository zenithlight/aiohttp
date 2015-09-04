#!/usr/bin/env python3
"""Example for aiohttp.web basic server
"""

import asyncio
from aiohttp import web
from cProfile import Profile


@asyncio.coroutine
def index(request):
    return web.Response()


@asyncio.coroutine
def test(request):
    txt = 'Hello, ' + request.match_info['name']
    print('keep-alive', request.keep_alive)
    return web.Response(text=txt)


@asyncio.coroutine
def prepare(request):
    return web.Response(text='OK')


@asyncio.coroutine
def stop(request):
    loop.call_later(0.1, loop.stop)
    return web.Response(text='OK')


@asyncio.coroutine
def init(loop):
    app = web.Application(loop=loop)
    app.router.add_route('GET', '/', index)
    app.router.add_route('GET', '/prepare', prepare)
    app.router.add_route('GET', '/stop', stop)
    app.router.add_route('GET', '/test/{name}', test)

    handler = app.make_handler(keep_alive=15, timeout=None, debug=True)
    srv = yield from loop.create_server(handler, '127.0.0.1', 8080)
    return srv, app, handler


loop = asyncio.get_event_loop()
srv, app, handler = loop.run_until_complete(init(loop))
prof = Profile()
prof.enable()
try:
    loop.run_forever()
except KeyboardInterrupt:
    pass
finally:
    prof.disable()
    prof.dump_stats('bench.prof')
    loop.run_until_complete(handler.finish_connections())
