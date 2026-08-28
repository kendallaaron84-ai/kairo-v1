import { AppShell } from '@/components/shell/app-shell';
import { AIScreen } from '@/components/screens/ai-screen';
import { CommandCenterScreen } from '@/components/screens/command-center-screen';
import { ExperimentsScreen } from '@/components/screens/experiments-screen';
import { RiskScreen } from '@/components/screens/risk-screen';
import { StrategiesScreen } from '@/components/screens/strategies-screen';
import { TradingScreen } from '@/components/screens/trading-screen';
import type { Section } from '@/lib/navigation';

const screens:Record<Section,React.ComponentType>={
  'command-center':CommandCenterScreen,
  trading:TradingScreen,
  strategies:StrategiesScreen,
  ai:AIScreen,
  experiments:ExperimentsScreen,
  risk:RiskScreen,
};

export function DashboardApp({section}:{section:Section}){const Screen=screens[section];return <AppShell section={section}><Screen/></AppShell>}
