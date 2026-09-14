#!/usr/bin/env python3
"""Double-fork a command so it survives this shell. macOS has no setsid(1).

A parent shell or automation may kill its background children when it exits, so
anything measured in tens of minutes has to leave the process group.
Verify with `ps -o ppid` — a real detach reads PPID 1.
"""
import os
import sys
log = sys.argv[1]
cmd = sys.argv[2:]
if os.fork(): sys.exit()
os.setsid()
if os.fork(): sys.exit()
fd = os.open(log, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o644)
os.dup2(fd, 1); os.dup2(fd, 2)
os.close(0)
os.open(os.devnull, os.O_RDONLY)
os.execvp(cmd[0], cmd)
