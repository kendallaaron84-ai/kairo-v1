import { DashboardApp } from '@/components/dashboard-app';
import { notFound } from 'next/navigation';
const valid=['trading','strategies','ai','experiments','risk'] as const;
type ValidSection=typeof valid[number];
export default async function SectionPage({params}:{params:Promise<{section:string}>}){const {section}=await params;if(!valid.includes(section as ValidSection))notFound();return <DashboardApp section={section as ValidSection}/>}
