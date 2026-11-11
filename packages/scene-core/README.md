SceneIR 0.4 is the persisted display contract for an 800 × 450 page. The versioned JSON Schemas in `contracts/scene-ir/` are shared by Node, Python and the browser. TypeScript types and reference checks are exported from this package.

- Coordinates are root-relative CSS px. Source string indices are UTF-16 offsets, matching DOM Range, including surrogate pairs.
- Group nodes retain semantic tags and text owner identity. Text nodes correspond to author DOM text runs, including rich inline text; they are never synthesized from a parent's `textContent`.
- Each text node stores original text, run style, measured lines, fragment `displayText`, source ranges, frame, baseline and natural Canvas width. Collapsed spaces, explicit breaks and text-transform are resolved during capture. The renderer does no wrapping or source slicing.
- `paintOps` is a balanced sequence of group begin/end and primitive draw operations. A drawable node appears once. Groups isolate opacity; clips also travel with stacking contexts promoted out of ordinary ancestors.
- Tables retain cell ownership and resolved geometry; cell shapes and text are ordinary draw nodes referenced once by `paintOps`.
- SVG is a normalized vector asset with frozen computed styles and local references. It declares a separately hashed PNG fallback from the same SVG, bounded at up to 3× scale. Asset dimensions describe the resource; image node frames describe display geometry. PNG fallback ownership and aspect ratio must agree. No entire-page screenshot replaces ordinary HTML text.
- `textInventory` gives each author text run one target: structured text node, SVG asset/image node, or hidden with a reason. Legacy nodeId/reason projections must agree with the target. Diagnostics include overflow and text retained in SVG; they do not drive rendering.
- `source` binds raw HTML/CSS hashes and converter version; `environment` records Chromium, platform and font metrics. Runtime timings and job IDs do not enter the canonical Scene content.

Readers validate both 0.3 and 0.4; writers emit only 0.4. Historical 0.3 PNG assets remain PNG. `textTarget` projects legacy asset text ownership without rewriting stored Scenes.

The renderer requires only SceneIR, the declared assets and matching local fonts. RenderPlan and PPTX conversion are deliberately outside this contract's current consumer path. Extending the contract requires changing schema, types and independent fixtures together; incompatible persisted semantics require a new version.
