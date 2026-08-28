import { DashboardApp } from '@/components/dashboard-app';
import { notFound } from 'next/navigation';
import { sections, type Section } from '@/lib/navigation';
export default async function SectionPage({params}:{params:Promise<{section:string}>}){const {section}=await params;if(section==='command-center'||!sections.includes(section as Section))notFound();return <DashboardApp section={section as Section}/>}
