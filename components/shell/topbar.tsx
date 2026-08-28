'use client';
import { Menu, Radio } from 'lucide-react';
import { sectionConfig, type Section } from '@/lib/navigation';
import { useUIStore } from '@/stores/ui-store';
export function Topbar({section}:{section:Section}){const toggle=useUIStore(s=>s.toggleMobileMenu);return <header className="topbar"><button className="icon-button mobile-menu" aria-label="Open navigation" onClick={toggle}><Menu size={19}/></button><div><p className="eyebrow">Governance Standard v0.1 · Mock mode</p><h1>{sectionConfig[section].label}</h1></div><div className="topbar-actions"><span className="status-pill good"><Radio size={13}/> M0 data healthy</span><span className="clock">CELL-A001 <small>PAPER</small></span><button className="avatar" aria-label="Private operator menu">KA</button></div></header>}
