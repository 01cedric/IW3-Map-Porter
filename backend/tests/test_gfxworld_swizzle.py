import sys
from pathlib import Path
ROOT=Path(__file__).resolve().parents[1];sys.path.insert(0,str(ROOT))
from cod4porter.assets.gfxworld import _swizzle_2d_pot,_unswizzle_2d_pot

def main():
    src=bytes(range(16))
    got=_swizzle_2d_pot(src,4,4,1)
    expected=bytes([0,1,4,5,2,3,6,7,8,9,12,13,10,11,14,15])
    assert got==expected,(got,expected)
    assert _unswizzle_2d_pot(got,4,4,1)==src
    rect=bytes((i*37+11)&0xff for i in range(8*4*4))
    sw=_swizzle_2d_pot(rect,8,4,4)
    assert sw!=rect
    assert _unswizzle_2d_pot(sw,8,4,4)==rect
    tall=bytes((i*13+5)&0xff for i in range(4*8))
    sw2=_swizzle_2d_pot(tall,4,8,1)
    assert sw2!=tall
    assert _unswizzle_2d_pot(sw2,4,8,1)==tall
    print({'passed':True,'morton_4x4':list(got),'rect_roundtrip':True,'tall_roundtrip':True})
if __name__=='__main__':main()
