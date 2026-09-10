"""Native IW3 expression records for a controller-scrolled map viewport.

All rows retain native focus eligibility. Rows outside the viewport are moved
outside the draw area; onFocus moves the viewport before the next paint.
"""
import struct

SCROLL_VAR = 'iw3_map_scroll'
VISIBLE_ROWS = 16
RECT_Y = 0x1B4


def op(value):
    return (0, value, 0)


def number(value):
    if isinstance(value, float):
        return (1, 1, struct.unpack('>I', struct.pack('>f', value))[0])
    return (1, 0, value & 0xFFFFFFFF)


def scroll():
    return [op(58), (1, 2, SCROLL_VAR), op(1)]


def row_y(index, base=34):
    # base + 20 * (index - scroll) + 10000 * (outside viewport)
    return [op(16), number(base), op(5), number(20), op(2), op(16), number(index), op(6), *scroll(), op(1),
            op(5), number(10000), op(2), op(16), number(index), op(8), *scroll(),
            op(15), number(index), op(11), op(16), *scroll(), op(5), number(VISIBLE_ROWS), op(1), op(1)]


def thumb_y(count):
    height = 320.0 * VISIBLE_ROWS / count
    return [op(16), number(34), op(5), number((320.0-height)/(count-VISIBLE_ROWS)), op(2), *scroll()]


def expression_events(tokens):
    """Serialize Statement_s's pointer array, expression entries and XStrings."""
    events = [[2, 3], [5, ('ff' * (4*len(tokens)))]]
    for kind, typ, value in tokens:
        string = isinstance(value, str)
        raw = struct.pack('>3I', kind, typ, 0xFFFFFFFF if string else value)
        events.extend([[2, 3], [5, raw.hex()]])
        if string:
            events.extend([[2, 0], [5, (value.encode('ascii')+b'\0').hex()]])
    return events


def set_expression(header, field, tokens):
    if struct.unpack_from('>2I', header, field) != (0, 0):
        raise ValueError('Scrolling requires an unused expression field in the verified menu template.')
    struct.pack_into('>2I', header, field, len(tokens), 0xFFFFFFFF)


def focus_scroll(index, count):
    top = min(max(index - 7, 0), max(count - VISIBLE_ROWS, 0))
    return f'"setLocalVarInt" "{SCROLL_VAR}" {top} ; '.encode()
