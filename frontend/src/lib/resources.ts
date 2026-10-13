import type { HostCapacity, ResourceLimits } from "../api/resourceTypes";
import { formatBytes } from "./format";

export const MIB = 1024 ** 2;
export const GIB = 1024 ** 3;
/** Docker refuses a memory limit below 6 MiB. */
export const MIN_MEMORY_BYTES = 6 * MIB;
export const MIN_CPUS = 0.01;
export const MIN_CPU_SHARES = 2;
export const MAX_CPU_SHARES = 262_144;

export type MemoryUnit = "MiB" | "GiB";
export const UNIT_BYTES: Record<MemoryUnit, number> = { MiB: MIB, GiB: GIB };

export interface Preset {
  name: string;
  cpus: number;
  memory: number;
  unit: MemoryUnit;
}

export const PRESETS: readonly Preset[] = [
  { name: "Small", cpus: 0.5, memory: 512, unit: "MiB" },
  { name: "Medium", cpus: 1, memory: 1, unit: "GiB" },
  { name: "Large", cpus: 2, memory: 4, unit: "GiB" },
];

/** What the form fields hold: text, so a half typed number is never rewritten. */
export interface LimitsForm {
  cpus: string;
  memory: string;
  memoryUnit: MemoryUnit;
  reservation: string;
  reservationUnit: MemoryUnit;
  shares: string;
}

/** A size as the number and unit a person would write: 1536 MiB reads as 1.5 GiB. */
export function splitBytes(bytes: number | null): { value: string; unit: MemoryUnit } {
  if (bytes === null) return { value: "", unit: "MiB" };
  const gib = (bytes / GIB) * 100;
  if (bytes >= GIB && Math.abs(gib - Math.round(gib)) < 1e-6) {
    return { value: String(Math.round(gib) / 100), unit: "GiB" };
  }
  return { value: String(Math.round((bytes / MIB) * 100) / 100), unit: "MiB" };
}

export function formFromLimits(limits: ResourceLimits | null): LimitsForm {
  const memory = splitBytes(limits?.memory_bytes ?? null);
  const reservation = splitBytes(limits?.memory_reservation_bytes ?? null);
  return {
    cpus: limits?.cpus == null ? "" : String(limits.cpus),
    memory: memory.value,
    memoryUnit: memory.unit,
    reservation: reservation.value,
    reservationUnit: reservation.unit,
    shares: limits?.cpu_shares == null ? "" : String(limits.cpu_shares),
  };
}

export type Parsed =
  { ok: true; limits: ResourceLimits } | { ok: false; field: keyof LimitsForm; message: string };

const NUMBER = /^\d+(?:\.\d+)?$/;

function size(
  text: string,
  unit: MemoryUnit,
  label: string,
  host: HostCapacity,
): { bytes: number | null; message?: string } {
  if (text.trim() === "") return { bytes: null };
  if (!NUMBER.test(text.trim())) {
    return { bytes: null, message: `${label} must be a number, for example 512 or 1.5.` };
  }
  const bytes = Math.round(Number(text) * UNIT_BYTES[unit]);
  if (bytes < MIN_MEMORY_BYTES) {
    return { bytes: null, message: `${label} must be at least ${formatBytes(MIN_MEMORY_BYTES)}.` };
  }
  if (bytes > host.memory_bytes) {
    return {
      bytes: null,
      message: `${label} cannot exceed the host's ${formatBytes(host.memory_bytes)} of memory.`,
    };
  }
  return { bytes };
}

/** Turns the form into limits, or says which field is wrong and why (the server checks again). */
export function parseForm(form: LimitsForm, host: HostCapacity): Parsed {
  let cpus: number | null = null;
  if (form.cpus.trim() !== "") {
    if (!NUMBER.test(form.cpus.trim())) {
      return { ok: false, field: "cpus", message: "CPU cores must be a number, for example 0.5." };
    }
    cpus = Number(form.cpus);
    if (cpus <= 0) {
      return { ok: false, field: "cpus", message: "CPU cores must be greater than 0." };
    }
    if (cpus < MIN_CPUS) {
      return {
        ok: false,
        field: "cpus",
        message: `CPU cores must be at least ${String(MIN_CPUS)}.`,
      };
    }
    if (cpus > host.cpus) {
      return {
        ok: false,
        field: "cpus",
        message: `CPU cores cannot exceed the host's ${String(host.cpus)} CPUs.`,
      };
    }
  }
  const memory = size(form.memory, form.memoryUnit, "Memory", host);
  if (memory.message) return { ok: false, field: "memory", message: memory.message };
  const reservation = size(form.reservation, form.reservationUnit, "Memory reservation", host);
  if (reservation.message) {
    return { ok: false, field: "reservation", message: reservation.message };
  }
  if (memory.bytes !== null && reservation.bytes !== null && reservation.bytes > memory.bytes) {
    return {
      ok: false,
      field: "reservation",
      message: "The memory reservation cannot be larger than the memory limit.",
    };
  }
  let shares: number | null = null;
  if (form.shares.trim() !== "") {
    shares = Number(form.shares);
    if (!Number.isInteger(shares) || shares < MIN_CPU_SHARES || shares > MAX_CPU_SHARES) {
      return {
        ok: false,
        field: "shares",
        message: `CPU shares must be a whole number from ${String(MIN_CPU_SHARES)} to ${String(MAX_CPU_SHARES)} (default 1024).`,
      };
    }
  }
  const limits: ResourceLimits = {
    cpus,
    memory_bytes: memory.bytes,
    memory_reservation_bytes: reservation.bytes,
    cpu_shares: shares,
  };
  if (Object.values(limits).every((value) => value === null)) {
    return {
      ok: false,
      field: "cpus",
      message: "Set at least one limit, or use Remove limit.",
    };
  }
  return { ok: true, limits };
}

export function describeLimits(limits: ResourceLimits | null): string {
  if (limits === null) return "No limit";
  const parts: string[] = [];
  if (limits.cpus !== null) parts.push(`${String(limits.cpus)} CPU${limits.cpus === 1 ? "" : "s"}`);
  if (limits.memory_bytes !== null) parts.push(formatBytes(limits.memory_bytes));
  if (limits.memory_reservation_bytes !== null) {
    parts.push(`${formatBytes(limits.memory_reservation_bytes)} reserved`);
  }
  if (limits.cpu_shares !== null) parts.push(`${String(limits.cpu_shares)} shares`);
  return parts.join(" · ");
}
