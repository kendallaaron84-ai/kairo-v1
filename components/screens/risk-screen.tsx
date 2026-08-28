import { GuardrailsWidget } from '@/components/command-center/guardrails-widget';
import { GovernanceMatrix } from '@/components/governance/governance-matrix';
import { MilestoneLadder } from '@/components/governance/milestone-ladder';
import { SystemHealth } from '@/components/system/system-health';
import { PageIntro, StatusBadge } from '@/components/ui';
import { governance, milestones, riskState, systemHealth } from '@/data/mock-data';
export function RiskScreen(){return <><PageIntro section="risk" title="Deterministic capital governor" description="Hard boundaries remain authoritative over strategies, autonomy, and simulated execution." aside={<StatusBadge tone="good">{riskState.governorStatus}</StatusBadge>}/><div className="risk-contract-grid"><GuardrailsWidget risk={riskState}/><GovernanceMatrix state={governance}/></div><MilestoneLadder milestones={milestones} governance={governance}/><SystemHealth system={systemHealth}/></>}
