/** Computed browser colors → clamped sRGB plus alpha, shared by text/fill/line. */
export function color(value: string): { color: string; transparency: number } {
  let rgb: number[],
    alpha = 1;
  const v = value.trim().toLowerCase();
  if (v === "transparent") return { color: "000000", transparency: 100 };
  if (v === "white" || v === "black")
    return { color: v === "white" ? "FFFFFF" : "000000", transparency: 0 };
  if (/^#[0-9a-f]{3,8}$/i.test(v)) {
    const h = v.slice(1),
      full = h.length <= 4 ? [...h].map((c) => c + c).join("") : h;
    rgb = [0, 2, 4].map((i) => parseInt(full.slice(i, i + 2), 16) / 255);
    if (full.length === 8) alpha = parseInt(full.slice(6), 16) / 255;
  } else {
    const body = v.slice(v.indexOf("(") + 1, -1),
      sides = body.split("/"),
      nums =
        sides[0]
          .match(/[-+]?\d*\.?\d+(?:e[-+]?\d+)?%?/g)
          ?.map((x) =>
            x.endsWith("%") ? parseFloat(x) / 100 : parseFloat(x),
          ) || [];
    if (sides[1])
      alpha = parseFloat(sides[1]) / (sides[1].includes("%") ? 100 : 1);
    if (v.startsWith("rgb")) {
      const percent = sides[0].includes("%");
      rgb = nums.slice(0, 3).map((n) => (percent ? n : n / 255));
      if (nums.length === 4) alpha = nums[3];
    } else if (v.startsWith("color(srgb ")) rgb = nums.slice(0, 3);
    else if (v.startsWith("oklch(") || v.startsWith("oklab(")) {
      const [l, c, h] = nums,
        a = v.startsWith("oklch") ? c * Math.cos((h * Math.PI) / 180) : c,
        b = v.startsWith("oklch") ? c * Math.sin((h * Math.PI) / 180) : h;
      const ll = (l + 0.3963377774 * a + 0.2158037573 * b) ** 3,
        mm = (l - 0.1055613458 * a - 0.0638541728 * b) ** 3,
        ss = (l - 0.0894841775 * a - 1.291485548 * b) ** 3;
      rgb = [
        4.0767416621 * ll - 3.3077115913 * mm + 0.2309699292 * ss,
        -1.2684380046 * ll + 2.6097574011 * mm - 0.3413193965 * ss,
        -0.0041960863 * ll - 0.7034186147 * mm + 1.707614701 * ss,
      ].map((n) =>
        n <= 0.0031308 ? 12.92 * n : 1.055 * n ** (1 / 2.4) - 0.055,
      );
    } else throw new Error(`UNSUPPORTED_COLOR: ${value}`);
  }
  if (
    rgb.length !== 3 ||
    rgb.some((n) => !Number.isFinite(n)) ||
    !Number.isFinite(alpha)
  )
    throw new Error(`INVALID_COLOR: ${value}`);
  return {
    color: rgb
      .map((n) =>
        Math.round(Math.max(0, Math.min(1, n)) * 255)
          .toString(16)
          .padStart(2, "0"),
      )
      .join("")
      .toUpperCase(),
    transparency: Math.round((1 - Math.max(0, Math.min(1, alpha))) * 100),
  };
}
