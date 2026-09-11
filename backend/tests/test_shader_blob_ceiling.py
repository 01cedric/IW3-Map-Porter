#!/usr/bin/env python3
"""A runaway RSX shader blob must fail closed, never serialize.

mp_wmd_night's compiled TechniqueSet consumed ~12 MiB in a single asset load
and then faulted: only a u32 blob-length field can drive a stream read that
large, so one compiled shader blob was multi-megabyte - a runaway translation.
The console streams exactly that many bytes and dereferences the trailing
garbage as its next structural pointer (the `unmapped address 0x3f03` fault).

The compiler now refuses a blob past MAX_SHADER_BLOB_BYTES with a
TechsetCompileError, which the sourcing ladder already catches: the techset
then stays on its substitution/omission path instead of writing a length
field that freezes the console.  A normal shader (the test pipeline emits a
few hundred bytes) is untouched.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from cod4porter.rsx import cgbinary
from cod4porter.rsx import techset_compile as tc
from cod4porter.rsx.techset_compile import (MAX_SHADER_BLOB_BYTES, TechsetCompileError,
                                            _guard_blob_size, compile_techniqueset)

sys.path.insert(0, str(Path(__file__).resolve().parent))
import importlib.util

_spec = importlib.util.spec_from_file_location('pipeline', str(Path(__file__).with_name('test_rsx_techset_pipeline.py')))
pipeline = importlib.util.module_from_spec(_spec)
try:
    _spec.loader.exec_module(pipeline)
except SystemExit:
    pass


def test_guard_accepts_normal_and_rejects_runaway():
    _guard_blob_size('pixel', 'ok', b'\x00' * 4096)                 # well within the ceiling
    _guard_blob_size('vertex', 'edge', b'\x00' * MAX_SHADER_BLOB_BYTES)
    try:
        _guard_blob_size('pixel', 'runaway_ps', b'\x00' * (MAX_SHADER_BLOB_BYTES + 1))
    except TechsetCompileError as error:
        assert 'runaway_ps' in str(error) and str(MAX_SHADER_BLOB_BYTES) in str(error)
        assert error.detail.startswith('pixel_blob_')
    else:
        raise AssertionError('a blob past the ceiling must raise')


def test_normal_pipeline_still_compiles():
    compiled = compile_techniqueset(pipeline._pc_techset())
    biggest = max(len(s.blob) for s in compiled.unique_shaders().values())
    assert biggest < MAX_SHADER_BLOB_BYTES, biggest
    assert any(slot is not None for slot in compiled.slots)


def test_runaway_translation_fails_closed_in_the_ladder():
    # Force the fragment build to emit a runaway blob; the techset compile must
    # raise the ladder-caught TechsetCompileError, not silently serialize it.
    real_build = cgbinary.build_fragment_binary

    def runaway(translation, **kw):
        blob, index = real_build(translation, **kw)
        return blob + b'\x00' * (MAX_SHADER_BLOB_BYTES + 16), index

    tc.cgbinary.build_fragment_binary = runaway
    try:
        try:
            compile_techniqueset(pipeline._pc_techset())
        except TechsetCompileError as error:
            assert 'ceiling' in str(error)
        else:
            raise AssertionError('runaway blob must abort compilation')
    finally:
        tc.cgbinary.build_fragment_binary = real_build

    # And the sourcing ladder catches exactly this exception type.
    from cod4porter import source_plan  # noqa: F401 - import proves the module wiring
    assert issubclass(TechsetCompileError, ValueError)


def main():
    test_guard_accepts_normal_and_rejects_runaway()
    test_normal_pipeline_still_compiles()
    test_runaway_translation_fails_closed_in_the_ladder()
    print(json.dumps({'passed': True, 'tests': 3}))


if __name__ == '__main__':
    main()
