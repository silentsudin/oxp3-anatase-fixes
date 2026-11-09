// oxp3-vibrance.fx - saturation / vibrance shader for the OneXPlayer 3 panel.
//
// Part of oxp3-anatase-fixes.  Our own code; no third-party shader text.
//
// Why: the eDP panel is wide gamut (tools/display/oxp3-edid.py reports ~1.5x
// sRGB area), so a colour-managed desktop already reproduces sRGB correctly.
// Games mostly ignore the display profile and write sRGB primaries straight
// into the framebuffer, which reads as washed-out or oversaturated depending
// on the title.  This shader is the per-game knob for that, mirroring the
// vendor's Windows "Screen Vibrance" slider (-100..+100, 0 = neutral) so a
// value read off OneXConsole means the same thing here.
//
// Dialect: ReShade FX as implemented by gamescope 3.16.23 (verified - see
// docs/hw/hdr-vibrance.md §5).  Consequences, learned from its parser:
//   * `register(t0)` / `register(s0)` annotations are rejected: textures and
//     samplers bind by declaration order, so texColorBuffer/samplerColor come
//     first and are units 0.
//   * a sampler needs the `{ Texture = <name>; }` state block.
//   * every pass needs an explicit VertexShader (we carry our own
//     fullscreen-triangle VS, so no ReShade.fxh on the include path is needed).
// This form is also valid for vkBasalt, whose parser is the same lineage.
//
// Loaded by either backend:
//   gamescope   --reshade-effect oxp3-vibrance.fx
//               (searched in /usr/share/gamescope/reshade/Shaders and
//                ~/.local/share/gamescope/reshade/Shaders)
//   vkBasalt    effects = oxp3_vibrance      (tools/display/vkbasalt/)
//
// Implementation: everything is a lerp around Rec.709 luma, so hue is
// preserved and the neutral axis never shifts (unlike a plain RGB gain).
// Vibrance weights the boost by how unsaturated the pixel already is, so
// skin tones and reds do not clip while foliage/UI get the lift.

uniform float Vibrance <
    ui_type = "slider";
    ui_min = -100.0; ui_max = 100.0; ui_step = 1.0;
    ui_label = "Vibrance (-100..+100)";
    ui_tooltip = "Saturation-weighted boost: low-saturation pixels move more.";
> = 0.0;

uniform float Saturation <
    ui_type = "slider";
    ui_min = -100.0; ui_max = 100.0; ui_step = 1.0;
    ui_label = "Saturation (-100..+100)";
> = 0.0;

uniform float VibranceProtect <
    ui_type = "slider";
    ui_min = 0.0; ui_max = 1.0; ui_step = 0.01;
    ui_label = "Vibrance protection (0 = linear, 1 = only greys)";
> = 0.65;

uniform int KeepLuma <
    ui_type = "combo";
    ui_items = "Off\0On\0";
    ui_label = "Keep original luminance";
> = 0;

texture2D texColorBuffer;
sampler2D samplerColor { Texture = texColorBuffer; };

static const float3 LUMA_709 = float3(0.2126, 0.7152, 0.0722);

// Fullscreen triangle: one draw, no vertex buffer required.
void Oxp3VibranceVS(in uint id : SV_VertexID,
                    out float4 position : SV_Position,
                    out float2 uv : TEXCOORD0)
{
    uv.x = (id == 2) ? 2.0 : 0.0;
    uv.y = (id == 1) ? 2.0 : 0.0;
    position = float4(uv * float2(2.0, -2.0) + float2(-1.0, 1.0), 0.0, 1.0);
}

float luma709(float3 c)
{
    return dot(c, LUMA_709);
}

// Push a colour away from (gain > 1) or toward (gain < 1) its own luma.
float3 saturateAroundLuma(float3 c, float gain)
{
    float y = luma709(c);
    return y + (c - y) * gain;
}

// Per-pixel saturation in 0..1 (HSV-style S), stable for near-black pixels.
float pixelSaturation(float3 c)
{
    float mx = max(c.r, max(c.g, c.b));
    float mn = min(c.r, min(c.g, c.b));
    return mx > 1e-5 ? (mx - mn) / mx : 0.0;
}

float3 applyVibrance(float3 c, float amount, float protect)
{
    if (abs(amount) < 1e-4)
        return c;

    float gain = 1.0 + amount;
    float s = pixelSaturation(c);
    // w = 1 for neutral pixels, 0 for fully saturated ones.
    float w = 1.0 - smoothstep(0.0, 1.0, s);
    w = lerp(1.0, w, clamp(protect, 0.0, 1.0));

    float effective = 1.0 + (gain - 1.0) * w;
    return saturateAroundLuma(c, effective);
}

float4 PS_Oxp3Vibrance(float4 position : SV_Position, float2 uv : TEXCOORD0) : SV_Target
{
    float4 src = tex2D(samplerColor, uv);
    float3 c = src.rgb;
    float y0 = luma709(c);

    c = applyVibrance(c, Vibrance / 100.0, VibranceProtect);
    if (abs(Saturation) > 1e-4)
        c = saturateAroundLuma(c, 1.0 + Saturation / 100.0);
    c = clamp(c, 0.0, 1.0);

    if (KeepLuma != 0) {
        float y1 = luma709(c);
        if (y1 > 1e-5)
            c = clamp(c * (y0 / y1), 0.0, 1.0);
    }

    return float4(c, src.a);
}

technique Oxp3Vibrance <
    ui_label = "OXP3 vibrance / saturation";
    ui_tooltip = "-100..+100 like the vendor slider, 0 = passthrough";
>
{
    pass
    {
        VertexShader = Oxp3VibranceVS;
        PixelShader  = PS_Oxp3Vibrance;
    }
}
