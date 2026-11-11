import type {
  SceneIR,
  SceneNode,
  GroupNode,
  TextStyle,
  Rect,
  CanvasSize,
} from "@lumina/scene-core";

/** Runs in the isolated page. No imports/closures: all author text is visited exactly once. */
export async function extractDOM({ width, height }: CanvasSize): Promise<{
  nodes: SceneNode[];
  paintOps: SceneIR["paintOps"];
  textInventory: SceneIR["textInventory"];
  diagnostics: SceneIR["diagnostics"];
  images: {
    id: string;
    data: string;
    svg: string;
    width: number;
    height: number;
    pixelWidth: number;
    pixelHeight: number;
  }[];
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
  if (Math.abs(origin.width - width) > 0.1 || Math.abs(origin.height - height) > 0.1)
    throw new Error(`UNSUPPORTED_CANVAS: expected ${width}x${height}`);
  const nodes: SceneNode[] = [],
    paintOps: SceneIR["paintOps"] = [],
    textInventory: SceneIR["textInventory"] = [],
    diagnostics: SceneIR["diagnostics"] = [],
    images: {
      id: string;
      data: string;
      svg: string;
      width: number;
      height: number;
      pixelWidth: number;
      pixelHeight: number;
    }[] = [],
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
  let svgPixels = 0;
  async function svg(el: SVGElement, r: Rect) {
    if (r.width <= 0 || r.height <= 0 || styleOf(el).visibility !== "visible") {
      hiddenText(el, "invisible-svg");
      return;
    }
    if (r.width > 4096 || r.height > 4096 || r.width * r.height > 8_000_000)
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
      "stroke-dashoffset",
      "stroke-miterlimit",
      "font-family",
      "font-size",
      "font-weight",
      "font-style",
      "font-variant",
      "letter-spacing",
      "word-spacing",
      "text-anchor",
      "dominant-baseline",
      "alignment-baseline",
      "text-decoration",
      "white-space",
      "color",
      "opacity",
      "visibility",
      "display",
      "stop-color",
      "stop-opacity",
      "clip-rule",
      "clip-path",
      "marker-start",
      "marker-mid",
      "marker-end",
      "paint-order",
      "vector-effect",
      "overflow",
      "transform",
      "transform-origin",
    ];
    const srgb = (value: string) => {
      if (value === "none" || value.startsWith("url(")) return value;
      measure.clearRect(0, 0, 1, 1);
      measure.fillStyle = value;
      measure.fillRect(0, 0, 1, 1);
      const [red, green, blue, alpha] = measure.getImageData(0, 0, 1, 1).data;
      return alpha === 255
        ? `rgb(${red}, ${green}, ${blue})`
        : `rgba(${red}, ${green}, ${blue}, ${round(alpha / 255)})`;
    };
    originals.forEach((e, i) => {
      const style = styleOf(e),
        copy = copies[i] as SVGElement;
      if (
        style.filter !== "none" ||
        style.maskImage !== "none" ||
        style.mixBlendMode !== "normal"
      )
        throw new Error(`UNSUPPORTED_SVG_EFFECT: ${path(e)}`);
      copy.removeAttribute("class");
      copy.removeAttribute("style");
      for (const prop of props) {
        let value = style.getPropertyValue(prop);
        if (/^(fill|stroke|color|stop-color)$/.test(prop)) value = srgb(value);
        // Computed URLs may be absolute. Assets are standalone and only local references survive.
        value = value.replace(
          /url\(\s*["']?[^#)]*#([^"')]+)["']?\s*\)/g,
          "url(#$1)",
        );
        if (value.includes("url(") && !/^url\(#[a-zA-Z0-9_.:-]+\)$/.test(value))
          throw new Error("UNSUPPORTED_SVG_REFERENCE");
        if (value) {
          if (copy.hasAttribute(prop)) copy.removeAttribute(prop);
          copy.style.setProperty(prop, value);
          // Presentation attributes also carry resolved colors/fonts for Office SVG consumers.
          if (
            /^(fill|stroke|color|stop-color|font-family|font-size|font-weight|font-style|letter-spacing|text-anchor|dominant-baseline)$/.test(
              prop,
            )
          )
            copy.setAttribute(prop, value);
        }
      }
    });
    clone.querySelectorAll("style").forEach((e) => e.remove());
    clone.setAttribute("xmlns", "http://www.w3.org/2000/svg");
    clone.setAttribute("width", String(r.width));
    clone.setAttribute("height", String(r.height));
    clone.style.width = `${r.width}px`;
    clone.style.height = `${r.height}px`;
    // Root opacity already belongs to the Scene group.
    clone.style.opacity = "1";
    clone.setAttribute("opacity", "1");
    clone.style.margin = "0";
    const source = new XMLSerializer().serializeToString(clone);
    const image = new Image();
    image.src =
      "data:image/svg+xml;charset=utf-8," + encodeURIComponent(source);
    await image.decode();
    const scale = Math.min(
      3,
      4096 / r.width,
      4096 / r.height,
      Math.sqrt(8_000_000 / (r.width * r.height)),
    );
    const c = document.createElement("canvas");
    c.width = Math.max(1, Math.floor(r.width * scale));
    c.height = Math.max(1, Math.floor(r.height * scale));
    svgPixels += c.width * c.height;
    if (svgPixels > 32_000_000) throw new Error("SVG_PAGE_PIXEL_LIMIT");
    c.getContext("2d")!.drawImage(image, 0, 0, c.width, c.height);
    const assetId = id(el, "svg"),
      nodeId = assetId + "_image";
    images.push({
      id: assetId,
      data: c.toDataURL("image/png").split(",")[1],
      svg: source,
      width: r.width,
      height: r.height,
      pixelWidth: c.width,
      pixelHeight: c.height,
    });
    c.width = c.height = 1;
    nodes.push({
      id: nodeId,
      sourcePath: path(el),
      kind: "image",
      frame: r,
      assetId,
    });
    paintOps.push({ op: "draw", nodeId });
    let textCount = 0;
    const inventory = (n: Node, parentHidden = false) => {
      if (n instanceof Text) {
        const hidden =
          parentHidden ||
          !n.data.trim() ||
          styleOf(n.parentElement!).visibility !== "visible";
        if (!hidden) textCount++;
        textInventory.push({
          sourcePath: path(n),
          sourceText: n.data,
          nodeId: hidden ? null : nodeId,
          reason: hidden ? "invisible-svg-text" : "svg-asset",
          target: hidden
            ? { kind: "hidden", reason: "invisible-svg-text" }
            : { kind: "asset", nodeId, assetId },
        });
      } else {
        const hidden =
          parentHidden ||
          (n instanceof Element &&
            (["defs", "title", "desc", "style", "clippath", "marker"].includes(
              n.tagName.toLowerCase(),
            ) ||
              styleOf(n).display === "none"));
        for (const child of n.childNodes) inventory(child, hidden);
      }
    };
    inventory(el);
    if (textCount)
      diagnostics.push({
        code:
          r.width >= 760 && r.height >= 420
            ? "FULL_PAGE_SVG_TEXT"
            : "ASSET_TEXT",
        message: `${textCount} text sources are preserved inside a vector asset; they are not editable text boxes.`,
        sourcePath: path(el),
        nodeId,
        severity: "warning",
        details: { textCount, assetId },
      });
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
  for (const item of textInventory)
    if (!item.target)
      item.target = item.nodeId
        ? { kind: "text", nodeId: item.nodeId }
        : { kind: "hidden", reason: item.reason! };
  for (const n of nodes)
    if (
      n.kind !== "group" &&
      n.kind !== "table" &&
      (n.frame.x < 0 ||
        n.frame.y < 0 ||
        n.frame.x + n.frame.width > width ||
        n.frame.y + n.frame.height > height)
    ) {
      const outside =
        n.frame.x >= width ||
        n.frame.y >= height ||
        n.frame.x + n.frame.width <= 0 ||
        n.frame.y + n.frame.height <= 0;
      diagnostics.push({
        code: outside ? "OUTSIDE_PAGE" : "PAGE_OVERFLOW",
        sourcePath: n.sourcePath,
        nodeId: n.id,
        severity: "warning",
        message: outside
          ? "Content lies entirely outside the page."
          : "Content extends beyond the page boundary.",
      });
    }
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
