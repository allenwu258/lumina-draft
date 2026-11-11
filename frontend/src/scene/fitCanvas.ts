import type { CanvasSize } from '@lumina/scene-core';

export function fitCanvas(availableWidth: number, availableHeight: number, canvas: CanvasSize) {
  return Math.min(availableWidth / canvas.width, availableHeight / canvas.height);
}
