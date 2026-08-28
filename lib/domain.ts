export type Tone = 'good' | 'warning' | 'danger' | 'ai' | 'neutral';
export type AutonomyAuthority = 'OBSERVE' | 'ADVISE' | 'VETO_ONLY' | 'SELECT_STRATEGY';
export type MilestoneStatus = 'complete' | 'active' | 'locked' | 'failed' | 'demoted';
export type ParameterProvenance = 'INHERITED_PROTOTYPE' | 'TEST_DEFAULT' | 'RESEARCH_HYPOTHESIS';

export interface CapitalCell {
  id: string;
  seedCapital: number;
  status: string;
  strategyId: string;
  targetTreasuryId: string;
  mission: string;
}

export interface TreasuryMilestone { shares: number; label: string }
export interface Treasury {
  id: string;
  name: string;
  symbol: string;
  contributedDollars: number;
  fractionalShares: number;
  milestones: TreasuryMilestone[];
}

export interface GovernanceState {
  autonomyLevel: number;
  autonomyLabel: string;
  authorizedCapital: number;
  strategyId: string;
  strategyClearance: 'PAPER_ONLY' | 'SHADOW' | 'LIVE';
  currentMilestone: string;
  nextUnlock: string;
  capitalScale: 'LOCKED' | 'REVIEW' | 'AUTHORIZED';
}

export interface MilestoneState {
  id: string;
  label: string;
  status: MilestoneStatus;
  progress?: { current: number; target: number };
}

export interface StrategyParameter { name: string; value: string; provenance: ParameterProvenance }
export interface StrategyRuntime {
  id: string;
  version: string;
  name: string;
  instruments: string[];
  clearance: GovernanceState['strategyClearance'];
  emaPeriod: number;
  barInterval: string;
  quotePoll: string;
  warmup: { current: number; target: number };
  latestSignal: string;
  lossStreak: number;
  liveCapital: number;
  paperCapital: number;
  parameters: StrategyParameter[];
}

export interface RiskState {
  sessionNetPnl: number;
  profitCeiling: number;
  lossShutdown: number;
  safetyReserve: number;
  capitalAtRisk: number;
  governorStatus: 'AUTHORIZED' | 'DEGRADED' | 'BLOCKED';
}

export interface MarketDataHealth {
  feedStatus: 'HEALTHY' | 'DEGRADED' | 'OFFLINE';
  lastQuoteAgeMs: number;
  clockDriftMs: number;
  missingBars: number;
  droppedEvents: number;
  spreadState: 'NORMAL' | 'WIDE' | 'UNKNOWN';
  streamReconnects: number;
}

export interface AIDecisionTrace {
  id: string;
  strategyId: string;
  trigger: string;
  context: string;
  authority: AutonomyAuthority;
  verdict: string;
  riskGovernor: string;
  execution: string;
}

export interface SystemHealth {
  overall: 'HEALTHY' | 'DEGRADED' | 'HALTED';
  marketData: MarketDataHealth;
  strategyEngine: string;
  riskGovernor: string;
  executionEngine: string;
  lastHeartbeatMs: number;
}

export interface CapitalBuilderSnapshot {
  seedReference: number;
  todayNetPnl: number;
  safetyReserve: number;
  ownershipTreasuries: number;
  replicationFund: number;
  replicationGoal: number;
}
