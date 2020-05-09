# Smartnet trunking module
#
# Copyright 2020 Graham J. Norbury - gnorbury@bondcar.com
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
#

import sys
import ctypes
import time
import json
from log_ts import log_ts
from collections import deque

OSW_QUEUE_SIZE = 3      # Some OSWs can be 3 commands long

class rx_ctl(object):
    def __init__(self, debug=0, frequency_set=None, slot_set=None, chans={}):
        self.frequency_set = frequency_set
        self.debug = debug
        self.osw_parser = osw_parser(debug=debug, config=chans[0])
        self.receivers = {}
        self.config = chans

    def post_init(self):
       pass
       #for rx_id in self.receivers:
       #    self.receivers[rx_id].post_init()

    def add_receiver(self, msgq_id):
        self.receivers[msgq_id] = msgq_id # TODO: fill this placeholder

    def process_qmsg(self, msg):
        m_proto = ctypes.c_int16(msg.type() >> 16).value  # upper 16 bits of msg.type() is signed protocol
        if m_proto != 2: # Smartnet m_proto=2
            return

        m_type = ctypes.c_int16(msg.type() & 0xffff).value
        m_rxid = int(msg.arg1()) >> 1
        m_ts = float(msg.arg2())

        if (m_type == -1):  # Timeout
            pass
        elif (m_type == 0): # OSW
            s = msg.to_string()

            osw_addr = (ord(s[0]) << 8) + ord(s[1])
            osw_grp  =  ord(s[2])
            osw_cmd  = (ord(s[3]) << 8) + ord(s[4])
            self.osw_parser.enqueue(osw_addr, osw_grp, osw_cmd)

            if self.debug >= 11:
                sys.stderr.write("%s SMARTNET OSW (%d,%d,0x%03x)\n" % (log_ts.get(), osw_addr, osw_grp, osw_cmd))

class osw_parser(object):
    def __init__(self, debug, config):
        self.debug = debug
        self.config = config
        self.osw_q = deque(maxlen=OSW_QUEUE_SIZE)

    def is_chan(self, cmd):
        band = self.config['bandplan'][:3]
        if band == "800":
            if (cmd >= 0 and cmd <= 0x2F7) or (cmd >= 0x32f and cmd <= 0x33F) or (cmd >= 0x3c1 and cmd <= 0x3FE) or cmd == 0x3BE:
                return True
        elif band == "900":
            if cmd >= 0 and cmd <= 0x1DE:
                return True
        elif band == "400":
            if (cmd >= int(self.config['bp_offset']) and cmd <= (int(self.config['bp_offset']) + 380)):
                return True
            else:
                return False
        return False

    def get_freq(self, cmd):
        freq = 0.0
        band = self.config['bandplan'][:3]
        subtype = self.config['bandplan'][3:len(self.config['bandplan'])].lower().lstrip("_-:")

        if band == "800":
            if cmd <= 0x2CF:
                if subtype == "reband" and cmd >= 0x1B8 and cmd <= 0x22F: # REBAND site
                    freq = 851.0250 + (0.025 * (cmd - 0x1B8))
                elif subtype == "splinter" and cmd <= 0x257:              # SPLINTER site
                    freq = 851.0 + (0.025 * cmd)
                else:
                    freq = 851.0125 + (0.025 * cmd)                       # STANDARD site
            elif cmd <= 0x2f7:
                freq = 866.0000 + (0.025 * (cmd - 0x2D0))
            elif cmd >= 0x32F and cmd <= 0x33F:
                freq = 867.0000 + (0.025 * (cmd - 0x32F))
            elif cmd == 0x3BE:
                freq = 868.9750
            elif cmd >= 0x3C1 and cmd <= 0x3FE:
                freq = 867.4250 + (0.025 * (cmd - 0x3C1))

        elif band == "900":
            freq = 935.0125 + (0.0125 * cmd)

        elif band == "400":
            bp_offset = self.config['bp_offset']
            bp_high = self.config['bp_high']
            bp_base = self.config['bp_base']
            bp_spacing = self.config['bp_spacing']
            high_cmd = bp_offset + bp_high - bp_base / bp_spacing

            if (cmd >= bp_offset) and (cmd < high_cmd):
                freq = bp_base + (bp_spacing * (cmd - bp_offset ))

        if freq > 0.0:
            sys.stderr.write("%s SMARTNET FREQ (0x%03x): %f\n" % (log_ts.get(), cmd, freq))

        return freq

    def enqueue(self, addr, grp, cmd):
        if self.is_chan(cmd):
            freq = self.get_freq(cmd)
        else:
            freq = 0.0
        self.osw_q.append((addr, (grp != 0), cmd, self.is_chan(cmd), freq))
        self.process()

    def process(self):
        if len(self.osw_q) < OSW_QUEUE_SIZE:
            return

        osw_addr, osw_grp, osw_cmd, osw_ch, osw_f = self.osw_q.popleft()
        if osw_ch:
            sys.stderr.write("%s SMARTNET OSW (%d,%s,0x%03x,%f)\n" % (log_ts.get(), osw_addr, osw_grp, osw_cmd, osw_f))
        else:
            sys.stderr.write("%s SMARTNET OSW (%d,%s,0x%03x)\n" % (log_ts.get(), osw_addr, osw_grp, osw_cmd))

