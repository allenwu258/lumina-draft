SceneIR 0.3 is the persisted display contract for an 800 × 450 page. The JSON Schema in `contracts/scene-ir/0.3.0.schema.json` is shared by Node, Python and the browser. TypeScript types and reference checks are exported from this package.

- Coordinates are root-relative CSS px. Source string indices are UTF-16 offsets, matching DOM Range, including surrogate pairs.
- Group nodes retain semantic tags and text owner identity. Text nodes correspond to author DOM text runs, including rich inline text; they are never synthesized from a parent's `textContent`.
- Each text node stores original text, run style, measured lines, fragment `displayText`, source ranges, frame, baseline and natural Canvas width. Collapsed spaces, explicit breaks and text-transform are resolved during capture. The renderer does no wrapping or source slicing.
- `paintOps` is a balanced sequence of group begin/end and primitive draw operations. A drawable node appears once. Groups isolate opacity; clips also travel with stacking contexts promoted out of ordinary ancestors.
- Tables retain cell ownership and resolved geometry; cell shapes and text are ordinary draw nodes referenced once by `paintOps`.
- SVG is frozen into a transparent local PNG. Assets declare hashes, dimensions, coverage and baked effects. No entire-page screenshot is used as the rendering result.
- `textInventory` accounts for each author text run, including hidden/collapsed text. Diagnostics are informational, not a rendering dependency.
- `source` binds raw HTML/CSS hashes and converter version; `environment` records Chromium, platform and font metrics. Runtime timings and job IDs do not enter the canonical Scene content.

The renderer requires only SceneIR, the declared assets and matching local fonts. RenderPlan and PPTX conversion are deliberately outside this contract's current consumer path. Extending the contract requires changing schema, types and independent fixtures together; incompatible persisted semantics require a new version.
