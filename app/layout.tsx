import type { Metadata } from 'next';
import './globals.css';
import { Providers } from './providers';
export const metadata: Metadata = {
  metadataBase: new URL('https://kairo-trading-command-center.kendallaaron84.chatgpt.site'),
  title: 'Kairo — Trading Command Center',
  description: 'Private autonomous trading system cockpit.',
  openGraph: { title: 'Kairo — Trading Command Center', description: 'Private autonomous trading system cockpit.', images: [{url:'/og.png',width:1200,height:630,alt:'Kairo Personal AI Trading Command Center'}] },
  twitter: { card:'summary_large_image', title:'Kairo — Trading Command Center', description:'Private autonomous trading system cockpit.', images:['/og.png'] },
};
export default function RootLayout({children}:Readonly<{children:React.ReactNode}>){return <html lang="en"><body><Providers>{children}</Providers></body></html>}
