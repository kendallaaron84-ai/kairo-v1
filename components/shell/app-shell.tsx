'use client';
import { usePathname } from 'next/navigation';
import type { Section } from '@/lib/navigation';
import { Sidebar } from './sidebar';
import { Topbar } from './topbar';
import { HaltTradingDialog } from '@/components/controls/halt-trading-dialog';
import { FlattenAllDialog } from '@/components/controls/flatten-all-dialog';
export function AppShell({section,children}:{section:Section;children:React.ReactNode}){const path=usePathname();return <div className="app-shell"><Sidebar section={section}/><main className="workspace"><Topbar section={section}/><section className="content" key={path}>{children}</section></main><HaltTradingDialog/><FlattenAllDialog/></div>}
