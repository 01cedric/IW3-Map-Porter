#!/usr/bin/env python3
"""Portable deterministic runner for the script-style IW3MapPorter tests."""

from __future__ import annotations

import argparse
import fnmatch
import os
from pathlib import Path
import re
import subprocess
import sys


ROOT=Path(__file__).resolve().parents[1]
TEST_DIR=ROOT/'tests'
SKIP_EXIT_CODE=77
SKIP_PATTERN=re.compile(r'''["']skipped["']\s*:\s*(?:true|True)''')


def _fixture(parser:argparse.ArgumentParser,value:Path|None,environment:dict[str,str],name:str)->None:
    if value is None:return
    path=value.expanduser().resolve()
    if not path.is_file():parser.error(f'{name} fixture does not exist: {path}')
    environment[name]=str(path)


def _show_output(stdout:str,stderr:str)->None:
    for label,value in (('stdout',stdout),('stderr',stderr)):
        value=value.rstrip()
        if not value:continue
        print(f'  {label}:')
        for line in value.splitlines():print(f'    {line}')


def main(argv=None)->int:
    parser=argparse.ArgumentParser(description='Run every tests/test_*.py in a separate Python process')
    parser.add_argument('--match',action='append',default=[],metavar='GLOB',help='run matching test filenames; repeatable')
    parser.add_argument('--list',action='store_true',help='list selected tests without running them')
    parser.add_argument('--verbose',action='store_true',help='show stdout/stderr for passing and skipped tests')
    parser.add_argument('--no-compile',action='store_true',help='skip the initial compileall gate')
    parser.add_argument('--timeout',type=float,default=None,metavar='SECONDS',help='per-test timeout; default is unlimited')
    parser.add_argument('--pc-getaway-ff',type=Path,help='sets IW3_PORTER_PC_GETAWAY_FF for optional full-PC tests')
    parser.add_argument('--pc-nuked-ff',type=Path,help='sets IW3_PORTER_PC_NUKED_FF for optional Version-11 integration tests')
    parser.add_argument('--pc-nuked-iwd',type=Path,help='sets IW3_PORTER_PC_NUKED_IWD for optional Version-11 integration tests')
    parser.add_argument('--pc-nuked-load-ff',type=Path,help='sets IW3_PORTER_PC_NUKED_LOAD_FF for optional Version-11 load tests')
    parser.add_argument('--ps3-shipment-ff',type=Path,help='sets IW3_PORTER_PS3_SHIPMENT_FF for optional Retail tests')
    parser.add_argument('--ps3-shipment-load-ff',type=Path,help='sets IW3_PORTER_PS3_SHIPMENT_LOAD_FF for optional Retail-load tests')
    args=parser.parse_args(argv)
    if args.timeout is not None and args.timeout<=0:parser.error('--timeout must be positive')

    tests=sorted(TEST_DIR.glob('test_*.py'),key=lambda p:p.name)
    if args.match:tests=[p for p in tests if any(fnmatch.fnmatchcase(p.name,pattern) for pattern in args.match)]
    if not tests:parser.error('no tests matched')
    if args.list:
        for test in tests:print(test.relative_to(ROOT).as_posix())
        return 0

    environment=os.environ.copy()
    existing=environment.get('PYTHONPATH')
    environment['PYTHONPATH']=str(ROOT) if not existing else str(ROOT)+os.pathsep+existing
    environment['PYTHONHASHSEED']='0'
    environment['PYTHONDONTWRITEBYTECODE']='1'
    _fixture(parser,args.pc_getaway_ff,environment,'IW3_PORTER_PC_GETAWAY_FF')
    _fixture(parser,args.pc_nuked_ff,environment,'IW3_PORTER_PC_NUKED_FF')
    _fixture(parser,args.pc_nuked_iwd,environment,'IW3_PORTER_PC_NUKED_IWD')
    _fixture(parser,args.pc_nuked_load_ff,environment,'IW3_PORTER_PC_NUKED_LOAD_FF')
    _fixture(parser,args.ps3_shipment_ff,environment,'IW3_PORTER_PS3_SHIPMENT_FF')
    _fixture(parser,args.ps3_shipment_load_ff,environment,'IW3_PORTER_PS3_SHIPMENT_LOAD_FF')

    if not args.no_compile:
        compiled=subprocess.run(
            [sys.executable,'-m','compileall','-q','cod4porter','tools','tests'],
            cwd=ROOT,env=environment,capture_output=True,text=True,
        )
        if compiled.returncode:
            print('[FAIL] compileall')
            _show_output(compiled.stdout,compiled.stderr)
            return 1
        print('[PASS] compileall')

    passed=skipped=failed=0
    for test in tests:
        relative=test.relative_to(ROOT).as_posix()
        try:
            result=subprocess.run(
                [sys.executable,str(test)],cwd=ROOT,env=environment,
                capture_output=True,text=True,timeout=args.timeout,
            )
            declared_skip=bool(SKIP_PATTERN.search(result.stdout) or SKIP_PATTERN.search(result.stderr))
            if result.returncode==SKIP_EXIT_CODE or (result.returncode==0 and declared_skip):
                skipped+=1;print(f'[SKIP] {relative}')
                if args.verbose:_show_output(result.stdout,result.stderr)
            elif result.returncode==0:
                passed+=1;print(f'[PASS] {relative}')
                if args.verbose:_show_output(result.stdout,result.stderr)
            else:
                failed+=1;print(f'[FAIL] {relative} (exit {result.returncode})')
                _show_output(result.stdout,result.stderr)
        except subprocess.TimeoutExpired as exc:
            failed+=1;print(f'[FAIL] {relative} (timeout)')
            stdout=exc.stdout.decode(errors='replace') if isinstance(exc.stdout,bytes) else (exc.stdout or '')
            stderr=exc.stderr.decode(errors='replace') if isinstance(exc.stderr,bytes) else (exc.stderr or '')
            _show_output(stdout,stderr)

    print(f'[SUMMARY] PASS={passed} SKIP={skipped} FAIL={failed} TOTAL={len(tests)}')
    return 1 if failed else 0


if __name__=='__main__':raise SystemExit(main())
