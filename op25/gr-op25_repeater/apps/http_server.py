# Copyright 2017, 2018 Max H. Parke KA1RBI
# Copyright 2026  Graham J. Norbury
#
# This file is part of OP25
#
# OP25 is free software; you can redistribute it and/or modify it
# under the terms of the GNU General Public License as published by
# the Free Software Foundation; either version 3, or (at your option)
# any later version.
#
# OP25 is distributed in the hope that it will be useful, but WITHOUT
# ANY WARRANTY; without even the implied warranty of MERCHANTABILITY
# or FITNESS FOR A PARTICULAR PURPOSE. See the GNU General Public
# License for more details.
#
# You should have received a copy of the GNU General Public License
# along with OP25; see the file COPYING. If not, write to the Free
# Software Foundation, Inc., 51 Franklin Street, Boston, MA
# 02110-1301, USA.

import sys
import os
import time
import json
import socket
import traceback
import threading
import uuid
import collections
import urllib.request
import urllib.error

from gnuradio import gr
from waitress.server import create_server

import gnuradio.op25_repeater as op25_repeater

my_input_q = None
my_output_q = None
my_recv_q = None
my_port = None
my_uuids = []
q_mutex = threading.Lock()
u_mutex = threading.Lock()

# Where the persistent "server" process (docker/config-api/app.py) listens
# for pushed state -- see state_pusher below. Both processes are guaranteed
# co-located in the same container (docker/op25/supervisord.conf runs them
# side by side), so this is loopback-only and not worth making configurable.
SERVER_INGEST_URL = "http://127.0.0.1:8092/internal/state_push"


"""
op25's internal command/RPC server -- reachable only from the "server"
process on 127.0.0.1 (see db_config.py's terminal_type), never directly by
browsers. Everything browser-facing (static files, SSE, the public POST /
a browser's New UI actually calls) lives in "server" now, which is never
restarted for topology changes the way this process is -- see
supervisord.conf for the restart-domain split this exists to support.
TODO: make less fake
"""

def post_req(environ, start_response, postdata):
    global my_input_q, my_output_q, my_recv_q, my_port, q_mutex, u_mutex
    valid_req = False
    num_req = 0
    post_uuid = str(uuid.uuid4())
    with u_mutex:
        my_uuids.append(post_uuid)
    try:
        data = json.loads(postdata)
        for d in data:
            num_req += 1
            d['uuid'] = post_uuid
            msg = gr.message().make_from_string(json.dumps(d), -2, d['arg1'], d['arg2'])
            #sys.stderr.write("post_req: req=%s\n" % json.dumps(d))
            if not my_output_q.full_p():
                my_output_q.insert_tail(msg)
        valid_req = True
    except (json.JSONDecodeError, KeyError, TypeError):
        sys.stderr.write('post_req: error processing input: %s\n%s\n' % (postdata, traceback.format_exc()))

    # Each POST_REQ should result in one Response
    resp_msg = []
    valid_resp = False
    t_expiry = time.time() + 0.2
    while valid_req and not valid_resp and (time.time() < t_expiry):  # wait for a message or timeout
        if (len(my_recv_q) > 0):
            with u_mutex:
                with q_mutex:
                    m_uuid = my_recv_q[0][0]            # inspect uuid of first message
                    if m_uuid == post_uuid:             # message for me can be handled
                        (m_uuid, msg) = my_recv_q.popleft()
                        resp_msg = msg
                        valid_resp = True
                    elif m_uuid not in my_uuids:
                        my_recv_q.popleft()             # orphaned message can be discarded
                        sys.stderr.write("post_req: discard m_uuid=%s [%s]\n" % (m_uuid, msg))
                    else:
                        pass                            # message for someone else
        time.sleep(0)                                   # yield to other threads
    if not valid_req:
        resp_msg = []
    with u_mutex:
        try:
            my_uuids.remove(post_uuid)
        except (ValueError):
            pass
    status = '200 OK'
    content_type = 'application/json'
    output = json.dumps(resp_msg)
    #sys.stderr.write("post_req: resp=%s\n" % output)
    return status, content_type, output

def http_request(environ, start_response):
    if environ['REQUEST_METHOD'] == 'POST':
        postdata = environ['wsgi.input'].read()
        status, content_type, output = post_req(environ, start_response, postdata)
    else:
        # GET/static serving lives in "server" now -- this process has no
        # browser-facing surface left at all.
        status = '404 NOT FOUND'
        content_type = 'text/plain'
        output = status
        sys.stderr.write('http_request: unexpected input %s %s\n' % (environ['REQUEST_METHOD'], environ['PATH_INFO']))

    response_headers = [('Content-type', content_type),
                        ('Content-Length', str(len(output)))]
    start_response(status, response_headers)

    if sys.version[0] > '2':
        if type(output) is str:
            output = output.encode()

    return [output]

