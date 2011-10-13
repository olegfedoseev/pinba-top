#!/usr/bin/env python
# -*- coding: utf-8 -*-

from __future__ import with_statement

import os, sys, time, multiprocessing, itertools, math, errno, traceback, signal, resource
import sys, logging, logging.handlers, traceback
from optparse import OptionParser
from collections import deque

import gevent
from gevent.monkey import patch_all; patch_all()
from gevent.queue import JoinableQueue
from gevent.pool import Pool

from gevent_zeromq import zmq
from socketio import SocketIOServer

import daemon
#import daemon.pidlockfile

logger = logging.getLogger("pinba-top")

class groupby(dict):
    def __init__(self, seq, key=lambda x:x):
        for value in seq:
            self.setdefault(key(value), []).append(value)
    __iter__ = dict.iteritems

def avg(self, values, cnt=None):
    """ Вычисляет среднее значение """
    return sum(map(float, values)) / (len(values) if cnt is None else cnt)

class Frontend(object):
    def __init__(self, cwd):
        self.cwd = cwd

    def proxy(self, socketio, zmq_socket, session_id):
        """ Get data from zmq socket and sends it to socket.io """
        logger.info("Session %s is ready" % session_id)
        try:
            while socketio.connected():
                ts, top, rps, adv = zmq_socket.recv_pyobj()
                socketio.send(["top", {"time": ts, "top": top, "rps": rps, "adv": adv}])
        except zmq.ZMQError, e:
            logger.error("Session %s catch exception: %s, %s" % (session_id, type(e), e))    
        logger.info("Session %s is dead (Connected: %s)" % (session_id, socketio.connected()))

    def socket_io(self, socketio):
        """ Process socket.io connection """
        if socketio.session is None:
            return []
        session_id = socketio.session.session_id

        context = zmq.Context()
        sub = context.socket(zmq.SUB)
        sub.connect("ipc:///tmp/pinba-top.sock")
        sub.setsockopt(zmq.SUBSCRIBE, '')

        if socketio.on_connect():
            gevent.spawn(self.proxy, *(socketio, sub, session_id))
            
        while socketio.connected():
            message = socketio.recv()
            if message:
                logger.info("Session %s got message: %s" % (session_id, message))
        return []

    def html(self, path, environ, start_response):
        # Opera is evil!
        if "HTTP_USER_AGENT" in environ and "Opera" in environ["HTTP_USER_AGENT"]:
            start_response("404 Not Found", [])
            return ["<h1>Not Found</h1>"]
        
        if path is "":
            path = "pinba-top.html"

        if path in ["application.js", "pinba-top.html"]:
            try:
                data = open(os.path.join(self.cwd, path)).read()
            except Exception:
                logger.error('Wrong Path!')
                return not_found(start_response)

            start_response("200 OK", [("Content-Type", "text/javascript" if path.endswith(".js") else "text/html")])
            return [data]

        start_response("404 Not Found", [])
        return ["<h1>Not Found</h1>"]

    def __call__(self, environ, start_response):
        """ Process web-request """
        path = environ["PATH_INFO"].strip("/")
        return self.socket_io(environ["socketio"]) if path.startswith("socket.io") else self.html(path, environ, start_response)

