export const argvFields = ["test_command", "command", "healthcheck"];
export function profileDraft(values) {
  return {
    dockerfile: values?.dockerfile ?? "Dockerfile",
    context: values?.context ?? ".",
    target: values?.target ?? "",
    user: values?.user ?? "",
    ...Object.fromEntries(
      argvFields.map((key) => [key, values?.[key]?.join("\n") ?? ""]),
    ),
  };
}
export function profileProblems(draft) {
  const path = (value) =>
    typeof value === "string" &&
    value.length > 0 &&
    value.length <= 240 &&
    !value.startsWith("/") &&
    !value.includes(":") &&
    !value.includes("\\") &&
    !value.split("/").some((part) => ["..", ".git", ""].includes(part)) &&
    (value === "." || !value.split("/").includes(".")) &&
    !/[\x00-\x1f\x7f]/.test(value);
  const problems = {
    dockerfile:
      path(draft.dockerfile) && draft.dockerfile !== "."
        ? ""
        : "Enter a relative repository path.",
    context: path(draft.context) ? "" : "Enter a relative repository folder.",
    target:
      !draft.target || /^[A-Za-z0-9][A-Za-z0-9_.-]{0,127}$/.test(draft.target)
        ? ""
        : "Enter a valid Docker build stage.",
    user: /^[1-9][0-9]{0,8}:[0-9]{1,9}$/.test(draft.user)
      ? ""
      : "Enter a non-root numeric UID:GID, such as 1000:1000.",
  };
  for (const key of argvFields) {
    const argv = draft[key].split("\n");
    problems[key] =
      argv.length <= 40 &&
      argv.every(
        (arg) =>
          arg.length > 0 && arg.length <= 2048 && !/[\x00-\x1f\x7f]/.test(arg),
      )
        ? ""
        : "Enter 1–40 arguments, one per line. Empty lines are not arguments.";
  }
  return problems;
}
export function profilePayload(project, environment, reviewed, draft) {
  if (Object.values(profileProblems(draft)).some(Boolean))
    throw Error("Check the build profile fields.");
  if (
    !reviewed ||
    !["project_revision", "environment_revision"].every(
      (k) => Number.isInteger(reviewed[k]) && reviewed[k] > 0,
    ) ||
    !Number.isInteger(reviewed.profile_revision) ||
    reviewed.profile_revision < 0
  )
    throw Error("Reload the source profile before saving.");
  return {
    action: "build-profile-save",
    project,
    environment,
    project_revision: reviewed.project_revision,
    environment_revision: reviewed.environment_revision,
    profile_revision: reviewed.profile_revision,
    values: {
      dockerfile: draft.dockerfile,
      context: draft.context,
      target: draft.target || null,
      user: draft.user,
      ...Object.fromEntries(
        argvFields.map((key) => [key, draft[key].split("\n")]),
      ),
    },
  };
}
