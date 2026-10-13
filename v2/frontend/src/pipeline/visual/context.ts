import { createContext, useContext } from "react";
import type { Call, Catalog, Model, Stage, StepDef } from "../../api/pipelineTypes";

export interface EditorEnv {
  catalog: Catalog;
  /** Names of the environment's secrets (never values). */
  secrets: string[];
  /** Logical names of images built by dockerBuild-like steps in the pipeline. */
  builtImages: string[];
  /** Branch configured for this environment, used as the default of branch conditions. */
  branch: string;
  environment: string;
}

export const EditorEnvContext = createContext<EditorEnv | null>(null);

export function useEditorEnv(): EditorEnv {
  const value = useContext(EditorEnvContext);
  if (!value) throw new Error("EditorEnvContext is missing");
  return value;
}

export function moveItem<T>(list: T[], index: number, delta: -1 | 1): T[] {
  const target = index + delta;
  if (target < 0 || target >= list.length) return list;
  const next = [...list];
  const [item] = next.splice(index, 1);
  if (item === undefined) return list;
  next.splice(target, 0, item);
  return next;
}

export function replaceAt<T>(list: T[], index: number, item: T): T[] {
  return list.map((existing, i) => (i === index ? item : existing));
}

export function removeAt<T>(list: T[], index: number): T[] {
  return list.filter((_, i) => i !== index);
}

function callsOf(stage: Stage): Call[] {
  const own = stage.post?.blocks.flatMap((block) => block.steps) ?? [];
  switch (stage.body.type) {
    case "steps":
      return [...stage.body.steps, ...own];
    default:
      return [...stage.body.stages.flatMap(callsOf), ...own];
  }
}

function flatten(calls: Call[]): Call[] {
  return calls.flatMap((call) => [call, ...flatten(call.body ?? [])]);
}

/** Image names produced by steps whose catalogue entry marks an argument as `produces_image`. */
export function builtImagesOf(model: Model, steps: StepDef[]): string[] {
  const producers = new Map<string, string>();
  for (const def of steps) {
    const arg = def.args.find((item) => item.role === "produces_image");
    if (arg) producers.set(def.name, arg.name);
  }
  const names: string[] = [];
  const calls = flatten([
    ...model.stages.flatMap(callsOf),
    ...(model.post?.blocks.flatMap((block) => block.steps) ?? []),
  ]);
  for (const call of calls) {
    const argName = producers.get(call.name);
    const value = argName ? call.args[argName] : undefined;
    if (typeof value === "string" && value !== "" && !names.includes(value)) names.push(value);
  }
  return names;
}
