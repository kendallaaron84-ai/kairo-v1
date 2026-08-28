'use client';
import { create } from 'zustand';
export type ControlDialog='halt'|'flatten'|null;
interface UIState { mobileMenuOpen:boolean; dialog:ControlDialog; toggleMobileMenu:()=>void; closeMobileMenu:()=>void; openDialog:(dialog:Exclude<ControlDialog,null>)=>void; closeDialog:()=>void }
export const useUIStore=create<UIState>(set=>({mobileMenuOpen:false,dialog:null,toggleMobileMenu:()=>set(s=>({mobileMenuOpen:!s.mobileMenuOpen})),closeMobileMenu:()=>set({mobileMenuOpen:false}),openDialog:dialog=>set({dialog}),closeDialog:()=>set({dialog:null})}));
