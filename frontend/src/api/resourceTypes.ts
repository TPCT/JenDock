/** Wire types of the resource-limit endpoints (docs/api-contract.md, "Resource limits"). */

export interface ResourceLimits {
  cpus: number | null;
  memory_bytes: number | null;
  memory_reservation_bytes: number | null;
  cpu_shares: number | null;
}

export interface HostCapacity {
  cpus: number;
  memory_bytes: number;
}

export interface ServiceResources {
  service: string;
  /** `null` when no limits are saved for the service. */
  limits: ResourceLimits | null;
  updated_at: string | null;
  origin?: "user" | "default" | null;
}

export interface ResourceOverview {
  host: HostCapacity;
  services: ServiceResources[];
}

export interface AppliedContainer {
  id: string;
  name: string;
  ok: boolean;
  error: string | null;
}

export interface ResourceChange {
  service: string;
  limits: ResourceLimits | null;
  applied: AppliedContainer[];
  notes: string[];
}

export interface ContainerUsage {
  id: string;
  name: string;
  service: string | null;
  /** Percent of one core: 200 means two cores. */
  cpu_percent: number;
  memory_used: number;
  memory_percent: number;
  /** Limits applied now; `null` means unlimited. */
  cpu_limit: number | null;
  memory_limit: number | null;
}

export interface ResourceUsage {
  host: HostCapacity;
  containers: ContainerUsage[];
}

/** Body of `PUT .../resources/{service}`: the complete set of limits. */
export interface ResourceLimitsInput {
  cpus: number | null;
  memory_bytes: number | null;
  memory_reservation_bytes: number | null;
  cpu_shares: number | null;
  apply_now: boolean;
}
