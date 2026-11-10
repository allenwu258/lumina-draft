import { readFileSync } from "node:fs";
export const css = readFileSync(
  new URL("../../../backend/app/tailwind-ppt-v1.css", import.meta.url),
  "utf8",
);
export const profileHead = `<style>${css}</style><style>#ppt-page{width:800px;height:450px;box-sizing:border-box;overflow:hidden;--ppt-background:#F8FAFC;--ppt-surface:#FFFFFF;--ppt-primary:#2563EB;--ppt-text:#1F2937;--ppt-muted:#64748B;--ppt-title-size:32px;--ppt-body-size:18px;--ppt-caption-size:12px;--ppt-font-family:Arial,"Microsoft YaHei",sans-serif}</style>`;
export function tailwind(body) {
  return `<!doctype html><html><head><meta charset="utf-8">${profileHead}</head><body><main id="ppt-page" data-css-profile="tailwind-ppt-v1" class="bg-ppt-background p-8 font-ppt text-ppt-text">${body}</main></body></html>`;
}
export const fixture =
  tailwind(`<h1 class="text-ppt-title font-bold mb-4">SceneIR 中文渲染</h1>
<div class="grid grid-cols-2 gap-4"><section class="bg-ppt-surface rounded-lg border border-ppt-primary p-4"><p class="text-ppt-body">中文自动换行与 English rich text，保留<strong class="text-ppt-primary">重点内容</strong>。<br>新的一行内容</p><p class="uppercase tracking-wide text-sm mt-2">hello   scene ir</p><p class="whitespace-pre-wrap text-xs">保留  空格\n第二行</p></section><section class="bg-ppt-primary rounded-xl p-4 opacity-75"><svg width="80" height="50" viewBox="0 0 80 50"><rect x="2" y="2" width="60" height="40" rx="8" fill="#fff"/><circle cx="65" cy="25" r="12" fill="#ffcc00"/></svg><p class="text-ppt-body mt-2">组合透明度</p></section></div>
<table class="w-full text-sm mt-4 border border-ppt-primary"><tbody><tr class="bg-ppt-surface"><th class="text-left p-2">项目</th><th class="text-left p-2">进度</th></tr><tr><td class="p-2">转换服务</td><td class="p-2">完成</td></tr></tbody></table>`);
