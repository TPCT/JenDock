import type { Model } from "../api/pipelineTypes";

/** Shown for an environment without a pipeline; rendered to canonical text by the server. */
export const STARTER_MODEL: Model = {
  agent: { type: "any" },
  environment: [],
  options: [],
  parameters: [],
  stages: [
    {
      name: "Build",
      agent: null,
      environment: [],
      options: [],
      when: null,
      post: null,
      body: {
        type: "steps",
        steps: [{ name: "echo", args: { message: "Hello from Shiftcodes" } }],
      },
    },
  ],
  post: null,
};
