// Instruction-only extraction. See LICENSE.IW4Studio.txt.
namespace IW4.Render.Shaders;

public readonly record struct RsxFragmentOperand(int SourceIndex, uint Raw)
{
    public RsxFragmentRegisterType RegisterKind =>
        RsxFragmentInstruction.SourceRegisterKind(Raw);

    public int RegisterIndex => (int)((Raw >> 2) & 0x3f);

    public bool Fp16 => ((Raw >> 8) & 1) != 0;

    public bool Negate => ((Raw >> 17) & 1) != 0;

    public bool Absolute => SourceIndex == 0
        ? ((Raw >> 29) & 1) != 0
        : ((Raw >> 18) & 1) != 0;

    public RsxSwizzleComponent SwizzleX =>
        (RsxSwizzleComponent)((Raw >> 9) & 3);

    public RsxSwizzleComponent SwizzleY =>
        (RsxSwizzleComponent)((Raw >> 11) & 3);

    public RsxSwizzleComponent SwizzleZ =>
        (RsxSwizzleComponent)((Raw >> 13) & 3);

    public RsxSwizzleComponent SwizzleW =>
        (RsxSwizzleComponent)((Raw >> 15) & 3);
}

/// <summary>Exact decoded fragment destination word.</summary>
public readonly record struct RsxFragmentDestination(uint Raw)
{
    public bool End => (Raw & 1) != 0;

    public int RegisterIndex => (int)((Raw >> 1) & 0x3f);

    public bool Fp16 => ((Raw >> 7) & 1) != 0;

    public bool ConditionWriteEnabled => ((Raw >> 8) & 1) != 0;

    public RsxFragmentWriteMask WriteMask =>
        (RsxFragmentWriteMask)((Raw >> 9) & 0x0f);

    public RsxFragmentInputAttribute SourceAttribute =>
        (RsxFragmentInputAttribute)((Raw >> 13) & 0x0f);

    public int TextureUnit => (int)((Raw >> 17) & 0x0f);

    public byte OpcodeLowBits => (byte)((Raw >> 24) & 0x3f);

    public bool NoDestination => ((Raw >> 30) & 1) != 0;

    public bool Saturate => ((Raw >> 31) & 1) != 0;
}

/// <summary>
/// Inline fragment constant retaining exact post-lane-transform IEEE-754 bits
/// as well as backend-neutral float views.
/// </summary>
public readonly record struct RsxFragmentInlineConstant(
    uint XBits,
    uint YBits,
    uint ZBits,
    uint WBits)
{
    public float X => BitConverter.Int32BitsToSingle(unchecked((int)XBits));

    public float Y => BitConverter.Int32BitsToSingle(unchecked((int)YBits));

    public float Z => BitConverter.Int32BitsToSingle(unchecked((int)ZBits));

    public float W => BitConverter.Int32BitsToSingle(unchecked((int)WBits));
}

/// <summary>
/// One immutable RSX fragment instruction. Raw lane-transformed words remain
/// available alongside the current semantic interpretations.
/// </summary>
public readonly record struct RsxFragmentInstruction(
    int Index,
    int Offset,
    uint Dst,
    uint Src0,
    uint Src1,
    uint Src2,
    RsxFragmentOpcode OpcodeType,
    int ByteCount,
    RsxFragmentInlineConstant? Constant)
{
    public static RsxFragmentRegisterType SourceRegisterKind(uint source) =>
        (RsxFragmentRegisterType)(source & 3);

    /// <summary>
    /// Direct backend-neutral CodePixel source row associated with this
    /// instruction's inline constant payload, when uniquely resolved.
    /// </summary>
    public ushort? DirectCodeConstantIndex { get; init; }

    /// <summary>
    /// Stable selected-pass material/literal pixel argument owning this
    /// instruction's inline constant payload. Backends bind its exact float4
    /// at draw time rather than specializing it into the program image.
    /// </summary>
    public int? StaticPixelConstantArgumentOrdinal { get; init; }

    public RsxFragmentDestination Destination => new(Dst);

    public RsxFragmentOperand Source0Operand => new(0, Src0);

    public RsxFragmentOperand Source1Operand => new(1, Src1);

    public RsxFragmentOperand Source2Operand => new(2, Src2);

    public RsxFragmentInlineConstant? InlineConstant => Constant;

    public int OperandCount => IsControlFlow
        ? 0
        : RsxShaderInstructionSet.FragmentOperandCount(OpcodeType);

    public byte Opcode => (byte)OpcodeType;

    public bool End => (Dst & 1) != 0;
    public int DestRegister => (int)((Dst >> 1) & 0x3f);
    public bool DestFp16 => ((Dst >> 7) & 1) != 0;
    public bool CondWriteEnabled => ((Dst >> 8) & 1) != 0;
    public RsxFragmentWriteMask WriteMask =>
        (RsxFragmentWriteMask)((Dst >> 9) & 0x0f);
    public RsxFragmentInputAttribute SourceAttribute =>
        (RsxFragmentInputAttribute)((Dst >> 13) & 0x0f);
    public int TextureUnit => (int)((Dst >> 17) & 0x0f);
    public bool ExpandedTexture => ((Dst >> 21) & 1) != 0;
    public RsxFragmentPrecision DestinationPrecision =>
        (RsxFragmentPrecision)((Dst >> 22) & 3);
    public bool NoDest => ((Dst >> 30) & 1) != 0;
    public bool Saturate => ((Dst >> 31) & 1) != 0;
    public RsxConditionTest ConditionTest =>
        (RsxConditionTest)((Src0 >> 18) & 7);
    public RsxSwizzleComponent ConditionSwizzleX =>
        (RsxSwizzleComponent)((Src0 >> 21) & 3);
    public RsxSwizzleComponent ConditionSwizzleY =>
        (RsxSwizzleComponent)((Src0 >> 23) & 3);
    public RsxSwizzleComponent ConditionSwizzleZ =>
        (RsxSwizzleComponent)((Src0 >> 25) & 3);
    public RsxSwizzleComponent ConditionSwizzleW =>
        (RsxSwizzleComponent)((Src0 >> 27) & 3);
    public bool ConditionWriteRegister1 => ((Src0 >> 30) & 1) != 0;
    public bool ConditionReadRegister1 => ((Src0 >> 31) & 1) != 0;
    public bool IsControlFlow =>
        RsxShaderInstructionSet.IsFragmentControlFlow(OpcodeType);
    public RsxFragmentResultScale Scale =>
        (RsxFragmentResultScale)((Src1 >> 28) & 7);
    public bool UsesIndexedInput => ((Src2 >> 30) & 1) != 0;
    public bool PerspectiveCorrection => ((Src2 >> 31) & 1) != 0;
    public bool IsTexture =>
        !IsControlFlow && RsxShaderInstructionSet.IsFragmentTexture(OpcodeType);

    public RsxFragmentPrecision SourcePrecision(int sourceIndex) =>
        sourceIndex is >= 0 and <= 2
            ? (RsxFragmentPrecision)((Src1 >> (19 + sourceIndex * 3)) & 7)
            : throw new ArgumentOutOfRangeException(nameof(sourceIndex));

    public RsxSwizzleComponent ConditionSwizzle(int component) =>
        component switch
    {
        0 => ConditionSwizzleX,
        1 => ConditionSwizzleY,
        2 => ConditionSwizzleZ,
        3 => ConditionSwizzleW,
        _ => throw new ArgumentOutOfRangeException(nameof(component))
    };
}
