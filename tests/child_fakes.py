"""Fake inspect_job functions for the process-pool tests. Kept free of heavy
imports so spawned children start fast and fit under a memory limit."""

import os
import time


def slow_inspect(data, max_file_mb, filename_hint):
    with open(data.decode(), "w") as f:  # data is a path: the test reads the child's pid
        f.write(str(os.getpid()))
    time.sleep(30)


def hog_inspect(data, max_file_mb, filename_hint):
    block = bytearray(8 * 2**30)  # far past the child's address-space limit
    return len(block)


def echo(value):
    return value


def sleepy(seconds):
    time.sleep(seconds)
    return seconds
