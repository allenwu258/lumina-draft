import { readdirSync, existsSync, readFileSync } from "node:fs";
import { join } from "node:path";
import { homedir } from "node:os";
import { createHash } from "node:crypto";
import { openSync, type Font } from "fontkit";
import type { TextStyle } from "@lumina/scene-core";

type Face = {
  path: string;
  postscript: string;
  family: string;
  full: string;
  faceIndex: number;
  weight: number;
  italic: boolean;
};
export type ResolvedFont = {
  family: string;
  postscript: string;
  fileSha256: string;
  ascent: number;
  descent: number;
  unitsPerEm: number;
  width: number;
  bold: boolean;
  weight: number;
  requestedWeight: number;
  faceWeight: number;
  italic: boolean;
};
const normalize = (s: string) =>
  s.trim().replace(/["']/g, "").replace(/\s+/g, " ").toLowerCase();
type FontMap = {
  version: string;
  defaults: { cjk: string[]; western: string[]; stack: string[] };
  generics: Record<string, string[]>;
  aliases: Record<string, string>;
  symbolFamilies: string[];
};
const WINDOWS_FONT_MAP: FontMap = JSON.parse(
  readFileSync(
    new URL("../../../../packages/scene-core/contracts/font-map-win32.json", import.meta.url),
    "utf8",
  ),
);
export const FONT_MAP_VERSION = WINDOWS_FONT_MAP.version;
const windowsAliases = new Map(
  Object.entries(WINDOWS_FONT_MAP.aliases).map(([name, target]) => [
    normalize(name),
    target,
  ]),
);
const windowsGenerics = new Map(
  Object.entries(WINDOWS_FONT_MAP.generics).map(([name, targets]) => [
    normalize(name),
    targets,
  ]),
);
function familyList(value: string): string[] {
  const result: string[] = [];
  let current = "",
    quote = "",
    escaped = false;
  for (const char of value) {
    if (escaped) {
      current += char;
      escaped = false;
    } else if (char === "\\") {
      current += char;
      escaped = true;
    } else if (quote && char === quote) {
      current += char;
      quote = "";
    } else if (!quote && (char === "'" || char === '"')) {
      current += char;
      quote = char;
    } else if (!quote && char === ",") {
      if (current.trim()) result.push(current.trim().replace(/^(["'])(.*)\1$/, "$2"));
      current = "";
    } else current += char;
  }
  if (current.trim()) result.push(current.trim().replace(/^(["'])(.*)\1$/, "$2"));
  return result;
}
let faces: Face[] | undefined;
const loaded = new Map<string, Font>();
const fileHashes = new Map<string, string>();
function fontFileSha256(path: string): string {
  let value = fileHashes.get(path);
  if (!value) {
    value = createHash("sha256").update(readFileSync(path)).digest("hex");
    fileHashes.set(path, value);
  }
  return value;
}
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
        const fontFaces = "fonts" in font ? font.fonts : [font];
        for (const [faceIndex, face] of fontFaces.entries()) {
          faces!.push({
            path,
            postscript: face.postscriptName,
            family: face.familyName,
            full: face.fullName,
            faceIndex,
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
  const key = `${face.path}#${face.faceIndex}`;
  let font = loaded.get(key);
  if (!font) {
    const value = openSync(face.path);
    const selected =
      "fonts" in value
        ? value.fonts[face.faceIndex]
        : (value as Font);
    if (!selected || selected.postscriptName !== face.postscript)
      throw new Error(`FONT_UNAVAILABLE: ${face.postscript}`);
    font = selected;
    if (loaded.size >= 16) loaded.delete(loaded.keys().next().value!);
    loaded.set(key, font);
  }
  return font;
}

export function resolveFont(style: TextStyle, text: string): ResolvedFont {
  const candidates = familyList(style.fontFamily);
  const isWindows = process.platform === "win32";
  const defaults = isWindows
    ? WINDOWS_FONT_MAP.defaults.stack
    : ["Noto Sans CJK SC", "DejaVu Sans", "Arial"];
  const installedFaces = installed();
  const exact = isWindows
    ? candidates.filter((candidate) =>
        installedFaces.some(
          (face) =>
            normalize(face.family) === normalize(candidate) ||
            normalize(face.full) === normalize(candidate),
        ),
      )
    : candidates;
  const aliases = isWindows
    ? candidates
        .filter((candidate) => !exact.some((name) => normalize(name) === normalize(candidate)))
        .flatMap((candidate) => windowsAliases.get(normalize(candidate)) || [])
    : [];
  const generics = isWindows
    ? candidates.flatMap((candidate) => windowsGenerics.get(normalize(candidate)) || [])
    : [];
  const families = [...new Set([...exact, ...aliases, ...generics, ...defaults])];
  // Browser glyph fallback can select symbol faces absent from CSS font-family.
  families.push(
    ...(isWindows ? WINDOWS_FONT_MAP.symbolFamilies : ["Noto Sans Symbols", "DejaVu Sans"]),
  );
  const parsedWeight = Number.parseFloat(style.fontWeight);
  const requestedWeight = Number.isFinite(parsedWeight)
    ? Math.max(100, Math.min(900, parsedWeight))
    : /bold/i.test(style.fontWeight)
      ? 700
      : 400;
  const weight = requestedWeight >= 600 ? 700 : 400;
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
          Math.abs(a.weight - requestedWeight) +
          (a.italic !== (style.fontStyle === "italic") ? 1000 : 0) -
          (Math.abs(b.weight - requestedWeight) +
            (b.italic !== (style.fontStyle === "italic") ? 1000 : 0)),
      );
    for (const face of choices) {
      let font = load(face);
      // OOXML exposes regular/bold. Measure the same face/weight that the writer requests.
      if (font.variationAxes?.wght)
        font = font.getVariation({ wght: requestedWeight });
      if (!chars.every((c) => font.hasGlyphForCodePoint(c.codePointAt(0)!)))
        continue;
      const advance = font
        .layout(text)
        .positions.reduce((sum, p) => sum + p.xAdvance, 0);
      return {
        family: face.family,
        postscript: face.postscript,
        fileSha256: fontFileSha256(face.path),
        ascent: font.ascent,
        descent: font.descent,
        unitsPerEm: font.unitsPerEm,
        bold: weight >= 600,
        weight,
        requestedWeight,
        faceWeight: face.weight,
        italic: face.italic,
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

/** Resolve browser-style glyph fallback, preserving font runs instead of flattening a mixed string. */
export function splitFontRuns(
  style: TextStyle,
  text: string,
  resolver = resolveFont,
) {
  const result: { text: string; font: ResolvedFont }[] = [];
  const segments = new Intl.Segmenter(undefined, {
    granularity: "grapheme",
  }).segment(text);
  for (const { segment } of segments) {
    const font = resolver(style, segment),
      last = result.at(-1);
    if (
      last?.font.postscript === font.postscript &&
      last.font.weight === font.weight
    ) {
      last.text += segment;
      last.font = resolver(style, last.text);
    } else result.push({ text: segment, font });
  }
  return result;
}
