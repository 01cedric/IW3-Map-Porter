# IWI Wavelet Converter

Standalone, GPL-3.0 command-line converter from Call of Duty 4 IWI v6 wavelet images to standard uncompressed BGRA IWI v6 files. It has no PS3 or map-porter dependencies.

Usage: `python convert.py input.iwi output.iwi`

Derived from OpenAssetTools IwiWaveletDecoder by the OpenAssetTools contributors (wavelet contribution by michaeloliverx):
https://github.com/Laupetin/OpenAssetTools/blob/9dca965366541504b71fa8cfb7ac049cb9b717e1/src/ObjImage/Image/IwiWaveletDecoder.cpp

Modified 2026-09-09: Python implementation, independent file CLI, checked allocation and bitstream reads, expanded BGRA output. Full corresponding Python source and GPL license accompany this program. No warranty; redistribution and modification are permitted under GPL-3.0. The porter invokes this separate tool through standard IWI files.

Supports wavelet RGBA, RGB, luminance-alpha, luminance and alpha. Requires power-of-two 2D/cube images. Volume images are unsupported. Decoded mips retain the upstream reconstruction (including byte wrapping and predictor deltas); output expands luminance and alpha without quantization. Unused final bits are not required to be zero, matching the upstream reader.
