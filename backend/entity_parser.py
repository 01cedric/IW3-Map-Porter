"""MapEnts navigation data; positionless entities stay positionless."""
from __future__ import annotations
import math
import re

TOKEN = re.compile(r'//[^\n]*|"((?:[^"\\]|\\.)*)"|([{}])', re.MULTILINE)


def vector(value):
    try:
        values = [float(x) for x in value.split()]
        return values if len(values) == 3 and all(math.isfinite(x) for x in values) else None
    except (ValueError, AttributeError): return None


def parse_entities(text):
    entities = []; props = None; key = None
    for match in TOKEN.finditer(text):
        if match.group(0).startswith('//'): continue
        quoted, brace = match.groups()
        if brace == '{': props = {}; key = None
        elif brace == '}':
            if props is not None:
                origin = vector(props.get('origin'))
                angles = vector(props.get('angles'))
                if angles is None:
                    try:
                        angle = float(props.get('angle', '0'))
                        if not math.isfinite(angle): angle = 0
                        angles = [-90, 0, 0] if angle == -1 else [90, 0, 0] if angle == -2 else [0, angle, 0]
                    except ValueError: angles = [0, 0, 0]
                classname = props.get('classname', '(no classname)')
                entities.append(dict(id=len(entities), classname=classname, targetname=props.get('targetname', ''),
                                     origin=origin, angles=angles, properties=props,
                                     is_spawn=('spawn' in classname and classname.startswith('mp_')) or classname.startswith('info_player_')))
            props = None; key = None
        elif quoted is not None and props is not None:
            value = quoted.replace('\\"', '"').replace('\\\\', '\\')
            if key is None: key = value
            else: props[key] = value; key = None
    return entities
