import { isDesignCanvas, type CanvasSize } from '@lumina/scene-core';
import './design.css';

const presets = [
  { label: '竖版 9:16', width: 1080, height: 1920 },
  { label: '竖版 3:4', width: 900, height: 1200 },
  { label: '方形 1:1', width: 1080, height: 1080 },
  { label: '横版 16:9', width: 1280, height: 720 },
  { label: '横版 4:3', width: 1200, height: 900 },
];

export type DesignDraft = { format: string; width: string; height: string; pages: string };
export const defaultDesignDraft: DesignDraft = {
  format: '0',
  width: '1080',
  height: '1920',
  pages: '',
};

export function designOptions(draft: DesignDraft) {
  const canvas = { width: Number(draft.width), height: Number(draft.height) };
  const pageCount = draft.pages === '' ? null : Number(draft.pages);
  const canvasValid = isDesignCanvas(canvas);
  const pagesValid =
    pageCount === null || (Number.isInteger(pageCount) && pageCount >= 1 && pageCount <= 20);
  return { canvas, page_count: pageCount, canvasValid, pagesValid };
}

export function canvasRatio({ width, height }: CanvasSize) {
  let a = width;
  let b = height;
  while (b) [a, b] = [b, a % b];
  return `${width / a}:${height / a}`;
}

export function DesignOptions({
  draft,
  onChange,
}: {
  draft: DesignDraft;
  onChange: (draft: DesignDraft) => void;
}) {
  const { canvas, canvasValid, pagesValid } = designOptions(draft);
  return (
    <fieldset className="design-options">
      <legend>设计规格</legend>
      <div className="design-option-fields">
        <label>
          画布比例
          <select
            value={draft.format}
            onChange={(event) => {
              const format = event.target.value;
              const preset = presets[Number(format)];
              onChange(
                preset
                  ? { ...draft, format, width: String(preset.width), height: String(preset.height) }
                  : { ...draft, format },
              );
            }}
          >
            {presets.map(({ label, width, height }, index) => (
              <option key={label} value={index}>
                {label} · {width} × {height}
              </option>
            ))}
            <option value="custom">自定义尺寸</option>
          </select>
        </label>
        {draft.format === 'custom' &&
          (['width', 'height'] as const).map((axis) => (
            <label key={axis}>
              {axis === 'width' ? '宽度 (px)' : '高度 (px)'}
              <input
                type="number"
                min={320}
                max={1920}
                step={1}
                value={draft[axis]}
                aria-invalid={!canvasValid}
                onChange={(event) => onChange({ ...draft, [axis]: event.target.value })}
              />
            </label>
          ))}
        <label>
          页数
          <input
            type="number"
            min={1}
            max={20}
            step={1}
            placeholder="自动"
            value={draft.pages}
            aria-invalid={!pagesValid}
            onChange={(event) => onChange({ ...draft, pages: event.target.value })}
          />
        </label>
      </div>
      <small>
        {!canvasValid
          ? '宽高须为 320–1920 的整数，面积不超过 2,073,600 px²。'
          : !pagesValid
            ? '页数须为 1–20 的整数，留空自动决定。'
            : `所有页面使用 ${canvas.width} × ${canvas.height} px，创建后固定。页数留空由大纲决定。`}
      </small>
    </fieldset>
  );
}
