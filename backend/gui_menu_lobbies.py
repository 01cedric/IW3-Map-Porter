"""Local lobby image lookup and bounded startup diagnostics for the verified UI.

System-link and split-screen use a stock CSV image lookup. The private online
lobby already resolves loadscreen_<ui_mapname>. Reuse that naming convention
without altering the shared stock CSV string or adding unknown table assets.
"""
from gui_ui_layout import layout_value
import struct

LOCAL_PREVIEW_ENTRIES = (0x14A7060, 0x14B0E58)
LOADSCREEN_PREFIX_ENTRY = 0x1486BE5


def local_preview_tokens(prefix, mapname):
    # Keep 12 existing entry allocations, including string operands at 2 and 7.
    # The outermost grouping follows the native expression convention.
    return [(0,16,0), (0,16,0), (1,2,prefix), (0,5,0), (0,16,0),
            (0,16,0), (0,31,0), (1,2,mapname), (0,1,0), (0,1,0),
            (0,1,0), (0,1,0)]


def fix_local_lobby_previews(zone, replacements):
    prefix = struct.unpack_from('>I', zone, layout_value(zone, LOADSCREEN_PREFIX_ENTRY)+8)[0]
    if prefix in (0, 0xFFFFFFFF):
        raise ValueError('The lobby prefix must use the verified packed XString.')
    for start in layout_value(zone, LOCAL_PREVIEW_ENTRIES):
        records = [struct.unpack_from('>3I', zone, start+12*i) for i in range(12)]
        if records[1] != (0,56,0) or records[2][:2] != (1,2) or records[7][:2] != (1,2):
            raise ValueError('Local lobby material expression differs from its verified profile.')
        for i, token in enumerate(local_preview_tokens(prefix, records[7][2])):
            replacements[start+12*i] = struct.pack('>3I', *token)


def instrument_start(script, mode, native_startserver):
    prefix = (f'"execNow" "echo IW3MENU_{mode}_BEGIN" ; '
              '"execNow" "dvarlist ui_mapname" ; '
              '"execNow" "dvarlist ui_gametype" ; ')
    suffix = (f'"execNow" "echo IW3MENU_{mode}_HANDLER_RETURNED" ; '
              f'"exec" "echo IW3MENU_{mode}_BUFFER_REACHED" ; ')
    if native_startserver and b'"StartServer"' not in script:
        raise ValueError('The local lobby must retain the verified native StartServer handler.')
    result = prefix.encode() + script.rstrip(b'\0') + suffix.encode() + b'\0'
    if len(result) > 0x1400:
        raise ValueError('Instrumented launch exceeds the native script buffer.')
    return result
