import { readdirSync, existsSync } from "node:fs";
import { join } from "node:path";
import { homedir } from "node:os";
import { openSync, type Font } from "fontkit";
import type { TextStyle } from "@lumina/scene-core";

type Face = {
  path: string;
  postscript: string;
  family: string;
  full: string;
  weight: number;
  italic: boolean;
};
export type ResolvedFont = {
  family: string;
  postscript: string;
  ascent: number;
  descent: number;
  unitsPerEm: number;
  width: number;
};
const normalize = (s: string) => s.replace(/[\s"'-]/g, "").toLowerCase();
let faces: Face[] | undefined;
const loaded = new Map<string, Font>();
function installed(): Face[] {
  if (faces) return faces;
  faces = [];
  const folders =
    process.platform === "win32"
      ? [
          join(process.env.WINDIR || "C:/Windows", "Fonts"),
          join(
            process.env.LOCALAPPDATA || homedir(),
            "Microsoft/Windows/Fonts",
          ),
        ]
      : [
          "/usr/share/fonts/truetype",
          "/usr/share/fonts/opentype",
          "/Library/Fonts",
          join(homedir(), ".fonts"),
        ];
  const visit = (folder: string) => {
    if (!existsSync(folder)) return;
    for (const item of readdirSync(folder, { withFileTypes: true })) {
      const path = join(folder, item.name);
      if (item.isDirectory()) {
        visit(path);
        continue;
      }
      if (!/\.(ttf|otf|ttc)$/i.test(item.name)) continue;
      try {
        const font = openSync(path);
        for (const face of "fonts" in font ? font.fonts : [font]) {
          faces!.push({
            path,
            postscript: face.postscriptName,
            family: face.familyName,
            full: face.fullName,
            weight:
              face["OS/2"]?.usWeightClass ||
              (/bold/i.test(face.subfamilyName) ? 700 : 400),
            italic: face.italicAngle !== 0,
          });
        }
      } catch {
        /* Unreadable or unsupported installed font, never a silent glyph fallback. */
      }
    }
  };
  folders.forEach(visit);
  return faces;
}
function load(face: Face): Font {
  let font = loaded.get(face.postscript);
  if (!font) {
    const value = openSync(face.path);
    const selected =
      "fonts" in value
        ? value.fonts.find((f) => f.postscriptName === face.postscript)
        : (value as Font);
    if (!selected) throw new Error(`FONT_UNAVAILABLE: ${face.postscript}`);
    font = selected;
    if (loaded.size >= 16) loaded.delete(loaded.keys().next().value!);
    loaded.set(face.postscript, font);
  }
  return font;
}

export function resolveFont(style: TextStyle, text: string): ResolvedFont {
  const generic = /^(sans-serif|system-ui|serif|monospace)$/i;
  const candidates = style.fontFamily
    .split(",")
    .map((s) => s.trim().replace(/^["']|["']$/g, ""));
  const defaults =
    process.platform === "win32"
      ? ["Microsoft YaHei", "Arial"]
      : ["Noto Sans CJK SC", "DejaVu Sans", "Arial"];
  const families = candidates.flatMap((c) =>
    generic.test(c) ? defaults : [c],
  );
  // Browser glyph fallback can select symbol faces absent from CSS font-family.
  families.push(
    ...(process.platform === "win32"
      ? ["Segoe UI Symbol", "Segoe UI Emoji", "Segoe UI"]
      : ["Noto Sans Symbols", "DejaVu Sans"]),
  );
  const chars = [...text].filter((c) => !/[\s\u200b-\u200d\ufe0f]/u.test(c));
  for (const family of families) {
    const choices = installed()
      .filter(
        (f) =>
          normalize(f.family) === normalize(family) ||
          normalize(f.full) === normalize(family),
      )
      .sort(
        (a, b) =>
          Math.abs(a.weight - Number(style.fontWeight || 400)) +
          (a.italic !== (style.fontStyle === "italic") ? 1000 : 0) -
          (Math.abs(b.weight - Number(style.fontWeight || 400)) +
            (b.italic !== (style.fontStyle === "italic") ? 1000 : 0)),
      );
    for (const face of choices) {
      const font = load(face);
      if (!chars.every((c) => font.hasGlyphForCodePoint(c.codePointAt(0)!)))
        continue;
      const advance = font
        .layout(text)
        .positions.reduce((sum, p) => sum + p.xAdvance, 0);
      return {
        family: face.family,
        postscript: face.postscript,
        ascent: font.ascent,
        descent: font.descent,
        unitsPerEm: font.unitsPerEm,
        width:
          (advance * style.fontSize) / font.unitsPerEm +
          [...text].length * style.letterSpacing,
      };
    }
  }
  throw new Error(
    `FONT_UNAVAILABLE: ${style.fontFamily}: ${text.slice(0, 32)}`,
  );
}
