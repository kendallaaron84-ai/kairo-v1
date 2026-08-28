import type { Metadata } from 'next';
import './globals.css';
import { Providers } from './providers';
export const metadata: Metadata = {
  metadataBase: new URL('https://kairo-trading-command-center.kendallaaron84.chatgpt.site'),
  title: 'Kairo — Autonomous Capital Builder',
  description: 'Private governance cockpit for the $100 Autonomous Capital Builder.',
  openGraph: { title: 'Kairo — Autonomous Capital Builder', description: 'Private governance cockpit for the $100 Autonomous Capital Builder.', images: [{url:'/og.png',width:1200,height:630,alt:'Kairo Autonomous Capital Builder'}] },
  twitter: { card:'summary_large_image', title:'Kairo — Autonomous Capital Builder', description:'Private governance cockpit for the $100 Autonomous Capital Builder.', images:['/og.png'] },
};
export default function RootLayout({children}:Readonly<{children:React.ReactNode}>){return <html lang="en"><body><Providers>{children}</Providers></body></html>}
