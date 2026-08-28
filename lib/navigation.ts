import { BrainCircuit, ChartCandlestick, FlaskConical, LayoutDashboard, ShieldCheck, TrendingUp, type LucideIcon } from 'lucide-react';
export const sections=['command-center','trading','strategies','ai','experiments','risk'] as const;
export type Section=typeof sections[number];
export const sectionConfig:Record<Section,{label:string;eyebrow:string;icon:LucideIcon}>={
  'command-center':{label:'Command Center',eyebrow:'Capital Builder',icon:LayoutDashboard},
  trading:{label:'Trading',eyebrow:'Paper execution desk',icon:ChartCandlestick},
  strategies:{label:'Strategies',eyebrow:'Strategy clearance',icon:TrendingUp},
  ai:{label:'AI',eyebrow:'Decision provenance',icon:BrainCircuit},
  experiments:{label:'Experiments',eyebrow:'Research laboratory',icon:FlaskConical},
  risk:{label:'Risk',eyebrow:'Deterministic governor',icon:ShieldCheck},
};