"""
    Pinba Daemon - starts UDP server on port 30002, recives packets from php, every second sends them to decoder, and 
    then sends them in usable format via zmq socket.
"""
class PinbaTop(object):
    def __init__(self, cwd):
        self.is_running = False
        self.cwd = cwd
        self.requests = []
        self.queue = JoinableQueue()
        self.pub = None
        self.req = None
        self.servers = []

    def stop(self, signum, frame):
        self.is_running = False

    def top(self, requests):
        top = []
        servers = list(self.servers)
        for server, request in groupby(requests, key=lambda r: r[0][1]):
            if server in servers:
                servers.remove(server)
            else:
                self.servers.append(server)
            scripts = groupby([(r[0][2], r[1]) for r in request], key=lambda r: r[0])
            top.append((
                server, 
                sum([r[1][0] for r in request]),
                [(script, sum([t[1][0] for t in times]), sum([t[1][1] for t in times]), max([t[1][2]['max'] for t in times])) for script, times in scripts],
                sum([sum([t[1][0] for t in times]) for script, times in scripts if script == "/adplace.php"])
            ))
        for server in servers:
            top.append((server, 0, [], 0))

        top.sort(key=lambda r: r[1], reverse=True)
        return top

    def watcher(self):
        try:
            while self.is_running:
                gevent.sleep(1)        
            logger.info("Try to stop server...")
            self.server.stop()
        except Exception, e:
            logger.error(traceback.format_exc())
        logger.info("Watcher thread stops")

    def reader(self):
        """ every second collect requests from self.requests and send them to processing """
        logger.info("Reader thread starts")
        requests = None
        try:
            while self.is_running:
                try:
                    with gevent.Timeout(5, False):
                        ts, requests = self.sub.recv_json()
                    if requests is None:
                        continue
                    
                    top = self.top(requests)
                    rps = sum([r[1] for r in top])
                    adv = sum([r[3] for r in top])
                    self.pub.send_pyobj((ts, top, rps, adv))
                except zmq.ZMQError:
                    pass
        except KeyboardInterrupt:
            pass
        logger.info("Reader thread stops")

    def run(self, source):
        self.is_running = True
        signal.signal(signal.SIGTERM, self.stop)

        logger.info("Listen to %s" % source)
        
        context = zmq.Context()
        self.sub = context.socket(zmq.SUB)
        self.sub.connect(source)
        self.sub.setsockopt(zmq.SUBSCRIBE, '')

        self.pub = context.socket(zmq.PUB)
        self.pub.bind("ipc:///tmp/pinba-top.sock")
        self.pub.setsockopt(zmq.HWM, 60)
        self.pub.setsockopt(zmq.SWAP, 25000000)

        self.server = SocketIOServer(("", 8080), Frontend(self.cwd), resource="socket.io", log=StdErrWrapper())
        try:
            self.is_running = True
            logger.info('WebPinba started at %d' % time.time())
            gevent.spawn(self.watcher)
            self.workers = [gevent.spawn(self.reader)]
            self.server.serve_forever()
        except KeyboardInterrupt:            
            pass
        except Exception, e:
            logger.error(traceback.format_exc())

        logger.info("Daemon shutting down")
        self.is_running = False
        gevent.joinall(self.workers)
        logger.info("All workers stops")
        self.sub.close()
        self.pub.close()
        #context.term()
        logger.info("Sockets closed")
        logger.info("Daemon stops")


class StdErrWrapper:
    def write(self, s):
        logger.info(s.rstrip())

def main(options):
    daemon = PinbaTop(os.getcwd())
    try:
        logger.info("Starting daemon...")
        daemon.run(options.src)
    except KeyboardInterrupt:
        daemon.stop()

def cli_stop(options):
    logger.info("Stopping daemon...")
    try:
        pf = file(options.pid, "r")
        pid = int(pf.read().strip())
        pf.close()
    except IOError:
        sys.stderr.write("PID file at " + options.pid + " doesn't exist\n")
        return
    try:
        while True:
            os.kill(pid, signal.SIGTERM)
            time.sleep(0.5)
    except OSError, err:
        err = str(err)
        if "No such process" in err:
            if os.path.exists(options.pid):
                os.remove(options.pid)
            print "OK"
        else:
            print str(err)
            sys.exit(1)

def cli_start():
    parser = OptionParser()
    parser.add_option("-d", "--daemon", dest="daemon", help="run as daemon", action="store_true", default=False)
    parser.add_option("-s", "--src", dest="src", help="source address for zmq data from pinba2zmq", type="string")
    parser.add_option("-l", "--log", dest="log", help="log file path", type="string", default="/var/log/pinba-top.log")
    parser.add_option("-P", "--pid", dest="pid", help="pid file path", type="string", default="/var/run/pinba-top.pid")
    (options, args) = parser.parse_args()

    formatter = logging.Formatter("%(asctime)s - %(levelname)s - %(message)s")
    fh = logging.FileHandler(options.log)
    ch = logging.StreamHandler()
    fh.setFormatter(formatter)
    ch.setFormatter(formatter)
    logger.setLevel(logging.INFO)

    #pid = daemon.pidlockfile.TimeoutPIDLockFile(options.pid, 2)
    if options.daemon:
        if len(args) == 0:
            # pidfile=pid, 
            files = range(resource.getrlimit(resource.RLIMIT_NOFILE)[1] + 2)
            with daemon.DaemonContext(files_preserve=files, stdout=sys.stdout, stderr=sys.stderr, working_directory=os.getcwd()):
                logger.addHandler(fh)
                main(options)
        elif len(args) == 1 and args[0] == 'stop':
            sys.exit(cli_stop(options))
    else:
        logger.setLevel(logging.DEBUG)
        logger.addHandler(ch)
        main(options)
    
if __name__ == "__main__":
    cli_start()