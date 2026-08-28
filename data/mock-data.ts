import type { AIDecisionTrace, CapitalBuilderSnapshot, CapitalCell, GovernanceState, MarketDataHealth, MilestoneState, RiskState, StrategyRuntime, SystemHealth, Treasury } from '@/lib/domain';

export const capitalSnapshot: CapitalBuilderSnapshot = { seedReference:100, todayNetPnl:2.14, safetyReserve:8.4, ownershipTreasuries:7.93, replicationFund:4.2, replicationGoal:100 };
export const capitalCells: CapitalCell[] = [{ id:'CELL-A001', seedCapital:100, status:'Apprentice', strategyId:'EMA-CROSS-001', targetTreasuryId:'META', mission:'Generate capital for the META Ownership Treasury.' }];
export const treasuries: Treasury[] = [
  { id:'META', name:'META Treasury', symbol:'META', contributedDollars:7.93, fractionalShares:0.0138, milestones:[{shares:.25,label:'0.25 share'},{shares:.5,label:'0.50 share'},{shares:1,label:'1.00 share'}] },
  { id:'NVDA', name:'NVIDIA Treasury', symbol:'NVDA', contributedDollars:0, fractionalShares:0, milestones:[{shares:.25,label:'0.25 share'},{shares:.5,label:'0.50 share'},{shares:1,label:'1.00 share'}] },
  { id:'BYD', name:'BYD Treasury', symbol:'BYD', contributedDollars:0, fractionalShares:0, milestones:[{shares:.25,label:'0.25 share'},{shares:.5,label:'0.50 share'},{shares:1,label:'1.00 share'}] },
];
export const governance: GovernanceState = { autonomyLevel:1, autonomyLabel:'Apprentice', authorizedCapital:100, strategyId:'EMA-CROSS-001', strategyClearance:'PAPER_ONLY', currentMilestone:'M2 — Execution Fidelity', nextUnlock:'Guarded Autonomous Execution', capitalScale:'LOCKED' };
export const milestones: MilestoneState[] = [
  {id:'M0',label:'Market/Data Integrity',status:'complete'}, {id:'M1',label:'System Integrity',status:'complete'},
  {id:'M2',label:'Execution Fidelity',status:'active',progress:{current:14,target:20}}, {id:'M3',label:'Guarded Autonomous Survival',status:'locked'},
  {id:'M4',label:'First Profit Siphon',status:'locked'}, {id:'M5',label:'Resilient Buffer',status:'locked'}, {id:'M6',label:'Scale Evaluation',status:'locked'},
];
export const riskState: RiskState = { sessionNetPnl:2.14, profitCeiling:20, lossShutdown:-6, safetyReserve:8.4, capitalAtRisk:100, governorStatus:'AUTHORIZED' };
export const strategyRuntime: StrategyRuntime = {
  id:'EMA-CROSS-001',version:'1.0.0',name:'TQQQ/SQQQ 9-EMA Momentum Prototype',instruments:['TQQQ','SQQQ'],clearance:'PAPER_ONLY',emaPeriod:9,barInterval:'1 minute',quotePoll:'15 seconds',warmup:{current:6,target:9},latestSignal:'NONE',lossStreak:0,liveCapital:0,paperCapital:100,
  parameters:[{name:'EMA Period',value:'9',provenance:'INHERITED_PROTOTYPE'},{name:'Bar Interval',value:'1 minute',provenance:'TEST_DEFAULT'},{name:'Quote Poll',value:'15 seconds',provenance:'RESEARCH_HYPOTHESIS'}],
};
export const marketDataHealth: MarketDataHealth = { feedStatus:'HEALTHY',lastQuoteAgeMs:82,clockDriftMs:14,missingBars:0,droppedEvents:0,spreadState:'NORMAL',streamReconnects:0 };
export const aiDecision: AIDecisionTrace = { id:'TRACE-001',strategyId:'EMA-CROSS-001',trigger:'Bullish 9-EMA crossover',context:'Trending regime detected',authority:'ADVISE',verdict:'Favorable context',riskGovernor:'PAPER AUTHORIZED',execution:'Simulated order submitted' };
export const systemHealth: SystemHealth = { overall:'HEALTHY',marketData:marketDataHealth,strategyEngine:'PAPER READY',riskGovernor:'AUTHORIZED',executionEngine:'SIMULATION',lastHeartbeatMs:48 };
