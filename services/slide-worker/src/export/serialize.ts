type Pptx = import("pptxgenjs").default;

export type SlidePacket = {
  background: { color: string; transparency: number };
  objects: (
    | { kind: "image"; options: import("pptxgenjs").default.ImageProps }
    | {
        kind: "text";
        text: string;
        options: import("pptxgenjs").default.TextPropsOptions;
      }
    | {
        kind: "shape";
        shape: "rect" | "roundRect";
        options: import("pptxgenjs").default.ShapeProps;
      }
  )[];
};

/** This function executes in an isolated Chromium page. No Node/author HTML access. */
export async function serializePresentation(input: {
  title: string;
  slides: SlidePacket[];
}) {
  const Constructor = (window as unknown as { PptxGenJS: new () => Pptx })
    .PptxGenJS;
  const pptx = new Constructor();
  pptx.defineLayout({ name: "SCENE_16_9", width: 800 / 60, height: 450 / 60 });
  pptx.layout = "SCENE_16_9";
  pptx.title = input.title;
  pptx.subject = "SceneIR export";
  pptx.author = "Lumina Draft";
  pptx.company = "Lumina Draft";
  for (const packet of input.slides) {
    const slide = pptx.addSlide();
    slide.background = packet.background;
    for (const object of packet.objects) {
      if (object.kind === "image") slide.addImage(object.options);
      else if (object.kind === "text")
        slide.addText(object.text, object.options);
      else slide.addShape(object.shape, object.options);
    }
  }
  const data = await pptx.write({ outputType: "base64", compression: true });
  if (typeof data !== "string" || data.length > 180_000_000)
    throw new Error("PPTX_OUTPUT_LIMIT");
  return data;
}
