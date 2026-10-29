#!/usr/bin/env python3
#
# Copyright (c) Facebook, Inc. and its affiliates.
#
# This software may be used and distributed according to the terms of the
# GNU General Public License version 2.

import argparse
import datetime
import math
import time
import sys
import os

import errno

def h(x):
    """Translates a number of bytes to a human-readable string."""
    order = 0
    suffix = ['', 'k', 'M', 'G', 'T']
    max_order = len(suffix) - 1
    while abs(x) > 1024 and order < max_order:
        x /= 1024.0
        order += 1
    return '%.2f%s' % (x, suffix[order])


def log(string):
    """Logs timestamped information to stdout."""
    ts = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    print(ts + ' ' + string)


class Cgroup(object):
    def __init__(self, path):
        self.path = path
        self.pressure()  # Ensure psi is available in Linux 4.20+
        self.clear_memory_high()  # No memory.high cap; reclaim is done via memory.reclaim

    # Parsing the memory pressure file format:
    #
    # some avg10=0.00 avg60=0.00 avg300=0.00 total=0
    # full avg10=0.00 avg60=0.00 avg300=0.00 total=0

    def pressure(self):
        """Returns the 'some' avg10 value (percent)."""
        return float(self.readlines("memory.pressure")[0].split()[1].split('=')[1])

    def total(self):
        """Returns the cumulative 'some' stall time (microseconds)."""
        return int(self.readlines("memory.pressure")[0].split()[4].split('=')[1])

    # Memory reclaim

    def clear_memory_high(self):
        """Remove any limit left behind by the old memory.high Senpai."""
        if self.read("memory.high").strip() != "max":
            log('memory.high was set (old Senpai run?), resetting to max.')
            self.write("memory.high", "max")

    def reclaim(self, nbytes):
        """Proactively reclaims nbytes via memory.reclaim (Linux 5.19+).

        Returns False if the kernel couldn't reclaim the full amount (EAGAIN).
        """
        try:
            self.write("memory.reclaim", str(nbytes))
            return True
        except OSError as e:
            if e.errno == errno.EAGAIN:
                return False
            raise

    # Cgroupfs IO

    def read_current(self):
        return int(self.read("memory.current"))

    def read(self, filename):
        with open(os.path.join(self.path, filename)) as f:
            return f.read()

    def readlines(self, filename):
        with open(os.path.join(self.path, filename)) as f:
            return f.readlines()

    def write(self, filename, string):
        with open(os.path.join(self.path, filename), 'w') as f:
            f.write(string)


class Senpai(object):
    def __init__(self, conf):
        self.conf = conf
        log('Configuration:')
        for key, val in vars(conf).items():
            log(f'  {key} = {val}')

        self.cgroup = Cgroup(self.conf.cgpath)
        self.last_total = self.cgroup.total()

    def run(self):
        while True:
            time.sleep(self.conf.interval)  # tick every INTERVAL seconds
            self.tick()

    def tick(self):
        total = self.cgroup.total()
        delta = total - self.last_total
        self.last_total = total

        # PSI_some over the last period, in percent:
        # stall time (us) / period length (us) * 100
        psi_some = 100.0 * delta / (self.conf.interval * 1e6)

        # TMO formula:
        # reclaim = current * reclaim_ratio * max(0, 1 - psi_some / psi_threshold)
        current_mem = self.cgroup.read_current()
        factor = max(0.0, 1 - psi_some / self.conf.psi_threshold)
        reclaim = current_mem * self.conf.reclaim_ratio * factor

        reclaim = min(reclaim, current_mem * self.conf.max_reclaim)       # cap at MAX_RECLAIM per period
        reclaim = min(reclaim, max(0, current_mem - self.conf.min_size))  # don't go below MIN_SIZE

        reclaim = int(reclaim) & ~4095  # round down to 4K pages

        status = 'skip'  # nothing to reclaim
        if reclaim > 0:
            if self.conf.dry_run:  # just log it, don't reclaim
                status = 'dry'
            elif self.cgroup.reclaim(reclaim):  # kernel reclaimed the full amount
                status = 'ok'
            else:
                status = 'partial'  # kernel couldn't reclaim the full amount

        log ('current=%s avg10_psi=%f psi_some=%.4f total=%d delta=%d factor=%.3f reclaim=%d status=%s' %
             (h(current_mem), self.cgroup.pressure(), psi_some, total, delta, factor, reclaim, status))


parser = argparse.ArgumentParser(description="""
Senpai takes a cgroup and proactively reclaims its memory using psi
memory pressure data, following the TMO (Transparent Memory Offloading)
approach. No memory.high limit is set; memory is reclaimed one-shot
through memory.reclaim (Linux 5.19+).

Every INTERVAL seconds, senpai computes PSI_some (percent of the period
in which some task stalled on memory) and reclaims:

    current * RECLAIM_RATIO * max(0, 1 - PSI_some / PSI_THRESHOLD)

bytes. Reclaim stops once pressure reaches PSI_THRESHOLD, is capped at
MAX_RECLAIM of current memory per period, and never shrinks the cgroup
below MIN_SIZE bytes. With --dry-run, the amount is logged but not
reclaimed.
""",

formatter_class=argparse.RawDescriptionHelpFormatter)

parser.add_argument('cgpath', type=str)
parser.add_argument('--min-size', type=int, default=100 << 20)
parser.add_argument('--interval', type=int, default=6)
parser.add_argument('--psi-threshold', type=float, default=0.1)
parser.add_argument('--reclaim-ratio', type=float, default=0.0005)
parser.add_argument('--max-reclaim', type=float, default=0.01)
parser.add_argument('--dry-run', action='store_true')

conf = parser.parse_args()
senpai = Senpai(conf)
senpai.run()
