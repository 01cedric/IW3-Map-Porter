#!/usr/bin/env python3
"""Inflate a PS3 fastfile into the decompressed zone stream the emulator consumes.

    python3 extract_zone.py <file.ff> <zone.bin>
"""
import os, sys
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), '..', '..'))
from cod4porter.backend.v4_ffio import read_ps3_fastfile
z = read_ps3_fastfile(sys.argv[1]).zone
open(sys.argv[2], 'wb').write(z)
print(sys.argv[2], len(z), 'bytes')
