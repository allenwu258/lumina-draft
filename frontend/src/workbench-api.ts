import type { SceneSummary } from './scene/scene-api';

export type Slide = { slide_id: string; page_number: number; page_type: string; title: string; key_message: string; content_outline: string[]; design_notes: string; summary: string };
export type OutlineDocument = { schema_version: string; brief: { title: string; purpose: string; audience: string; language: string; tone: string; assumptions: string[] }; style_spec: { description: string; colors: Record<string,string>; typography: {font_family:string;title_size_px:number;body_size_px:number;caption_size_px:number}; layout_rules:string[] }; slides: Slide[] };
export type Page = { id:string; run_id:string; slide_id:string; page_number:number; title:string; status:string; html_hash:string|null; error:string|null; error_code:string|null; scene?:SceneSummary };
export type ConversationMessage = {id:string;seq:number;role:string;kind:string;status:string;body_text:string|null;payload:Record<string,unknown>;created_at:string};
export type OutlineVersion = {id:string; revision:number; content_hash:string; confirmed:boolean; document:OutlineDocument; message_id?:string};
export type ArtifactSelection = { artifact_message_id: string; slide_id?: string; source_ref?: string; label: string };
export type CardAction = { type: 'compose_deck'; artifact_message_id: string; expected_content_hash: string } | { type: 'retry_deck'; artifact_message_id: string };

export const api = async <T,>(path:string, init?:RequestInit):Promise<T> => { const response=await fetch(`/api/v1${path}`,{...init,headers:{'Content-Type':'application/json',...(init?.headers||{})}}); const data=await response.json(); if(!response.ok) throw new Error(data?.detail?.message||data?.detail||'请求失败'); return data as T; };
