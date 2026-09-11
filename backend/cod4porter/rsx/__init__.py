"""PC-to-RSX shader compilation for IW3 PS3 zones.

Package layout:

``d3d9``      exact Direct3D 9 shader-model 2/3 bytecode disassembly (fail closed)
``nv40``      RSX/NV40 vertex and fragment microcode encoding and decoding
``translate`` D3D9 IR -> RSX microcode translation with register accounting
``cgbinary``  Sony CgBinaryProgram container reading and writing
``interp``    deterministic software execution of RSX programs for previews

The instruction bit layouts in ``nv40`` mirror the vendored IW4Studio decoder
(``src/IW3MapPorter.Core/Vendor/IW4Studio``) field for field; the container
layout in ``cgbinary`` mirrors the upload/parameter shapes that decoder and the
PS3 Cg runtime consume.  Every module fails closed on inputs outside its proven
domain and reports the exact blocking construct.
"""

from .d3d9 import D3d9Program, D3d9Error, disassemble  # noqa: F401