def _synth_update_request():
    # Internal equivalent of post_req() receiving a POST body of
    # [{"command":"update","arg1":0,"arg2":0}] -- same push-onto-
    # my_output_q / poll-my_recv_q-by-uuid mechanism, just synthesized by
    # state_pusher() on a timer instead of triggered by an actual HTTP
    # request. Reuses the exact same state-computation path on the other
    # end (multi_rx.py's process_qmsg() 'update' branch) -- nothing about
    # how the JSON gets built changes, only who asks for it.
    global my_output_q, my_recv_q, q_mutex, u_mutex
    post_uuid = str(uuid.uuid4())
    with u_mutex:
        my_uuids.append(post_uuid)
    d = {"command": "update", "arg1": 0, "arg2": 0, "uuid": post_uuid}
    msg = gr.message().make_from_string(json.dumps(d), -2, d['arg1'], d['arg2'])
    if not my_output_q.full_p():
        my_output_q.insert_tail(msg)

    resp_msg = None
    t_expiry = time.time() + 0.5
    while resp_msg is None and time.time() < t_expiry:
        if len(my_recv_q) > 0:
            with u_mutex:
                with q_mutex:
                    m_uuid = my_recv_q[0][0]
                    if m_uuid == post_uuid:
                        (m_uuid, msg) = my_recv_q.popleft()
                        resp_msg = msg
                    elif m_uuid not in my_uuids:
                        my_recv_q.popleft()
                    else:
                        pass
        time.sleep(0)
    with u_mutex:
        try:
            my_uuids.remove(post_uuid)
        except (ValueError):
            pass
    return resp_msg

class state_pusher(threading.Thread):
    # Replaces sse_broadcaster: instead of fanning state out to in-process
    # SSE clients (there are none here anymore -- SSE lives entirely in
    # "server" now), this pushes state OUT to "server"'s ingest endpoint.
    # Runs unconditionally on every tick, since this side has no way to
    # know whether "server" currently has any browsers connected -- a
    # fixed, cheap cost (one _synth_update_request() call/sec, the same
    # cost this process always paid for this), not an oversight.
    def __init__(self, interval=1.0, **kwds):
        threading.Thread.__init__(self, **kwds)
        self.setDaemon(1)
        self.interval = interval
        self.start()

    def run(self):
        while True:
            time.sleep(self.interval)
            resp = _synth_update_request()
            if resp is None:
                continue    # op25 didn't reply within the timeout this tick -- retry next tick
            data = json.dumps(resp).encode()
            req = urllib.request.Request(SERVER_INGEST_URL, data=data, method="POST",
                                          headers={"Content-Type": "application/json"})
            try:
                urllib.request.urlopen(req, timeout=2).close()
            except (urllib.error.URLError, OSError):
                pass    # "server" not up yet / mid-restart -- next tick retries

def application(environ, start_response):
    try:
        result = http_request(environ, start_response)
    except Exception:
        sys.stderr.write('application: request failed:\n%s\n' % traceback.format_exc())
        sys.exit(1)
    return result

def process_qmsg(msg):
    if msg.type() == -4:                # we are only interested in JSON messages
      try:
        m_uuid = "no-uuid"
        m = json.loads(msg.to_string()) # incoming json formatted message is a list of dictionaries
        if len(m) == 0:
            return
        if "uuid" in m[0] and m[0]['uuid'] is not None: # first dict in list will contain uuid of originator
            m_uuid = m[0]['uuid']
            m[0].pop('uuid', None)
        my_recv_q.append((m_uuid, m))   # collections.deque automatically limits queue size to maxlen items
      except (KeyError, ValueError):
        sys.stderr.write("process_qmsg: improperly formatted message=%s\n" % json.dumps(m))

class http_server(object):
    def __init__(self, input_q, output_q, endpoint, **kwds):
        global my_input_q, my_output_q, my_recv_q, my_port
        host, port = endpoint.split(':')
        if my_port is not None:
            raise AssertionError('this server is already active on port %s' % my_port)
        my_input_q = input_q
        my_output_q = output_q
        my_port = int(port)

        my_recv_q = collections.deque(maxlen = 10)
        self.q_watcher = queue_watcher(my_input_q, process_qmsg)
        self.state_pusher = state_pusher()

        try:
            self.server = create_server(application, host=host, port=my_port, threads=6)
        except (OSError, ValueError):
            sys.stderr.write('Failed to create http terminal server\n%s\n' % traceback.format_exc())
            sys.exit(1)

    def run(self):
        self.server.run()

class queue_watcher(threading.Thread):
    def __init__(self, msgq,  callback, **kwds):
        threading.Thread.__init__ (self, **kwds)
        self.setDaemon(1)
        self.msgq = msgq
        self.callback = callback
        self.keep_running = True
        self.start()

    def run(self):
        while(self.keep_running):
            if not self.msgq.empty_p(): # check queue before trying to read a message to avoid deadlock at startup
                msg = self.msgq.delete_head()
                if msg is not None:
                    self.callback(msg)
                else:
                    self.keep_running = False
            else: # empty queue
                time.sleep(0.01)
