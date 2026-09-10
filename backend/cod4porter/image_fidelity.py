"""Independent source expectations for lossless PS3 image serialization."""
import hashlib


def expected_mips(source, dropped=0):
    rows=[]
    bitmap=source.format_name in ('rgb8','rgba8')
    if bitmap and source.face_count!=1:
        raise ValueError('Bitmap fidelity requires a 2D image')
    for mip in sorted(source.mips,key=lambda m:(m.face,m.level)):
        if mip.level<dropped:continue
        data=bytes(mip.data)
        if bitmap:
            stride=3 if source.format_name=='rgb8' else 4
            if len(data)!=mip.width*mip.height*stride:
                raise ValueError('Bitmap fidelity source byte count is invalid')
            # Canonical A,R,G,B words preserve BGR/BGRA source pixels exactly.
            # Their storage hash changes even though no colour or alpha is lost.
            data=b''.join(bytes((data[i+3] if stride==4 else 255,data[i+2],data[i+1],data[i]))
                          for i in range(0,len(data),stride))
            if source.level_count>1:
                # Independent Morton address calculation, including rectangular tails.
                width,height=mip.width,mip.height
                if width&(width-1) or height&(height-1):raise ValueError('Bitmap mip dimensions must be powers of two')
                target=bytearray(len(data))
                for y in range(height):
                    for x in range(width):
                        address=0;output_bit=0
                        for bit in range(max(width,height).bit_length()-1):
                            if (1<<bit)<width:
                                address|=((x>>bit)&1)<<output_bit;output_bit+=1
                            if (1<<bit)<height:
                                address|=((y>>bit)&1)<<output_bit;output_bit+=1
                        offset=(y*width+x)*4
                        target[address*4:address*4+4]=data[offset:offset+4]
                data=bytes(target)
        rows.append((mip.face,mip.level-dropped,mip.width,mip.height,len(data),hashlib.sha256(data).hexdigest().upper()))
    return tuple(rows)
