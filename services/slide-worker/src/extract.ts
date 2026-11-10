import type {
  SceneIR,
  SceneNode,
  GroupNode,
  TextStyle,
  Rect,
} from "@lumina/scene-core";

/** Runs in the isolated page. No imports/closures: all author text is visited exactly once. */
export async function extractDOM(): Promise<{
  nodes: SceneNode[];
  paintOps: SceneIR["paintOps"];
  textInventory: SceneIR["textInventory"];
  diagnostics: SceneIR["diagnostics"];
  images: { id: string; data: string; width: number; height: number }[];
  fonts: string[];
  paintGroups: {
    id: string;
    context: boolean;
    positioned: boolean;
    z: number;
  }[];
}> {
  const root = document.querySelector<HTMLElement>("#ppt-page")!;
  const origin = root.getBoundingClientRect();
  if (Math.abs(origin.width - 800) > 0.1 || Math.abs(origin.height - 450) > 0.1)
    throw new Error("UNSUPPORTED_CANVAS: expected 800x450");
  const nodes: SceneNode[] = [],
    paintOps: SceneIR["paintOps"] = [],
    textInventory: SceneIR["textInventory"] = [],
    diagnostics: SceneIR["diagnostics"] = [],
    images: { id: string; data: string; width: number; height: number }[] = [],
    fonts = new Set<string>();
  let count = 0;
  const paintGroups: {
    id: string;
    context: boolean;
    positioned: boolean;
    z: number;
  }[] = [];
  const round = (n: number) => Math.round(n * 1000) / 1000;
  const rect = (r: DOMRect): Rect => ({
    x: round(r.x - origin.x),
    y: round(r.y - origin.y),
    width: round(r.width),
    height: round(r.height),
  });
  const path = (n: Node): string => {
    if (n === root) return "main";
    const p = n.parentNode!;
    return path(p) + "/" + Array.prototype.indexOf.call(p.childNodes, n);
  };
  const id = (n: Node, role: string) =>
    path(n).replaceAll("/", "_") + "_" + role;
  const styleOf = (el: Element) => getComputedStyle(el);
  const transparent = (c: string) =>
    c === "transparent" || c === "rgba(0, 0, 0, 0)";
  const radius = (v: string, size: number) =>
    v.endsWith("%") ? (parseFloat(v) * size) / 100 : parseFloat(v) || 0;
  const radii = (s: CSSStyleDeclaration, r: Rect) =>
    [
      s.borderTopLeftRadius,
      s.borderTopRightRadius,
      s.borderBottomRightRadius,
      s.borderBottomLeftRadius,
    ].map((v) => {
      if (v.trim().split(/\s+/).length > 1)
        throw new Error("UNSUPPORTED_ELLIPTICAL_RADIUS");
      return round(
        Math.min(
          radius(v, Math.min(r.width, r.height)),
          r.width / 2,
          r.height / 2,
        ),
      );
    });
  const measure = document.createElement("canvas").getContext("2d")!;
  const font = (s: TextStyle) =>
    `${s.fontStyle} ${s.fontWeight} ${s.fontSize}px ${s.fontFamily}`;
  function hiddenText(n: Node, reason: string) {
    if (n.nodeType === Node.TEXT_NODE)
      textInventory.push({
        sourcePath: path(n),
        sourceText: n.textContent || "",
        nodeId: null,
        reason,
      });
    else for (const c of n.childNodes) hiddenText(c, reason);
  }
  function text(n: Text, owner: string) {
    const sourceText = n.data,
      p = n.parentElement!,
      s = styleOf(p),
      sourcePath = path(n);
    if (s.visibility !== "visible") {
      textInventory.push({
        sourcePath,
        sourceText,
        nodeId: null,
        reason: "visibility",
      });
      return;
    }
    if (!sourceText) {
      return;
    }
    const ts: TextStyle = {
      fontFamily: s.fontFamily,
      fontSize: parseFloat(s.fontSize),
      fontWeight: s.fontWeight,
      fontStyle: s.fontStyle,
      color: s.color,
      letterSpacing:
        s.letterSpacing === "normal" ? 0 : parseFloat(s.letterSpacing),
      decoration: s.textDecorationLine,
    };
    if (!["none", "uppercase", "lowercase"].includes(s.textTransform))
      throw new Error(`UNSUPPORTED_TEXT_TRANSFORM: ${sourcePath}`);
    if (
      s.writingMode !== "horizontal-tb" ||
      s.direction !== "ltr" ||
      s.textShadow !== "none"
    )
      throw new Error(`UNSUPPORTED_TEXT_STYLE: ${sourcePath}`);
    if (
      s.textAlign === "justify" ||
      !["normal", "0px"].includes(s.wordSpacing) ||
      s.fontVariantCaps !== "normal" ||
      s.fontStretch !== "100%"
    )
      throw new Error(`UNSUPPORTED_TEXT_TYPOGRAPHY: ${sourcePath}`);
    measure.font = font(ts);
    measure.letterSpacing = `${ts.letterSpacing}px`;
    fonts.add(font(ts));
    const segments = [
      ...new Intl.Segmenter(undefined, { granularity: "grapheme" }).segment(
        sourceText,
      ),
    ];
    const range = document.createRange();
    const lines: {
      frame: Rect;
      baseline: number;
      fragments: {
        runId: string;
        sourceStart: number;
        sourceEnd: number;
        displayText: string;
        frame: Rect;
        baseline: number;
        measuredWidth: number;
      }[];
    }[] = [];
    const nodeId = id(n, "text"),
      runId = nodeId + "_run";
    let current: (typeof lines)[number]["fragments"][number] | undefined;
    const preserve =
      s.whiteSpace.startsWith("pre") || s.whiteSpace === "break-spaces";
    for (const seg of segments) {
      const start = seg.index,
        end = start + seg.segment.length;
      range.setStart(n, start);
      range.setEnd(n, end);
      const boxes = [...range.getClientRects()].filter(
        (r) => r.width > 0.001 && r.height > 0.001,
      );
      if (!boxes.length) {
        current = undefined;
        continue;
      }
      const r = rect(boxes[0]);
      let display = seg.segment;
      if (display === "\t" && preserve) {
        current = undefined;
        continue;
      }
      if (!preserve && /^[\t\r\n ]+$/.test(display)) display = " ";
      if (/^[\r\n]+$/.test(display)) {
        current = undefined;
        continue;
      }
      if (s.textTransform === "uppercase") display = display.toUpperCase();
      if (s.textTransform === "lowercase") display = display.toLowerCase();
      const metrics = measure.measureText(display);
      const baseline = round(r.y + metrics.fontBoundingBoxAscent);
      let line = lines.find(
        (l) =>
          Math.abs(l.baseline - baseline) < 0.25 &&
          Math.abs(l.frame.y - r.y) < 0.25,
      );
      if (!line) {
        line = { frame: { ...r }, baseline, fragments: [] };
        lines.push(line);
        current = undefined;
      }
      if (
        current &&
        line.fragments.includes(current) &&
        current.sourceEnd === start
      ) {
        current.sourceEnd = end;
        current.displayText += display;
        current.frame.width = round(r.x + r.width - current.frame.x);
        current.measuredWidth = round(
          measure.measureText(current.displayText).width,
        );
      } else {
        current = {
          runId,
          sourceStart: start,
          sourceEnd: end,
          displayText: display,
          frame: { ...r },
          baseline,
          measuredWidth: round(metrics.width),
        };
        line.fragments.push(current);
      }
      line.frame.width = round(
        Math.max(line.frame.x + line.frame.width, r.x + r.width) -
          Math.min(line.frame.x, r.x),
      );
      line.frame.x = Math.min(line.frame.x, r.x);
    }
    if (!lines.length) {
      textInventory.push({
        sourcePath,
        sourceText,
        nodeId: null,
        reason: "collapsed-whitespace",
      });
      return;
    }
    const r = rect(rangeOf(n));
    nodes.push({
      id: nodeId,
      sourcePath,
      kind: "text",
      frame: r,
      text: {
        ownerId: owner,
        sourceText,
        paragraphs: [
          {
            runs: [
              {
                id: runId,
                sourceStart: 0,
                sourceEnd: sourceText.length,
                style: ts,
              },
            ],
          },
        ],
        lines,
      },
    });
    paintOps.push({ op: "draw", nodeId });
    textInventory.push({ sourcePath, sourceText, nodeId, reason: null });
  }
  function rangeOf(n: Node) {
    const r = document.createRange();
    r.selectNodeContents(n);
    return r.getBoundingClientRect();
  }
  function shadow(s: CSSStyleDeclaration, sourcePath: string) {
    if (s.boxShadow === "none") return [];
    const parts = s.boxShadow.split(/,(?![^()]*\))/);
    const result = [];
    for (const p of parts) {
      if (p.includes("inset"))
        throw new Error(`UNSUPPORTED_INSET_SHADOW: ${sourcePath}`);
      const color = p.match(/rgba?\([^)]+\)|color\([^)]+\)/)?.[0];
      const values =
        p
          .replace(color || "", "")
          .match(/-?[\d.]+px/g)
          ?.map(parseFloat) || [];
      if (!color || values.length < 2)
        throw new Error(`UNSUPPORTED_SHADOW: ${sourcePath}`);
      result.push({
        color,
        x: values[0],
        y: values[1],
        blur: values[2] || 0,
        spread: values[3] || 0,
      });
    }
    diagnostics.push({
      code: "SHADOW_CANVAS_BLUR",
      message: "Canvas uses its native blur for CSS box shadows.",
      sourcePath,
    });
    return result;
  }
  async function svg(el: SVGElement, r: Rect) {
    if (r.width <= 0 || r.height <= 0) return;
    if (
      r.width > 4096 ||
      r.height > 4096 ||
      r.width * r.height > 8 * 1024 * 1024
    )
      throw new Error("SVG_ASSET_LIMIT");
    const clone = el.cloneNode(true) as SVGElement;
    const originals = [el, ...el.querySelectorAll("*")],
      copies = [clone, ...clone.querySelectorAll("*")];
    const props = [
      "fill",
      "fill-opacity",
      "fill-rule",
      "stroke",
      "stroke-width",
      "stroke-opacity",
      "stroke-linecap",
      "stroke-linejoin",
      "stroke-dasharray",
      "font-family",
      "font-size",
      "font-weight",
      "color",
      "opacity",
      "visibility",
    ];
    originals.forEach((e, i) => {
      const s = styleOf(e);
      for (const p of props)
        (copies[i] as SVGElement).style.setProperty(p, s.getPropertyValue(p));
    });
    clone.setAttribute("xmlns", "http://www.w3.org/2000/svg");
    clone.setAttribute("width", String(r.width));
    clone.setAttribute("height", String(r.height));
    clone.style.opacity = "1";
    clone.style.margin = "0";
    const image = new Image();
    image.src =
      "data:image/svg+xml;charset=utf-8," +
      encodeURIComponent(new XMLSerializer().serializeToString(clone));
    await image.decode();
    const c = document.createElement("canvas");
    c.width = Math.ceil(r.width);
    c.height = Math.ceil(r.height);
    c.getContext("2d")!.drawImage(image, 0, 0, r.width, r.height);
    const assetId = id(el, "svg");
    images.push({
      id: assetId,
      data: c.toDataURL("image/png").split(",")[1],
      width: c.width,
      height: c.height,
    });
    nodes.push({
      id: assetId + "_image",
      sourcePath: path(el),
      kind: "image",
      frame: r,
      assetId,
    });
    paintOps.push({ op: "draw", nodeId: assetId + "_image" });
    hiddenText(el, "svg-asset");
  }
  async function element(el: Element, parentOwner: string) {
    if (++count > 5000) throw new Error("DOM_LIMIT");
    const sourcePath = path(el),
      s = styleOf(el),
      r = rect(el.getBoundingClientRect());
    if (s.display === "none" || s.contentVisibility === "hidden") {
      hiddenText(el, "display:none");
      return;
    }
    if (["STYLE", "SCRIPT", "META", "TITLE"].includes(el.tagName)) return;
    for (const pseudo of ["::before", "::after"]) {
      const p = getComputedStyle(el, pseudo);
      if (p.content && !["none", "normal", '""'].includes(p.content))
        throw new Error(`UNSUPPORTED_PSEUDO: ${sourcePath}`);
    }
    if (
      s.transform !== "none" ||
      s.filter !== "none" ||
      s.backdropFilter !== "none" ||
      s.mixBlendMode !== "normal" ||
      s.clipPath !== "none" ||
      s.maskImage !== "none" ||
      s.backgroundImage !== "none"
    )
      throw new Error(`UNSUPPORTED_EFFECT: ${sourcePath}`);
    if (
      s.position === "fixed" ||
      s.position === "sticky" ||
      (el.tagName === "LI" && s.listStyleType !== "none")
    )
      throw new Error(`UNSUPPORTED_LAYOUT: ${sourcePath}`);
    if (
      (el.scrollWidth > el.clientWidth + 1 && s.overflowX === "auto") ||
      (el.scrollHeight > el.clientHeight + 1 && s.overflowY === "auto")
    )
      throw new Error(`UNSUPPORTED_SCROLL: ${sourcePath}`);
    const groupId = id(el, "group");
    const parentDisplay = el.parentElement
      ? styleOf(el.parentElement).display
      : "";
    const positioned = s.position !== "static",
      zContext =
        s.zIndex !== "auto" &&
        (positioned ||
          parentDisplay.includes("flex") ||
          parentDisplay.includes("grid"));
    paintGroups.push({
      id: groupId,
      context: el === root || parseFloat(s.opacity) < 1 || zContext,
      positioned,
      z: s.zIndex === "auto" ? 0 : parseInt(s.zIndex) || 0,
    });
    const isBlock = !["inline", "contents"].includes(s.display),
      owner = isBlock ? groupId : parentOwner;
    const rad = radii(s, r);
    const clipped =
      ["hidden", "clip", "scroll", "auto"].includes(s.overflowX) ||
      ["hidden", "clip", "scroll", "auto"].includes(s.overflowY);
    const group: GroupNode = {
      id: groupId,
      sourcePath,
      kind: el.tagName === "TABLE" ? "table" : "group",
      tagName: el.tagName.toLowerCase(),
      frame: r,
      opacity: parseFloat(s.opacity),
      clip: null,
    };
    if (group.kind === "table")
      group.cells = [...el.querySelectorAll("tr")]
        .filter((tr) => tr.closest("table") === el)
        .flatMap((tr, row) =>
          [...tr.children]
            .filter((c) => ["TD", "TH"].includes(c.tagName))
            .map((c, column) => ({
              row,
              column,
              rowSpan: (c as HTMLTableCellElement).rowSpan,
              columnSpan: (c as HTMLTableCellElement).colSpan,
              nodeId: id(c, "group"),
              frame: rect(c.getBoundingClientRect()),
            })),
        );
    if (group.cells?.some((c) => c.rowSpan !== 1 || c.columnSpan !== 1))
      throw new Error(`UNSUPPORTED_MERGED_TABLE: ${sourcePath}`);
    nodes.push(group);
    paintOps.push({ op: "begin", nodeId: groupId });
    if (s.visibility === "visible" && r.width > 0 && r.height > 0) {
      const borders = ["Top", "Right", "Bottom", "Left"].map((side) => ({
        width:
          parseFloat(
            s.getPropertyValue("border-" + side.toLowerCase() + "-width"),
          ) || 0,
        color: s.getPropertyValue("border-" + side.toLowerCase() + "-color"),
        style: s.getPropertyValue("border-" + side.toLowerCase() + "-style"),
      }));
      if (
        borders.some(
          (b) => b.width && !["none", "solid", "hidden"].includes(b.style),
        )
      )
        throw new Error(`UNSUPPORTED_BORDER: ${sourcePath}`);
      let fill = ["TR", "TBODY", "THEAD", "TFOOT"].includes(el.tagName)
        ? "rgba(0, 0, 0, 0)"
        : s.backgroundColor;
      // CSS paints row/rowgroup backgrounds underneath transparent cells.
      if (["TD", "TH"].includes(el.tagName) && transparent(fill)) {
        let row = el.parentElement;
        while (row && row.tagName !== "TABLE") {
          const color = styleOf(row).backgroundColor;
          if (!transparent(color)) {
            fill = color;
            break;
          }
          row = row.parentElement;
        }
      }
      const shadows = shadow(s, sourcePath);
      if (
        !transparent(fill) ||
        borders.some((b) => b.width && b.style !== "none") ||
        shadows.length
      ) {
        const nodeId = id(el, "shape");
        nodes.push({
          id: nodeId,
          sourcePath,
          kind: "shape",
          frame: r,
          fill,
          radii: rad,
          borders,
          shadows,
        });
        paintOps.push({ op: "draw", nodeId });
      }
    }
    // An element's overflow clips descendants, not its own box shadow.
    const clipId = groupId + "_overflow";
    if (clipped) {
      nodes.push({
        id: clipId,
        sourcePath,
        kind: "group",
        tagName: "clip",
        frame: r,
        opacity: 1,
        clip: { frame: r, radii: rad },
      });
      paintGroups.push({ id: clipId, context: false, positioned: false, z: 0 });
      paintOps.push({ op: "begin", nodeId: clipId });
    }
    if (el instanceof SVGElement && el.tagName.toLowerCase() === "svg") {
      await svg(el, r);
    } else {
      const children = [...el.childNodes];
      for (const child of children) {
        if (child instanceof Text) text(child, owner);
        else if (child instanceof Element) await element(child, owner);
      }
    }
    if (clipped) paintOps.push({ op: "end", nodeId: clipId });
    paintOps.push({ op: "end", nodeId: groupId });
  }
  await element(root, id(root, "group"));
  return {
    nodes,
    paintOps,
    textInventory,
    diagnostics,
    images,
    fonts: [...fonts].sort(),
    paintGroups,
  };
}
